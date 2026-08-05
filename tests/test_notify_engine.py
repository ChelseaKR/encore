"""F4 delivery-engine tests: fan-out, cadences, backoff, and failure containment."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from encore.models import Delivery, NotificationChannel
from encore.notify import DeliveryError, run_delivery_cycle, send_test_notification
from encore.notify.engine import BASE_BACKOFF_SECONDS, MAX_ATTEMPTS
from encore.notify.render import RenderedNotification
from encore.storage import Storage, StorageError
from tests.notify_fixtures import (
    ARTIST_NAME,
    CHANNEL_URL,
    GROUP_MBID,
    RELEASE_TITLE,
    RecordingSender,
    seed_event,
)

# A fixed test clock, deliberately ahead of wall-clock time: rows are created
# with a real ``utcnow()`` default, and every assertion here is about what the
# cycle does at a *given* moment, so the moment has to be after row creation.
NOW = dt.datetime(2099, 1, 1, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _deliveries(storage: Storage) -> list[Delivery]:
    with storage.session() as session:
        from sqlmodel import select

        return list(session.exec(select(Delivery)).all())


def _channel(storage: Storage, name: str = "phone") -> NotificationChannel:
    row = storage.get_channel(name)
    assert row is not None
    return row


def test_instant_channel_receives_one_notification_per_event(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    sender = RecordingSender()

    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.enqueued == 1
    assert report.sent == 1
    assert len(sender.calls) == 1
    url, notification = sender.calls[0]
    assert url == CHANNEL_URL
    assert RELEASE_TITLE in notification.title


def test_an_event_is_never_delivered_twice(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    sender = RecordingSender()

    run_delivery_cycle(storage, sender, now=NOW)
    second = run_delivery_cycle(storage, sender, now=NOW + dt.timedelta(hours=1))

    assert len(sender.calls) == 1
    assert second.sent == 0


def test_a_settled_event_records_that_encore_finished_with_it(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)

    report = run_delivery_cycle(storage, RecordingSender(), now=NOW)

    assert report.events_settled == 1
    assert storage.list_events()[0].notified_at is not None


def test_adding_a_channel_does_not_replay_history(storage: Storage) -> None:
    # docs/adr/0012: a new destination's first act must not be to deliver the
    # entire back catalog — the baseline rule applied to channels.
    seed_event(storage)
    storage.add_channel("phone", CHANNEL_URL)
    sender = RecordingSender()

    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.enqueued == 0
    assert sender.calls == []
    # …and the pre-existing event stays honestly unnotified.
    assert storage.list_events()[0].notified_at is None


def test_multiple_channels_each_get_their_own_delivery(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    storage.add_channel("desktop", "discord://id/token-needle")
    seed_event(storage)
    sender = RecordingSender()

    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.enqueued == 2
    assert report.sent == 2
    assert {url for url, _notification in sender.calls} == {
        CHANNEL_URL,
        "discord://id/token-needle",
    }


def test_disabled_channels_are_skipped_without_losing_their_history(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    storage.set_channel_enabled("phone", False)
    seed_event(storage)
    sender = RecordingSender()

    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.enqueued == 0
    assert sender.calls == []
    assert _channel(storage).enabled is False


def test_digest_channel_rolls_up_and_respects_its_cadence(storage: Storage) -> None:
    storage.add_channel("email", CHANNEL_URL, mode="digest", digest_interval_hours=24.0)
    seed_event(storage, title="First album", group_mbid=GROUP_MBID, rating_key="1")
    seed_event(
        storage,
        title="Second album",
        artist_name="Other Artist",
        artist_mbid="99999999-2222-3333-4444-555555555555",
        group_mbid="ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
        rating_key="2",
    )
    sender = RecordingSender()

    first = run_delivery_cycle(storage, sender, now=NOW)
    assert first.digests_sent == 1
    assert len(sender.calls) == 1
    assert "First album" in sender.calls[0][1].body
    assert "Second album" in sender.calls[0][1].body

    # A new event an hour later waits for the next digest window.
    seed_event(
        storage,
        title="Third album",
        artist_name="Third Artist",
        artist_mbid="88888888-2222-3333-4444-555555555555",
        group_mbid="dddddddd-bbbb-cccc-dddd-eeeeeeeeeeee",
        rating_key="3",
    )
    held = run_delivery_cycle(storage, sender, now=NOW + dt.timedelta(hours=1))
    assert held.digests_sent == 0
    assert len(sender.calls) == 1

    released = run_delivery_cycle(storage, sender, now=NOW + dt.timedelta(hours=25))
    assert released.digests_sent == 1
    # A digest of exactly one renders as a plain notification (docs/adr/0012),
    # so the release names the subject line rather than a rollup body.
    assert "Third album" in sender.calls[1][1].title


def test_a_failed_delivery_retries_with_exponential_backoff(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    sender = RecordingSender(fail_with="the notification service rejected the message")

    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.retried == 1
    assert report.failed == 0
    delivery = _deliveries(storage)[0]
    assert delivery.status == "pending"
    assert delivery.attempts == 1
    # The next attempt is one base backoff away, not immediate.
    assert delivery.next_attempt_at.replace(tzinfo=dt.UTC) == NOW + dt.timedelta(
        seconds=BASE_BACKOFF_SECONDS
    )

    # A cycle inside the backoff window does not retry.
    quiet = run_delivery_cycle(storage, sender, now=NOW + dt.timedelta(seconds=60))
    assert quiet.retried == 0
    assert len(sender.calls) == 1


def test_retries_are_bounded_and_the_failure_surfaces_on_the_channel(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    sender = RecordingSender(fail_with="connection refused")

    now = NOW
    for _attempt in range(MAX_ATTEMPTS):
        run_delivery_cycle(storage, sender, now=now)
        now += dt.timedelta(days=1)  # always past the backoff

    assert len(sender.calls) == MAX_ATTEMPTS
    delivery = _deliveries(storage)[0]
    assert delivery.status == "failed"
    assert delivery.attempts == MAX_ATTEMPTS

    channel = _channel(storage)
    assert channel.consecutive_failures == MAX_ATTEMPTS
    assert channel.last_error == "connection refused"
    assert channel.last_success_at is None

    # Terminal means terminal: the next cycle does not try again.
    after = run_delivery_cycle(storage, sender, now=now)
    assert after.retried == 0
    assert len(sender.calls) == MAX_ATTEMPTS


def test_a_recovering_channel_clears_its_failure_state(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    run_delivery_cycle(storage, RecordingSender(fail_with="timed out"), now=NOW)
    assert _channel(storage).consecutive_failures == 1

    run_delivery_cycle(storage, RecordingSender(), now=NOW + dt.timedelta(hours=1))

    channel = _channel(storage)
    assert channel.consecutive_failures == 0
    assert channel.last_error is None
    assert channel.last_success_at is not None


def test_one_broken_channel_does_not_stop_the_others(storage: Storage) -> None:
    storage.add_channel("broken", CHANNEL_URL)
    storage.add_channel("working", "discord://id/token-needle")
    seed_event(storage)

    class SelectiveSender(RecordingSender):
        def send(self, url: str, notification: RenderedNotification) -> None:
            self.calls.append((url, notification))
            if url == CHANNEL_URL:
                raise DeliveryError("gone")

    sender = SelectiveSender()
    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.sent == 1
    assert report.retried == 1
    assert len(sender.calls) == 2


def test_an_unexpected_sender_exception_is_contained_to_its_channel(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)

    report = run_delivery_cycle(storage, RecordingSender(raise_unexpected=True), now=NOW)

    assert report.channels_skipped == 1
    assert report.sent == 0
    # The delivery is still pending — a crash is not a delivery.
    assert _deliveries(storage)[0].status == "pending"


def test_a_channel_whose_url_cannot_be_decrypted_is_skipped_not_crashed(
    tmp_path: Path,
) -> None:
    # A data directory restored without its key file (docs/adr/0008): every
    # channel is unreadable, and the cycle must survive it.
    from encore.storage import KEY_FILENAME

    original = Storage(tmp_path / "data")
    original.add_channel("phone", CHANNEL_URL)
    seed_event(original)
    original.close()

    (tmp_path / "data" / KEY_FILENAME).unlink()
    replacement = Storage(tmp_path / "data2")
    replacement.close()
    (tmp_path / "data2" / KEY_FILENAME).replace(tmp_path / "data" / KEY_FILENAME)

    storage = Storage(tmp_path / "data")
    sender = RecordingSender()
    report = run_delivery_cycle(storage, sender, now=NOW)

    assert report.channels_skipped == 1
    assert sender.calls == []
    storage.close()


def test_removing_a_channel_takes_its_deliveries_with_it(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    seed_event(storage)
    run_delivery_cycle(storage, RecordingSender(fail_with="nope"), now=NOW)
    assert _deliveries(storage)

    storage.remove_channel("phone")

    assert _deliveries(storage) == []
    assert storage.get_channel("phone") is None


def test_test_notification_reports_success_and_failure_on_the_channel(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)

    send_test_notification(storage, "phone", RecordingSender())
    assert _channel(storage).last_success_at is not None

    with pytest.raises(DeliveryError):
        send_test_notification(storage, "phone", RecordingSender(fail_with="bad token"))
    assert _channel(storage).last_error == "bad token"

    with pytest.raises(StorageError):
        send_test_notification(storage, "nonexistent", RecordingSender())


def test_a_cycle_with_no_channels_is_a_free_no_op(storage: Storage) -> None:
    seed_event(storage)
    report = run_delivery_cycle(storage, RecordingSender(), now=NOW)
    assert report == type(report)()


@pytest.mark.no_outing
@pytest.mark.no_secrets_in_logs
def test_delivery_logs_never_carry_taste_data_or_channel_urls(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    # The sentinel tripwire extended to F4's egress surface: a notification
    # body is nothing but taste data, and an Apprise URL is a credential.
    storage.add_channel("phone", CHANNEL_URL)
    storage.add_channel("broken", "discord://id/other-needle")
    seed_event(storage)

    with caplog.at_level(logging.DEBUG):
        run_delivery_cycle(storage, RecordingSender(fail_with="service said no"), now=NOW)
        run_delivery_cycle(storage, RecordingSender(raise_unexpected=True), now=NOW)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for needle in (ARTIST_NAME, RELEASE_TITLE, GROUP_MBID, CHANNEL_URL, "other-needle"):
        assert needle not in logged
    assert "delivery cycle:" in logged
