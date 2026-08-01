"""The F4 delivery engine: fan out release events to notification channels.

One cycle does three things, in order:

1. **Materialize** the delivery obligations — one row per (event, channel)
   for enabled channels, skipping events older than the channel itself so
   adding a channel never replays history (`Storage.ensure_deliveries`).
2. **Instant channels:** send each due delivery as its own notification.
3. **Digest channels:** once ``digest_interval_hours`` has elapsed, roll every
   due delivery for that channel into a single message.

Failure handling is the half of F4's acceptance that is easy to skip: a
failed channel **retries with exponential backoff** (`_backoff_seconds`) and,
after `MAX_ATTEMPTS`, the delivery goes terminal-``failed`` rather than
retrying forever. Either way the channel row records the failure and the
most recent error, so a dead webhook is visible in ``encore channels list``
instead of dying silently. One failing channel never blocks another: each
channel is handled independently, and an unexpected exception from a sender
is contained per channel — the skip-don't-queue posture F3 applies to
MusicBrainz, applied to notification services.

Privacy (no-outing lens): a notification body is nothing *but* taste data.
It is passed to the sender and never logged; this module's log lines carry
counts and channel *names* only — never a channel URL, an artist, a title,
or an MBID (docs/audits/dpia.md §4, OBS-11), and the channel name is the
operator's own label, chosen by them, not derived from library content.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from encore.models import Delivery, EventView, NotificationChannel
from encore.notify.render import RenderedNotification, render_digest, render_event, render_test
from encore.notify.sender import AppriseSender, DeliveryError, NotificationSender
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage, StorageError

__all__ = [
    "BASE_BACKOFF_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_SECONDS",
    "DeliveryReport",
    "run_delivery_cycle",
    "send_test_notification",
]

logger = logging.getLogger(__name__)

# Retry schedule: 5 min, 10, 20, 40, then give up. Five attempts spread over
# ~75 minutes rides out a restarting ntfy container or a brief DNS blip
# without hammering a service that is genuinely gone.
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 300.0
MAX_BACKOFF_SECONDS = 6 * 3600.0


@dataclass
class DeliveryReport:
    """Count what happened in one delivery cycle — all a log line may say."""

    enqueued: int = 0
    sent: int = 0
    digests_sent: int = 0
    retried: int = 0
    failed: int = 0
    channels_skipped: int = 0
    events_settled: int = 0


def _backoff_seconds(attempts: int) -> float:
    """Exponential backoff for the ``attempts``-th failure, bounded."""
    return min(BASE_BACKOFF_SECONDS * (2.0 ** max(0, attempts - 1)), MAX_BACKOFF_SECONDS)


def _digest_is_due(channel: NotificationChannel, now: dt.datetime) -> bool:
    """Whether a digest channel's interval has elapsed (first one is immediate)."""
    if channel.last_digest_at is None:
        return True
    last = channel.last_digest_at
    if last.tzinfo is None:  # SQLite round-trips naive datetimes
        last = last.replace(tzinfo=dt.UTC)
    return (now - last).total_seconds() >= channel.digest_interval_hours * 3600.0


def _record_failure(
    storage: Storage,
    channel: NotificationChannel,
    deliveries: list[Delivery],
    reason: str,
    now: dt.datetime,
    report: DeliveryReport,
) -> None:
    """Apply one failure to every delivery in the attempt, with backoff."""
    for delivery in deliveries:
        if delivery.id is None:  # pragma: no cover - persisted rows have one
            continue
        attempts = delivery.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            storage.update_delivery(delivery.id, "failed", attempts, last_error=reason)
            report.failed += 1
        else:
            next_attempt = now + dt.timedelta(seconds=_backoff_seconds(attempts))
            storage.update_delivery(
                delivery.id, "pending", attempts, next_attempt_at=next_attempt, last_error=reason
            )
            report.retried += 1
    if channel.id is not None:
        storage.record_channel_result(channel.id, success=False, error=reason)


def _send(
    storage: Storage,
    channel: NotificationChannel,
    url: str,
    notification: RenderedNotification,
    sender: NotificationSender,
    deliveries: list[Delivery],
    now: dt.datetime,
    report: DeliveryReport,
    digest: bool = False,
) -> bool:
    """Attempt one send; update the deliveries and the channel either way."""
    try:
        sender.send(url, notification)
    except DeliveryError as exc:
        _record_failure(storage, channel, deliveries, str(exc), now, report)
        return False
    for delivery in deliveries:
        if delivery.id is not None:
            storage.update_delivery(delivery.id, "delivered", delivery.attempts + 1)
    if channel.id is not None:
        storage.record_channel_result(
            channel.id, success=True, digest_sent_at=now if digest else None
        )
    if digest:
        report.digests_sent += 1
    else:
        report.sent += 1
    return True


