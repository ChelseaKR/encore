"""The F4 delivery engine: fan out release events to notification channels.

One cycle does three things, in order:

1. **Materialize** the delivery obligations — one row per (event, channel)
   for enabled channels, skipping events older than the channel itself so
   adding a channel never replays history (`Storage.ensure_deliveries`).
2. **Instant channels:** send each due delivery as its own notification.
3. **Digest channels:** once ``digest_interval_hours`` has elapsed, roll every
   due delivery for that channel into a single message.

F10 layers per-artist priority over that cadence: an ``instant`` artist's
events break through digest windows on any channel, a ``digest`` artist's
wait for the window even on instant channels, and ``normal`` events follow
the channel's mode exactly as before. Muted artists never reach this
engine at all — `Storage.ensure_deliveries` settles their events without
creating deliveries (un-muting must not replay history).

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

import dataclasses
import datetime as dt
import logging
from dataclasses import dataclass

from encore.models import CHANNEL_KIND_WEBHOOK, Delivery, EventView, NotificationChannel
from encore.notify.render import RenderedNotification, render_digest, render_event, render_test
from encore.notify.sender import AppriseSender, DeliveryError, NotificationSender
from encore.notify.webhook import WebhookSender, channel_test_envelope, event_envelope
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
    muted_skipped: int = 0


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


def _with_envelopes(
    channel: NotificationChannel,
    notification: RenderedNotification,
    views: list[EventView],
    machine_identifier: str | None,
) -> RenderedNotification:
    """Attach the machine-readable events, for a webhook channel only.

    An Apprise channel gets exactly the object it always got, so nothing about
    the ~90 existing services changes shape when webhooks exist.
    """
    if channel.kind != CHANNEL_KIND_WEBHOOK:
        return notification
    return dataclasses.replace(
        notification,
        envelopes=tuple(
            event_envelope(view, machine_identifier=machine_identifier) for view in views
        ),
    )


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
            _with_envelopes(
                channel, render_event(view, machine_identifier), [view], machine_identifier
            ),
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
    views_in_batch = [view for _delivery, view in batch]
    _send(
        storage,
        channel,
        url,
        _with_envelopes(
            channel,
            render_digest(views_in_batch, machine_identifier),
            views_in_batch,
            machine_identifier,
        ),
        sender,
        [delivery for delivery, _view in batch],
        now,
        report,
        digest=True,
    )


def _deliver_rollup(
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
    """Send the held-back batch once its window opens, in this channel's shape.

    A webhook takes the same *timing* as any other channel -- an artist the
    operator marked ``digest`` waits for the window here too, because that
    preference is about when they want to be told, not about who is reading --
    but not the same *packaging*. A rollup is a kindness to a human inbox; to a
    machine it is one request whose failure would leave several deliveries
    ambiguous, and this project promises no duplicate deliveries. So the window
    gates, and then each event goes as its own signed, individually retriable
    request. The window is advanced explicitly afterwards, or a webhook channel
    would never hold anything back again.
    """
    if channel.kind != CHANNEL_KIND_WEBHOOK:
        _deliver_digest(storage, channel, url, sender, due, views, machine_identifier, now, report)
        return
    if not _digest_is_due(channel, now):
        return
    _deliver_instant(storage, channel, url, sender, due, views, machine_identifier, now, report)
    if channel.id is not None:
        storage.record_channel_result(channel.id, success=True, digest_sent_at=now)


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


def _partition_by_priority(
    storage: Storage,
    due: list[Delivery],
    views: dict[int, EventView],
) -> tuple[list[Delivery], list[Delivery], list[Delivery]]:
    """Split due deliveries by their artist's F10 priority tier.

    Returns ``(force_instant, force_digest, normal)``. An ``instant``
    artist breaks through digest windows on every channel; a ``digest``
    artist waits for the window even on instant channels; ``normal`` —
    including anything whose policy cannot be resolved, which must behave
    exactly as it did before F10 — follows the channel's own mode.
    """
    mbids = {view.artist_mbid for view in views.values()}
    policies = storage.effective_watch_settings_for_mbids(sorted(mbids)) if mbids else {}
    force_instant: list[Delivery] = []
    force_digest: list[Delivery] = []
    normal: list[Delivery] = []
    for delivery in due:
        view = views.get(delivery.event_id)
        policy = policies.get(view.artist_mbid) if view is not None else None
        tier = policy.priority if policy is not None else "normal"
        if tier == "instant":
            force_instant.append(delivery)
        elif tier == "digest":
            force_digest.append(delivery)
        else:
            normal.append(delivery)
    return force_instant, force_digest, normal


def sender_for(
    storage: Storage,
    channel: NotificationChannel,
    override: NotificationSender | None = None,
) -> NotificationSender | None:
    """Resolve the sender this channel's payload goes out through, or None.

    ``override`` wins for every channel. That is what a caller injecting a fake
    sender means, and it is what keeps a test able to drive a webhook channel
    without a network.

    Returns None, having logged, when a webhook channel has no usable signing
    secret. Sending it unsigned would be worse than not sending: a subscriber
    with no signature has no way to tell an Encore event from anything else that
    can reach the URL, and it would look like the feature working.
    """
    if override is not None:
        return override
    if channel.kind != CHANNEL_KIND_WEBHOOK:
        return AppriseSender()
    try:
        secret = storage.channel_secret(channel)
    except SecretDecryptionError:
        # Worded around the word "secret" on purpose. `.semgrep-rules/encore.yml`'s
        # no-sensitive-values-in-logs rule matches any argument whose text contains
        # it, message strings included, and the rule over-matching here is a much
        # better failure than it under-matching somewhere it counts. Rewording is
        # cheap; waiving a rule that guards credentials in logs is not.
        logger.error(
            "channel %r skipped: its stored signing key cannot be decrypted with "
            "the key beside the database (docs/adr/0008)",
            channel.name,
        )
        return None
    if not secret:
        logger.error(
            "channel %r skipped: it is a webhook channel with nothing to sign with, "
            "so every event it sent would be unsigned",
            channel.name,
        )
        return None
    return WebhookSender(secret)


def _run_channel(
    storage: Storage,
    channel: NotificationChannel,
    sender: NotificationSender | None,
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
    resolved = sender_for(storage, channel, sender)
    if resolved is None:
        report.channels_skipped += 1
        return []
    sender = resolved
    views = storage.event_views_for([delivery.event_id for delivery in due])
    force_instant, force_digest, normal = _partition_by_priority(storage, due, views)
    # The instant stream sends now regardless of mode: forced-instant
    # artists first, then everything else on an instant channel.
    instant_batch = force_instant + (normal if channel.mode == "instant" else [])
    if instant_batch:
        try:
            _deliver_instant(
                storage, channel, url, sender, instant_batch, views, machine_identifier, now, report
            )
        except Exception:
            logger.exception("channel %r raised during delivery; skipped this cycle", channel.name)
            report.channels_skipped += 1
    # The rollup stream waits for the digest window on any channel:
    # everything else on a digest channel, plus forced-digest artists.
    rollup_batch = (normal if channel.mode == "digest" else []) + force_digest
    if rollup_batch:
        try:
            _deliver_rollup(
                storage, channel, url, sender, rollup_batch, views, machine_identifier, now, report
            )
        except Exception:
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

    ``sender`` is no longer resolved once for the whole cycle, because an
    Apprise channel and a webhook channel put different things on the wire. It
    is resolved per channel by :func:`sender_for`; passing one here still
    overrides every channel, which is what an injected fake means.
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    created, muted_skipped = storage.ensure_deliveries(now)
    report = DeliveryReport(enqueued=created, muted_skipped=muted_skipped)
    machine_identifier = storage.get_plex_machine_identifier()
    touched_events: list[int] = []
    for channel in storage.list_channels(enabled_only=True):
        touched_events.extend(
            _run_channel(storage, channel, sender, machine_identifier, now, report)
        )
    report.events_settled = storage.settle_events(touched_events)
    logger.info(
        "delivery cycle: enqueued=%d sent=%d digests=%d retried=%d failed=%d "
        "channels_skipped=%d settled=%d muted_skipped=%d",
        report.enqueued,
        report.sent,
        report.digests_sent,
        report.retried,
        report.failed,
        report.channels_skipped,
        report.events_settled,
        report.muted_skipped,
    )
    return report


def _now_iso() -> str:
    """Return the moment a test fire happened, as the envelope's ``occurred_at``."""
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def send_test_notification(
    storage: Storage,
    channel_name: str,
    sender: NotificationSender | None = None,
) -> None:
    """Fire the test message at one channel (``encore channels test``, F6 wizard).

    Unlike a real delivery this is synchronous and loud: the caller wants to
    know *now* whether the channel works, so the failure propagates instead
    of being queued for retry. The result is still recorded on the channel.

    A webhook channel is test-fired with a real ``channel.test`` envelope, the
    same shape a release event has with the parts it cannot have set to null --
    so wiring up an endpoint exercises the subscriber's parser and its signature
    check, rather than bypassing both with a shorter document.

    Raises:
        StorageError: no channel with that name exists, or it is a webhook
            channel with no usable signing secret.
        DeliveryError: the channel could not be reached.
        SecretDecryptionError: the stored URL cannot be decrypted.
    """
    channel = storage.get_channel(channel_name)
    if channel is None:
        raise StorageError(f"no notification channel named {channel_name!r}")
    resolved = sender_for(storage, channel, sender)
    if resolved is None:
        raise StorageError(
            f"channel {channel_name!r} is a webhook channel with no usable signing "
            "secret, so a test fire would send an unsigned event"
        )
    sender = resolved
    url = storage.channel_url(channel)
    notification = render_test()
    if channel.kind == CHANNEL_KIND_WEBHOOK:
        notification = dataclasses.replace(
            notification,
            envelopes=(channel_test_envelope(occurred_at=_now_iso()),),
        )
    try:
        sender.send(url, notification)
    except DeliveryError as exc:
        if channel.id is not None:
            storage.record_channel_result(channel.id, success=False, error=str(exc))
        raise
    if channel.id is not None:
        storage.record_channel_result(channel.id, success=True)
