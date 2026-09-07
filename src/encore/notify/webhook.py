"""Signed outbound webhooks: the machine-readable half of F4's egress boundary.

The README's non-goals draw the line here in terms: "At most: standard outbound
webhooks on new-release events so *other* tools can subscribe -- Encore's
responsibility ends at the notification." Apprise's generic ``json://`` target
already reaches a URL, but what it sends is a *rendered message* -- a title and a
body of prose -- with no schema, no event type, no MBIDs and no signature. A
Home Assistant automation, an n8n flow or a small script cannot subscribe to
that; it can only scrape it.

So this module emits an event, not a message:

* a **versioned envelope** (:data:`SCHEMA_VERSION`) whose keys are documented in
  ``docs/webhooks.md`` and pinned by ``docs/webhook-event-v1.schema.json``;
* a **byte-stable body**, because a signature over a JSON document only means
  anything if the bytes signed are the bytes sent. :func:`canonical_body` sorts
  keys and uses the compact separators, so the same envelope always produces the
  same bytes on any Python;
* an **HMAC-SHA256 signature** over ``"<timestamp>." + body`` in a
  ``X-Encore-Signature: t=<unix>,v1=<hex>`` header, which is the Stripe-style
  scheme most consumers already have a verifier for. The timestamp is inside the
  signed material so a captured request cannot be replayed under a new one.

What is deliberately *not* in the envelope, and why (docs/adr/0012, the DPIA's
§4 taste-data rule):

* no Plex token, ever, and no Plex machine identifier except inside the deep
  link the user's own notifications already carry;
* no internal database identifiers beyond ``event_id``, which a consumer needs
  to deduplicate retries;
* nothing about the library beyond the one artist and release-group the event is
  about.

Two failure rules, both the same rule the Apprise sender follows. A non-2xx
response is a delivery failure and goes back through the engine's bounded
backoff. And the response body is never read into a log, an exception message or
the channel's ``last_error`` column: an operator's own endpoint is free to echo
anything into it, including the URL it was reached at.

The URL and the shared secret are credentials. They arrive here decrypted, are
used, and are never logged, never repr'd, and never put in a
:class:`~encore.notify.sender.DeliveryError` message.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from encore.notify.render import cover_art_url, plex_artist_url
from encore.notify.sender import DeliveryError

if TYPE_CHECKING:
    from encore.models import EventView
    from encore.notify.render import RenderedNotification

__all__ = [
    "SCHEMA_VERSION",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TYPE_HEADER",
    "WebhookSender",
    "canonical_body",
    "event_envelope",
    "sign",
    "verify",
]

logger = logging.getLogger(__name__)

#: Version of the envelope's shape. Within one version every key is append
#: only: removing a key, renaming one, or changing what a key holds is a
#: breaking change and moves this number. Pinned by
#: ``docs/webhook-event-v1.schema.json`` and by ``tests/test_notify_webhook.py``.
SCHEMA_VERSION = 1

#: The signature scheme's own version, carried inside the header so a future
#: scheme can be added beside this one rather than replacing it silently.
SIGNATURE_VERSION = "v1"

SIGNATURE_HEADER = "X-Encore-Signature"
TYPE_HEADER = "X-Encore-Event"

#: How long a consumer should accept a signature for, in seconds. Advisory:
#: this project cannot enforce it at the far end, and it is documented so the
#: number in ``docs/webhooks.md`` and the number a verifier defaults to are the
#: same one.
DEFAULT_TOLERANCE_SECONDS = 300

_REQUEST_TIMEOUT_SECONDS = 10.0

#: The event types this project emits. ``release.*`` mirror
#: `encore.models.RELEASE_EVENT_KINDS`; ``channel.test`` is the one a test fire
#: produces, so a consumer wiring up an endpoint can prove the path works
#: without waiting for a real release.
TEST_EVENT_TYPE = "channel.test"


def event_type_for(kind: str) -> str:
    """Return the wire event type for a `ReleaseEvent.kind`."""
    return f"release.{kind}"


def event_envelope(
    view: EventView,
    *,
    machine_identifier: str | None = None,
    cover_art_base_url: str | None = None,
) -> dict[str, Any]:
    """Build the versioned envelope for one release event.

    Flat and total: every key is present on every event, and a value the record
    does not have is ``null`` rather than absent. A consumer that has to tell
    "this release has no cover art" from "this build stopped sending the key" is
    reading absence as a value, which is the defect class this project spends
    most of its tests on.

    ``links.plex`` is null before a Plex sync has run, because the deep link
    needs the server's machine identifier and one built from a guess goes
    nowhere -- the same rule ``encore.notify.render`` applies to the human text.
    """
    plex_link = (
        plex_artist_url(machine_identifier, view.plex_rating_key)
        if machine_identifier and view.plex_rating_key
        else None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": event_type_for(view.kind),
        "event_id": view.event_id,
        "occurred_at": _iso(view.created_at),
        "artist": {"name": view.artist_name, "mbid": view.artist_mbid},
        "release_group": {
            "title": view.title,
            "mbid": view.release_group_mbid,
            "primary_type": view.primary_type,
            "secondary_types": list(view.secondary_types),
            "first_release_date": view.first_release_date,
        },
        "links": {
            "cover_art": cover_art_url(view.release_group_mbid, base_url=cover_art_base_url),
            "plex": plex_link,
        },
    }


def channel_test_envelope(*, occurred_at: str) -> dict[str, Any]:
    """Build the envelope a ``channels test`` fire sends.

    Same shape as a release event with the parts it cannot have set to null,
    rather than a different, shorter document: a consumer's parser should be
    exercised by the test fire, not bypassed by it.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": TEST_EVENT_TYPE,
        "event_id": None,
        "occurred_at": occurred_at,
        "artist": {"name": None, "mbid": None},
        "release_group": {
            "title": None,
            "mbid": None,
            "primary_type": None,
            "secondary_types": [],
            "first_release_date": None,
        },
        "links": {"cover_art": None, "plex": None},
    }