def _deliver_instant(
    storage: Storage,
    channel: NotificationChannel,
    url: str,
    sender: NotificationSender,
    due: list[Delivery],
    views: dict[int, EventView],
    machine_identifier: str | None,
    now: dt.datetime,
    report: DeliveryReport,
) -> None:
    """One notification per due event, oldest first."""
    for delivery in due:
        view = views.get(delivery.event_id)
        if view is None:  # pragma: no cover - the event was deleted mid-cycle
            continue
        _send(
            storage,
            channel,
            url,
            render_event(view, machine_identifier),
            sender,
            [delivery],
            now,
            report,
        )


def _deliver_digest(
    storage: Storage,
    channel: NotificationChannel,
    url: str,
    sender: NotificationSender,
    due: list[Delivery],
    views: dict[int, EventView],
    machine_identifier: str | None,
    now: dt.datetime,
    report: DeliveryReport,
) -> None:
    """One rollup message covering every due event for this channel."""
    if not _digest_is_due(channel, now):
        return
    batch = [(d, views[d.event_id]) for d in due if d.event_id in views]
    if not batch:
        return
    _send(
        storage,
        channel,
        url,
        render_digest([view for _delivery, view in batch], machine_identifier),
        sender,
        [delivery for delivery, _view in batch],
        now,
        report,
        digest=True,
    )


def _channel_url(storage: Storage, channel: NotificationChannel) -> str | None:
    """Decrypt a channel's URL, or ``None`` (logged, counted) if the key is wrong."""
    try:
        return storage.channel_url(channel)
    except SecretDecryptionError:
        logger.error(
            "channel %r skipped: its stored URL cannot be decrypted with the key "
            "beside the database (docs/adr/0008)",
            channel.name,
        )
        return None


def _run_channel(
    storage: Storage,
    channel: NotificationChannel,
    sender: NotificationSender,
    machine_identifier: str | None,
    now: dt.datetime,
    report: DeliveryReport,
) -> list[int]:
    """Deliver one channel's due work; return the event ids it touched."""
    if channel.id is None:  # pragma: no cover - persisted rows have one
        return []
    due = storage.due_deliveries(channel.id, now)
    if not due:
        return []
    url = _channel_url(storage, channel)
    if url is None:
        report.channels_skipped += 1
        return []
    views = storage.event_views_for([delivery.event_id for delivery in due])
    deliver = _deliver_digest if channel.mode == "digest" else _deliver_instant
    try:
        deliver(storage, channel, url, sender, due, views, machine_identifier, now, report)
    except Exception:
        # A sender that raises something other than DeliveryError is a bug in
        # that sender, not a reason to abandon every other channel this cycle.
        logger.exception("channel %r raised during delivery; skipped this cycle", channel.name)
        report.channels_skipped += 1
    return [delivery.event_id for delivery in due]


def run_delivery_cycle(
    storage: Storage,
    sender: NotificationSender | None = None,
    now: dt.datetime | None = None,
) -> DeliveryReport:
    """Run one full delivery cycle across every enabled channel.

    Never raises for a per-channel problem: an undecryptable URL, a dead
    service, or an unexpected sender exception is recorded against that
    channel and the cycle continues.
    """
    if sender is None:
        sender = AppriseSender()
    if now is None:
        now = dt.datetime.now(dt.UTC)
    report = DeliveryReport(enqueued=storage.ensure_deliveries(now))
    machine_identifier = storage.get_plex_machine_identifier()
    touched_events: list[int] = []
    for channel in storage.list_channels(enabled_only=True):
        touched_events.extend(
            _run_channel(storage, channel, sender, machine_identifier, now, report)
        )
    report.events_settled = storage.settle_events(touched_events)
    logger.info(
        "delivery cycle: enqueued=%d sent=%d digests=%d retried=%d failed=%d "
        "channels_skipped=%d settled=%d",
        report.enqueued,
        report.sent,
        report.digests_sent,
        report.retried,
        report.failed,
        report.channels_skipped,
        report.events_settled,
    )
    return report


def send_test_notification(
    storage: Storage,
    channel_name: str,
    sender: NotificationSender | None = None,
) -> None:
    """Fire the test message at one channel (``encore channels test``, F6 wizard).

    Unlike a real delivery this is synchronous and loud: the caller wants to
    know *now* whether the channel works, so the failure propagates instead
    of being queued for retry. The result is still recorded on the channel.

    Raises:
        StorageError: no channel with that name exists.
        DeliveryError: the channel could not be reached.
        SecretDecryptionError: the stored URL cannot be decrypted.
    """
    if sender is None:
        sender = AppriseSender()
    channel = storage.get_channel(channel_name)
    if channel is None:
        raise StorageError(f"no notification channel named {channel_name!r}")
    url = storage.channel_url(channel)
    try:
        sender.send(url, render_test())
    except DeliveryError as exc:
        if channel.id is not None:
            storage.record_channel_result(channel.id, success=False, error=str(exc))
        raise
    if channel.id is not None:
        storage.record_channel_result(channel.id, success=True)
