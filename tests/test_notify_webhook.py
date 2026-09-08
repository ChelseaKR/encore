"""Signed outbound webhooks (issue #56): envelope, signature, delivery, secrecy.

Four properties carry this feature, and each has its own class here.

**The body is byte-stable and the signature is over those exact bytes.** A
signature over a JSON document means nothing if the bytes signed are not the
bytes sent, and `json.dumps` is free to vary with key insertion order. The
envelope is pinned key by key and value by value, because a property test cannot
catch a wrong constant: "the payload has a `schema_version`" would pass on any
number, and the number is what a consumer branches on.

**A webhook is an ordinary channel.** Routing, muting, backoff and the terminal
``failed`` state are the engine's, not this module's, and the tests here prove
the new sender inherits them rather than reimplementing them.

**The URL and the secret are credentials.** They are held to the same rule as
the Plex token: absent from the raw database bytes, absent from logs.

**Nothing leaks into the envelope that should not be there.** Checked by
grepping the emitted body for sentinel needles that only exist in the parts of
the record a webhook must not carry.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from encore.artistsettings import SettingsOverride
from encore.models import CHANNEL_KIND_APPRISE, CHANNEL_KIND_WEBHOOK
from encore.notify.engine import run_delivery_cycle, send_test_notification, sender_for
from encore.notify.sender import DeliveryError
from encore.notify.webhook import (
    SCHEMA_VERSION,
    SIGNATURE_HEADER,
    TYPE_HEADER,
    WebhookSender,
    canonical_body,
    channel_test_envelope,
    event_envelope,
    sign,
    verify,
)
from encore.storage import DB_FILENAME, Storage, StorageError
from tests.notify_fixtures import (
    ARTIST_MBID,
    ARTIST_NAME,
    GROUP_MBID,
    MACHINE_ID,
    RATING_KEY,
    RELEASE_TITLE,
    make_view,
    seed_event,
)

SECRET = "WEBHOOK-SECRET-needle-5e2c9a"  # noqa: S105 - deliberately fake, exists to be grepped for
WEBHOOK_URL = "https://hooks.example/encore?token=WEBHOOK-URL-needle-7d13b8"
FIXED_NOW = 1_757_203_200


class RecordingPoster:
    """A poster that records what would have gone out and answers a fixed status."""

    def __init__(self, status: int = 204, raise_with: Exception | None = None) -> None:
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []
        self.status = status
        self.raise_with = raise_with

    def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> int:
        self.calls.append((url, body, headers))
        if self.raise_with is not None:
            raise self.raise_with
        return self.status


def _sender(poster: RecordingPoster, secret: str = SECRET) -> WebhookSender:
    return WebhookSender(secret, poster=poster, clock=lambda: FIXED_NOW)


class TestTheEnvelope:
    """Pinned key by key. A consumer branches on these literals."""

    def test_it_is_exactly_this_document(self) -> None:
        view = make_view(kind="new")
        envelope = event_envelope(view, machine_identifier=MACHINE_ID)
        assert envelope == {
            "schema_version": 1,
            "event_type": "release.new",
            "event_id": 1,
            "occurred_at": "2026-08-01T12:00:00Z",
            "artist": {"name": ARTIST_NAME, "mbid": ARTIST_MBID},
            "release_group": {
                "title": RELEASE_TITLE,
                "mbid": GROUP_MBID,
                "primary_type": "Album",
                "secondary_types": [],
                "first_release_date": "2026-08-14",
            },
            "links": {
                "cover_art": (f"https://coverartarchive.org/release-group/{GROUP_MBID}/front"),
                "plex": (
                    f"https://app.plex.tv/desktop/#!/server/{MACHINE_ID}"
                    f"/details?key=%2Flibrary%2Fmetadata%2F{RATING_KEY}"
                ),
            },
        }

    def test_the_schema_version_is_one(self) -> None:
        """The literal, not just "there is a version". Consumers branch on it."""
        assert SCHEMA_VERSION == 1
        assert event_envelope(make_view())["schema_version"] == 1

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("new", "release.new"),
            ("upcoming", "release.upcoming"),
            ("date_changed", "release.date_changed"),
        ],
    )
    def test_every_release_kind_maps_to_its_wire_type(self, kind: str, expected: str) -> None:
        assert event_envelope(make_view(kind=kind))["event_type"] == expected

    def test_absent_values_are_null_and_the_keys_stay(self) -> None:
        """Absence rendered as absence, not as a missing key.

        A consumer that has to tell "no Plex link" from "this build stopped
        sending the key" is reading absence as a value.
        """
        envelope = event_envelope(make_view(primary_type=None, plex_rating_key=None))
        assert envelope["release_group"]["primary_type"] is None
        assert envelope["links"]["plex"] is None
        assert "plex" in envelope["links"]
        assert "primary_type" in envelope["release_group"]

    def test_a_partial_release_date_is_not_padded(self) -> None:
        """Padding 2027 to 2027-01-01 would invent precision MusicBrainz did not publish."""
        envelope = event_envelope(make_view(first_release_date="2027"))
        assert envelope["release_group"]["first_release_date"] == "2027"

    def test_an_undated_release_group_publishes_null_rather_than_an_empty_string(self) -> None:
        """The absence spelled `""` upstream, published as the absence it is.

        `matching.mb._parse_release_group` writes `""` for a release group
        MusicBrainz has not dated, and `watch.engine._kind_for_unseen` raises an
        ordinary `new` event for it. The envelope published that `""` verbatim
        while `channel_test_envelope` sent `null` for the same key and
        `notify.render` printed "date not announced" — three spellings of one
        fact, one of them a value.
        """
        envelope = event_envelope(make_view(first_release_date=""))
        assert envelope["release_group"]["first_release_date"] is None
        assert "first_release_date" in envelope["release_group"]

    def test_an_unresolvable_artist_name_publishes_null_rather_than_an_empty_string(self) -> None:
        """`storage.list_event_views` ends its name lookup with `.get(mbid, "")`."""
        assert event_envelope(make_view(artist_name=""))["artist"]["name"] is None

    def test_no_value_in_the_envelope_is_an_empty_string(self) -> None:
        """The property, over a view whose every absence is spelled `""`.

        Asserted on leaves rather than on the two keys above, so a third field
        that starts arriving empty is caught by the rule instead of needing its
        own test written first.
        """
        envelope = event_envelope(make_view(first_release_date="", artist_name=""))
        empty: list[str] = []
        for section, value in envelope.items():
            if isinstance(value, dict):
                empty.extend(f"{section}.{key}" for key, leaf in value.items() if leaf == "")
            elif value == "":
                empty.append(section)
        assert not empty, f"absence published as an empty string: {empty}"

    def test_a_real_value_is_never_coerced_to_null(self) -> None:
        """The boundary in the other direction.

        `or None` on a populated string would be a different defect with the
        same shape, and an empty `secondary_types` list is a *measurement* —
        "MusicBrainz publishes none" — that must survive as `[]`.
        """
        envelope = event_envelope(make_view(first_release_date="2027-03", artist_name="Låpsley"))
        assert envelope["release_group"]["first_release_date"] == "2027-03"
        assert envelope["artist"]["name"] == "Låpsley"
        assert envelope["release_group"]["secondary_types"] == []

    def test_the_test_fire_has_the_same_shape_as_a_release(self) -> None:
        """So a subscriber's parser is exercised by it, not bypassed."""
        real = event_envelope(make_view())
        fired = channel_test_envelope(occurred_at="2026-09-06T21:00:00Z")
        assert fired.keys() == real.keys()
        assert fired["release_group"].keys() == real["release_group"].keys()
        assert fired["event_type"] == "channel.test"
        assert fired["event_id"] is None

    def test_it_matches_the_published_schema(self) -> None:
        """The committed JSON Schema and the builder cannot drift apart."""
        schema = json.loads(
            (
                Path(__file__).resolve().parent.parent / "docs" / "webhook-event-v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        envelope = event_envelope(make_view(), machine_identifier=MACHINE_ID)
        assert set(schema["properties"]) == set(envelope)
        assert set(schema["required"]) == set(envelope)
        assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
        for section in ("artist", "release_group", "links"):
            assert set(schema["properties"][section]["properties"]) == set(envelope[section])
            assert set(schema["properties"][section]["required"]) == set(envelope[section])
        emitted_types = {
            event_envelope(make_view(kind=kind))["event_type"]
            for kind in ("new", "upcoming", "date_changed")
        } | {channel_test_envelope(occurred_at="x")["event_type"]}
        assert set(schema["properties"]["event_type"]["enum"]) == emitted_types


class TestTheBodyIsByteStable:
    def test_the_same_envelope_always_produces_the_same_bytes(self) -> None:
        """Built in a different key order, and the bytes must not move."""
        first = event_envelope(make_view())
        shuffled = dict(reversed(list(first.items())))
        assert canonical_body(first) == canonical_body(shuffled)

    def test_it_is_compact_and_sorted(self) -> None:
        body = canonical_body({"b": 1, "a": 2})
        assert body == b'{"a":2,"b":1}'

    def test_a_non_ascii_name_survives_as_utf8(self) -> None:
        """Keep the name the record holds, rather than escaping it.

        The bytes stay deterministic either way; a consumer reading the raw
        body should still see the artist's own name in it.
        """
        body = canonical_body(event_envelope(make_view(artist_name="Sigur Rós")))
        assert "Sigur Rós".encode() in body


class TestTheSignature:
    def test_it_verifies_with_the_shared_secret(self) -> None:
        body = canonical_body(event_envelope(make_view()))
        header = sign(SECRET, body, timestamp=FIXED_NOW)
        assert verify(SECRET, body, header, now=FIXED_NOW)

    def test_one_changed_byte_fails_verification(self) -> None:
        body = canonical_body(event_envelope(make_view()))
        header = sign(SECRET, body, timestamp=FIXED_NOW)
        tampered = body.replace(b"release.new", b"release.neW")
        assert tampered != body
        assert not verify(SECRET, tampered, header, now=FIXED_NOW)

    def test_a_different_secret_fails_verification(self) -> None:
        body = canonical_body(event_envelope(make_view()))
        header = sign(SECRET, body, timestamp=FIXED_NOW)
        assert not verify(SECRET + "x", body, header, now=FIXED_NOW)

    def test_the_timestamp_is_inside_the_signed_material(self) -> None:
        """Or a captured request could be replayed under a fresh timestamp."""
        body = canonical_body(event_envelope(make_view()))
        header = sign(SECRET, body, timestamp=FIXED_NOW)
        digest = header.split("v1=")[1]
        replayed = f"t={FIXED_NOW + 60},v1={digest}"
        assert not verify(SECRET, body, replayed, now=FIXED_NOW + 60)

    def test_a_stale_signature_is_rejected_however_well_it_verifies(self) -> None:
        body = canonical_body(event_envelope(make_view()))
        header = sign(SECRET, body, timestamp=FIXED_NOW)
        assert verify(SECRET, body, header, now=FIXED_NOW + 299)
        assert not verify(SECRET, body, header, now=FIXED_NOW + 301)

    @pytest.mark.parametrize("header", ["", "nonsense", "t=abc,v1=ff", "v1=ff", f"t={FIXED_NOW}"])
    def test_an_unreadable_header_is_refused_rather_than_crashing(self, header: str) -> None:
        body = canonical_body(event_envelope(make_view()))
        assert not verify(SECRET, body, header, now=FIXED_NOW)

    def test_the_documented_verifier_is_the_one_that_runs(self) -> None:
        """docs/webhooks.md publishes a verifier; this is that code, transcribed.

        A documented recipe nobody executes is a recipe that rots. This one is
        run against a real signed request on every test run.
        """
        import hmac
        from hashlib import sha256

        poster = RecordingPoster()
        _sender(poster).send(WEBHOOK_URL, _notification_for(make_view()))
        _url, body, headers = poster.calls[0]
        parts = dict(p.strip().split("=", 1) for p in headers[SIGNATURE_HEADER].split(","))
        expected = hmac.new(
            SECRET.encode(), f"{int(parts['t'])}.".encode() + body, sha256
        ).hexdigest()
        assert hmac.compare_digest(expected, parts["v1"])


def _notification_for(view: Any) -> Any:
    from encore.notify.render import render_event

    return render_event(view).__class__(
        title="t", body="b", envelopes=(event_envelope(view, machine_identifier=MACHINE_ID),)
    )


class TestTheRequest:
    def test_it_carries_the_event_type_header_and_the_signature(self) -> None:
        poster = RecordingPoster()
        _sender(poster).send(WEBHOOK_URL, _notification_for(make_view(kind="upcoming")))
        url, body, headers = poster.calls[0]
        assert url == WEBHOOK_URL
        assert headers[TYPE_HEADER] == "release.upcoming"
        assert headers["Content-Type"] == "application/json"
        assert verify(SECRET, body, headers[SIGNATURE_HEADER], now=FIXED_NOW)

    @pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
    def test_any_2xx_is_a_success(self, status: int) -> None:
        _sender(RecordingPoster(status=status)).send(WEBHOOK_URL, _notification_for(make_view()))

    @pytest.mark.parametrize("status", [199, 300, 301, 400, 401, 404, 429, 500, 502])
    def test_anything_else_is_a_delivery_failure(self, status: int) -> None:
        with pytest.raises(DeliveryError, match=str(status)):
            _sender(RecordingPoster(status=status)).send(
                WEBHOOK_URL, _notification_for(make_view())
            )

    def test_a_transport_failure_reports_the_type_and_not_the_url(self) -> None:
        """An httpx exception message can carry the URL, which is a credential."""
        boom = RuntimeError(f"connection to {WEBHOOK_URL} refused")
        with pytest.raises(DeliveryError) as caught:
            _sender(RecordingPoster(raise_with=boom)).send(
                WEBHOOK_URL, _notification_for(make_view())
            )
        assert "RuntimeError" in str(caught.value)
        assert "needle" not in str(caught.value)

    def test_a_failing_status_does_not_put_the_response_in_the_error(self) -> None:
        """The operator's own endpoint is free to echo the URL into its body."""
        with pytest.raises(DeliveryError) as caught:
            _sender(RecordingPoster(status=500)).send(WEBHOOK_URL, _notification_for(make_view()))
        assert str(caught.value) == "the webhook endpoint answered HTTP 500"


@pytest.fixture
def webhook_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path)
    storage.add_channel(
        "hooks", WEBHOOK_URL, kind=CHANNEL_KIND_WEBHOOK, secret=SECRET, mode="instant"
    )
    return storage


class TestTheChannel:
    def test_a_webhook_channel_must_carry_a_secret(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path)
        with pytest.raises(StorageError, match="signing secret"):
            storage.add_channel("hooks", WEBHOOK_URL, kind=CHANNEL_KIND_WEBHOOK)
        storage.close()

    def test_an_apprise_channel_may_not_carry_one(self, tmp_path: Path) -> None:
        """A credential on disk for something that never signs anything."""
        storage = Storage(tmp_path)
        with pytest.raises(StorageError, match="takes no secret"):
            storage.add_channel("n", "ntfy://x", kind=CHANNEL_KIND_APPRISE, secret=SECRET)
        storage.close()

    def test_an_unknown_kind_is_refused(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path)
        with pytest.raises(StorageError, match="invalid channel kind"):
            storage.add_channel("n", "ntfy://x", kind="carrier-pigeon")
        storage.close()

    def test_an_existing_database_migrates_to_apprise(self, tmp_path: Path) -> None:
        """The column defaults so a pre-webhook channel keeps behaving as it did."""
        storage = Storage(tmp_path)
        storage.add_channel("n", "ntfy://x")
        channel = storage.get_channel("n")
        assert channel is not None
        assert channel.kind == CHANNEL_KIND_APPRISE
        assert channel.secret_cipher is None
        storage.close()

    def test_the_secret_round_trips_through_the_cipher(self, webhook_storage: Storage) -> None:
        channel = webhook_storage.get_channel("hooks")
        assert channel is not None
        assert webhook_storage.channel_secret(channel) == SECRET
        webhook_storage.close()

    def test_a_webhook_channel_resolves_to_a_webhook_sender(self, webhook_storage: Storage) -> None:
        channel = webhook_storage.get_channel("hooks")
        assert channel is not None
        assert isinstance(sender_for(webhook_storage, channel), WebhookSender)
        webhook_storage.close()

    def test_a_secretless_webhook_channel_is_skipped_rather_than_sent_unsigned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one that matters. An unsigned webhook looks like a working one.

        The row is forced into that state directly, because `add_channel`
        refuses to create it -- which is the point: the skip has to hold for a
        row that got there some other way (a hand-edited database, a restore
        from a partial backup).
        """
        storage = Storage(tmp_path)
        storage.add_channel("hooks", WEBHOOK_URL, kind=CHANNEL_KIND_WEBHOOK, secret=SECRET)
        with storage.session() as session:
            session.connection().execute(text("UPDATE channels SET secret_cipher = NULL"))
            session.commit()
        channel = storage.get_channel("hooks")
        assert channel is not None
        with caplog.at_level(logging.ERROR):
            assert sender_for(storage, channel) is None
        assert "unsigned" in caplog.text
        assert SECRET not in caplog.text
        storage.close()


class TestItIsAnOrdinaryChannel:
    """Routing, muting, backoff and the terminal state are the engine's."""

    def test_a_cycle_delivers_one_signed_request_per_event(self, webhook_storage: Storage) -> None:
        seed_event(webhook_storage, kind="new")
        poster = RecordingPoster()
        report = run_delivery_cycle(webhook_storage, sender=_sender(poster))
        assert report.sent == 1
        assert len(poster.calls) == 1
        _url, body, headers = poster.calls[0]
        assert verify(SECRET, body, headers[SIGNATURE_HEADER], now=FIXED_NOW)
        assert json.loads(body)["event_type"] == "release.new"
        webhook_storage.close()

    def test_a_muted_artist_creates_no_webhook_delivery(self, webhook_storage: Storage) -> None:
        seed_event(webhook_storage, kind="new")
        webhook_storage.set_artist_settings(RATING_KEY, SettingsOverride(muted=True))
        poster = RecordingPoster()
        report = run_delivery_cycle(webhook_storage, sender=_sender(poster))
        assert poster.calls == []
        assert report.muted_skipped >= 1
        webhook_storage.close()

    def test_three_failures_back_off_and_the_fifth_goes_terminal(
        self, webhook_storage: Storage
    ) -> None:
        """A dead endpoint is a failing channel with its status recorded, not a crash."""
        seed_event(webhook_storage, kind="new")
        poster = RecordingPoster(status=500)
        now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)
        for attempt in range(5):
            report = run_delivery_cycle(
                webhook_storage, sender=_sender(poster), now=now + dt.timedelta(hours=attempt * 6)
            )
        assert report.failed == 1
        channel = webhook_storage.get_channel("hooks")
        assert channel is not None
        assert channel.consecutive_failures >= 1
        assert channel.last_error == "the webhook endpoint answered HTTP 500"
        # Terminal means terminal: a further cycle sends nothing more.
        before = len(poster.calls)
        run_delivery_cycle(webhook_storage, sender=_sender(poster), now=now + dt.timedelta(days=7))
        assert len(poster.calls) == before
        webhook_storage.close()

    def test_a_later_test_fire_clears_the_failing_state(self, webhook_storage: Storage) -> None:
        seed_event(webhook_storage, kind="new")
        run_delivery_cycle(webhook_storage, sender=_sender(RecordingPoster(status=500)))
        send_test_notification(webhook_storage, "hooks", sender=_sender(RecordingPoster()))
        channel = webhook_storage.get_channel("hooks")
        assert channel is not None
        assert channel.consecutive_failures == 0
        assert channel.last_success_at is not None
        webhook_storage.close()

    def test_a_test_fire_sends_a_real_channel_test_envelope(self, webhook_storage: Storage) -> None:
        poster = RecordingPoster()
        send_test_notification(webhook_storage, "hooks", sender=_sender(poster))
        _url, body, headers = poster.calls[0]
        assert headers[TYPE_HEADER] == "channel.test"
        assert json.loads(body)["schema_version"] == SCHEMA_VERSION
        assert verify(SECRET, body, headers[SIGNATURE_HEADER], now=FIXED_NOW)
        webhook_storage.close()

    def test_a_digest_batch_goes_as_one_request_per_event(self, tmp_path: Path) -> None:
        """The window still gates; the packaging is per event.

        A rollup to a machine is one request whose failure would leave several
        deliveries ambiguous, and this project promises no duplicate deliveries.
        """
        storage = Storage(tmp_path)
        storage.add_channel(
            "hooks",
            WEBHOOK_URL,
            kind=CHANNEL_KIND_WEBHOOK,
            secret=SECRET,
            mode="digest",
            digest_interval_hours=24.0,
        )
        seed_event(storage, kind="new", title="First needle", group_mbid=GROUP_MBID)
        seed_event(
            storage,
            kind="new",
            title="Second needle",
            group_mbid="ffffffff-1111-2222-3333-444444444444",
            rating_key="4243",
            artist_name="Other Artist",
            artist_mbid="99999999-8888-7777-6666-555555555555",
        )
        poster = RecordingPoster()
        now = dt.datetime(2026, 9, 6, 12, 0, tzinfo=dt.UTC)
        run_delivery_cycle(storage, sender=_sender(poster), now=now)
        assert len(poster.calls) == 2
        for _url, body, headers in poster.calls:
            assert verify(SECRET, body, headers[SIGNATURE_HEADER], now=FIXED_NOW)
        # The window advanced, or a digest webhook would never hold anything back.
        channel = storage.get_channel("hooks")
        assert channel is not None
        assert channel.last_digest_at is not None
        storage.close()

    def test_an_apprise_channel_still_gets_no_envelopes(self, tmp_path: Path) -> None:
        """Nothing about the ~90 existing services changes shape."""
        from tests.notify_fixtures import RecordingSender

        storage = Storage(tmp_path)
        storage.add_channel("n", "ntfy://x")
        seed_event(storage, kind="new")
        sender = RecordingSender()
        run_delivery_cycle(storage, sender=sender)
        assert sender.calls
        assert all(notification.envelopes == () for _url, notification in sender.calls)
        storage.close()