def _iso(moment: datetime) -> str:
    """Render UTC ISO-8601 with a ``Z``, from a datetime SQLite may hand back naive."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_body(envelope: dict[str, Any]) -> bytes:
    r"""Render the exact bytes that are sent, which are the bytes that are signed.

    Byte stability is the whole point. ``json.dumps`` with the default
    separators inserts a space after each ``,`` and ``:``, and preserves
    insertion order, so two builds that assembled the same envelope in a
    different order would sign different bytes and a consumer's verification
    would fail for no reason a user could see. Sorted keys and compact
    separators remove both variables.

    ``ensure_ascii=False`` keeps an artist's name as the UTF-8 the record holds
    rather than a pile of ``\\uXXXX`` escapes; the bytes are still deterministic
    because UTF-8 encoding is.
    """
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _signed_material(timestamp: int, body: bytes) -> bytes:
    return f"{timestamp}.".encode() + body


def sign(secret: str, body: bytes, *, timestamp: int) -> str:
    """Build the ``X-Encore-Signature`` value for one body at one moment.

    The timestamp is part of the signed material, not merely carried beside it,
    so a captured request cannot be replayed under a fresh timestamp.
    """
    digest = hmac.new(secret.encode("utf-8"), _signed_material(timestamp, body), sha256).hexdigest()
    return f"t={timestamp},{SIGNATURE_VERSION}={digest}"


def parse_signature(header: str) -> tuple[int, str] | None:
    """Split a signature header into ``(timestamp, hex digest)``, or None."""
    timestamp: int | None = None
    digest: str | None = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                return None
        elif key == SIGNATURE_VERSION:
            digest = value
    if timestamp is None or not digest:
        return None
    return timestamp, digest


def verify(
    secret: str,
    body: bytes,
    header: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Check that ``header`` is a valid signature over ``body`` under ``secret``.

    Published as part of this module rather than left to prose in
    ``docs/webhooks.md``, so the consumer-side check the documentation describes
    is the one this project's own tests run. Compared with
    :func:`hmac.compare_digest`, so the comparison does not leak the digest
    through its own timing.

    ``now`` is optional; when given, a signature older or newer than
    ``tolerance_seconds`` is rejected however well it verifies, which is what
    makes the timestamp worth signing.
    """
    parsed = parse_signature(header)
    if parsed is None:
        return False
    timestamp, digest = parsed
    if now is not None and abs(now - timestamp) > tolerance_seconds:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), _signed_material(timestamp, body), sha256
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


#: How a POST is actually made. A parameter so the delivery logic is testable
#: without a network and without patching a module global: a fake poster is
#: handed in, exactly as `NotificationSender` is handed to the delivery engine.
Poster = Callable[[str, bytes, dict[str, str]], int]


def _httpx_post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST ``body`` and return the status code. Never returns the response body."""
    import httpx

    # httpx logs full request URLs at INFO; a channel URL is a credential, so the
    # library's own logger is turned down here for the same reason the
    # MusicBrainz client turns it down (encore.matching.mb).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    response = httpx.post(url, content=body, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
    return int(response.status_code)


class WebhookSender:
    """Deliver one signed event envelope per notification.

    Implements the same shape as `encore.notify.sender.NotificationSender`, so
    the delivery engine's retry, backoff and channel-health bookkeeping apply
    unchanged: a webhook that 500s three times backs off and then goes terminal
    ``failed`` with its status recorded, exactly as a dead ntfy container does.

    One secret per instance, because the secret is per channel. The engine
    builds one of these for each webhook channel it is about to deliver to.
    """

    def __init__(
        self,
        secret: str,
        *,
        poster: Poster | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        """Hold one channel's secret, and the seams a test replaces."""
        self._secret = secret
        self._post = poster or _httpx_post
        self._clock = clock or _now_unix

    def send(self, url: str, notification: RenderedNotification) -> None:
        """POST the notification's events, one signed request each.

        A webhook is a machine consumer, so a rollup is not a kindness to it: it
        is one request whose failure would make several deliveries ambiguous.
        Each event is therefore its own request, and the first failure raises --
        the engine retries the whole batch, and a consumer deduplicates on
        ``event_id``, which is why that key is in the envelope.

        Raises:
            DeliveryError: the endpoint was unreachable or answered non-2xx.
        """
        for envelope in notification.envelopes:
            self._post_one(url, envelope)

    def _post_one(self, url: str, envelope: dict[str, Any]) -> None:
        body = canonical_body(envelope)
        timestamp = self._clock()
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(self._secret, body, timestamp=timestamp),
            TYPE_HEADER: str(envelope.get("event_type", "")),
            "User-Agent": "encore-webhook/1",
        }
        try:
            status = self._post(url, body, headers)
        except Exception as exc:
            # Broad on purpose and reported by type only: every transport failure
            # is, from here, one thing -- the event did not go out -- and an
            # httpx exception message can carry the URL, which is a credential.
            raise DeliveryError(
                f"the webhook endpoint could not be reached ({type(exc).__name__})"
            ) from exc
        if not 200 <= status < 300:
            # The status code and nothing else. The response body is the
            # operator's own endpoint talking, and it is free to echo the URL.
            raise DeliveryError(f"the webhook endpoint answered HTTP {status}")


def _now_unix() -> int:
    return int(datetime.now(UTC).timestamp())