class TestNothingLeaks:
    @pytest.mark.no_secrets_in_logs
    def test_neither_the_url_nor_the_secret_is_in_the_database_file(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path)
        storage.add_channel("hooks", WEBHOOK_URL, kind=CHANNEL_KIND_WEBHOOK, secret=SECRET)
        storage.close()
        blob = b""
        for suffix in ("", "-wal", "-shm"):
            candidate = tmp_path / f"{DB_FILENAME}{suffix}"
            if candidate.exists():
                blob += candidate.read_bytes()
        assert blob, "no database file was written, so this test proved nothing"
        assert SECRET.encode() not in blob
        assert b"WEBHOOK-URL-needle-7d13b8" not in blob

    @pytest.mark.no_secrets_in_logs
    def test_a_delivery_cycle_logs_neither_credential_nor_taste_data(
        self, webhook_storage: Storage, caplog: pytest.LogCaptureFixture
    ) -> None:
        seed_event(webhook_storage, kind="new")
        with caplog.at_level(logging.DEBUG):
            run_delivery_cycle(webhook_storage, sender=_sender(RecordingPoster()))
        for needle in (SECRET, "WEBHOOK-URL-needle-7d13b8", ARTIST_NAME, RELEASE_TITLE):
            assert needle not in caplog.text
        webhook_storage.close()

    def test_the_envelope_carries_no_plex_token_and_no_internal_ids(self) -> None:
        """Grepped rather than reasoned about: the body is what a subscriber sees."""
        body = canonical_body(event_envelope(make_view(), machine_identifier=MACHINE_ID))
        payload = json.loads(body)
        assert set(payload) == {
            "schema_version",
            "event_type",
            "event_id",
            "occurred_at",
            "artist",
            "release_group",
            "links",
        }
        assert b"token" not in body.lower().replace(b"?token=", b"")
        assert "plex_rating_key" not in payload
