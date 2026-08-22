"""F1 — the background sync scheduler (daily by default, off without creds).

APScheduler (encore-plans/04 stack) runs the library sync on an interval.
Deliberately conservative at M1:

- **No credentials, no scheduler.** Until `encore plex configure` has stored
  a base URL + token, there is nothing to sync and no thread is started.
- **Interval** comes from ``$ENCORE_SYNC_INTERVAL_HOURS`` (default 24, the
  plan's "daily"); ``0`` (or negative) disables scheduling entirely. A
  settings-table cadence replaces the env var with the F6 wizard (M2).
- **First run is one interval away**, not at boot — a restart loop must not
  hammer the Plex server (the same skip-don't-queue posture F3 will apply
  to MusicBrainz).

Five independent schedulers live here now: the Plex sync (F1, needs
credentials), the MusicBrainz identity matcher (F2, keyless), the
MusicBrainz release watcher (F3, keyless), the notification delivery
cycle (F4, minutes rather than hours, since its queue is local), and the
weekly recommendation refresh (F7, ListenBrainz labs). All five share the
conservative posture: first run one interval away, coalesce after
downtime, never a backlog. The match and watch schedulers run the *same*
passes as `encore match` / `encore watch` (`run_matching_pass` /
`watch_all_artists`), so the manual and automatic paths cannot drift.
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from encore.matching.engine import run_matching_pass
from encore.matching.mb import MusicBrainzClient
from encore.notify import run_delivery_cycle
from encore.plex import PlexMusicClient
from encore.recommend.engine import refresh_recommendations
from encore.recommend.lb import ListenBrainzClient
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage
from encore.sync import SyncError, sync_artists
from encore.watch import watch_all_artists

__all__ = [
    "DEFAULT_MATCH_INTERVAL_HOURS",
    "DEFAULT_NOTIFY_INTERVAL_MINUTES",
    "DEFAULT_REC_INTERVAL_HOURS",
    "DEFAULT_SYNC_INTERVAL_HOURS",
    "DEFAULT_WATCH_INTERVAL_HOURS",
    "MATCH_INTERVAL_ENV",
    "MATCH_JOB_ID",
    "NOTIFY_INTERVAL_ENV",
    "NOTIFY_JOB_ID",
    "REC_INTERVAL_ENV",
    "REC_JOB_ID",
    "SYNC_INTERVAL_ENV",
    "SYNC_JOB_ID",
    "WATCH_INTERVAL_ENV",
    "WATCH_JOB_ID",
    "build_match_scheduler",
    "build_notify_scheduler",
    "build_rec_scheduler",
    "build_sync_scheduler",
    "build_watch_scheduler",
]

logger = logging.getLogger(__name__)

SYNC_INTERVAL_ENV = "ENCORE_SYNC_INTERVAL_HOURS"
DEFAULT_SYNC_INTERVAL_HOURS = 24.0
SYNC_JOB_ID = "plex-sync"

WATCH_INTERVAL_ENV = "ENCORE_WATCH_INTERVAL_HOURS"
DEFAULT_WATCH_INTERVAL_HOURS = 24.0
WATCH_JOB_ID = "mb-watch"

NOTIFY_INTERVAL_ENV = "ENCORE_NOTIFY_INTERVAL_MINUTES"
DEFAULT_NOTIFY_INTERVAL_MINUTES = 15.0
NOTIFY_JOB_ID = "notify-deliver"

# F2's scheduled twin of `encore match`: a freshly synced library reaches
# the watch cycle without anyone remembering to run a command. Same daily
# default as sync/watch — matching only queries for artists that have no
# decision yet, so a steady-state install costs zero MusicBrainz requests.
MATCH_INTERVAL_ENV = "ENCORE_MATCH_INTERVAL_HOURS"
DEFAULT_MATCH_INTERVAL_HOURS = 24.0
MATCH_JOB_ID = "mb-match"

# F7's weekly recommendation refresh: ListenBrainz labs is a slow-moving,
# donation-funded dataset and similarity barely changes day to day. A
# refresh with zero watched artists is a free no-op.
REC_INTERVAL_ENV = "ENCORE_REC_INTERVAL_HOURS"
DEFAULT_REC_INTERVAL_HOURS = 168.0
REC_JOB_ID = "lb-recommend"


def _run_scheduled_sync(storage: Storage) -> None:
    """One scheduled sync run: re-read credentials, sync, log counts only."""
    credentials = storage.get_plex_credentials()
    if credentials is None:
        logger.warning("scheduled sync skipped: the stored Plex connection was removed")
        return
    base_url, token = credentials
    try:
        client = PlexMusicClient(base_url, token)
        sync_artists(storage, client)
    except SyncError as exc:
        logger.error("scheduled sync failed: %s", exc)


def _configured_interval(env_var: str, default: float) -> float:
    """Read a scheduler interval from the environment (unit is the caller's)."""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r — falling back to %s", env_var, raw, default)
        return default


def build_sync_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the background sync scheduler, or return ``None`` when it can't run.

    ``None`` cases (all logged): scheduling disabled via
    ``$ENCORE_SYNC_INTERVAL_HOURS <= 0``; no Plex credentials stored yet; the
    stored token can't be decrypted (restored DB without its key file —
    the server still boots so the operator can repair, docs/adr/0008).
    """
    interval_hours = _configured_interval(SYNC_INTERVAL_ENV, DEFAULT_SYNC_INTERVAL_HOURS)
    if interval_hours <= 0:
        logger.info("sync scheduler disabled (%s=%s)", SYNC_INTERVAL_ENV, interval_hours)
        return None
    try:
        credentials = storage.get_plex_credentials()
    except SecretDecryptionError as exc:
        logger.error("sync scheduler not started: %s", exc)
        return None
    if credentials is None:
        logger.info("sync scheduler idle: Plex connection not configured yet")
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_sync,
        "interval",
        args=[storage],
        hours=interval_hours,
        id=SYNC_JOB_ID,
        coalesce=True,  # after downtime, run once — never queue a backlog
        max_instances=1,
    )
    scheduler.start()
    logger.info("sync scheduler started: every %s hours", interval_hours)
    return scheduler


def _run_scheduled_watch(storage: Storage) -> None:
    """One scheduled watch cycle: fresh MB client, poll, log counts only.

    A run with zero matched artists is a free no-op (no requests leave the
    host), so the scheduler can start before matching has happened — newly
    matched artists are picked up on the next cycle without a restart.
    """
    client = MusicBrainzClient()
    try:
        watch_all_artists(storage, client)
    finally:
        client.close()


def build_watch_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the release-watch scheduler (F3), or ``None`` when disabled.

    Unlike the Plex sync scheduler there is no credential gate — MusicBrainz
    is keyless — so the only ``None`` case is disabling via
    ``$ENCORE_WATCH_INTERVAL_HOURS <= 0``. The first cycle is one interval
    away and ``coalesce`` collapses any downtime backlog to a single run:
    the skip-don't-queue posture MetaBrainz politeness requires (risk R8).
    """
    interval_hours = _configured_interval(WATCH_INTERVAL_ENV, DEFAULT_WATCH_INTERVAL_HOURS)
    if interval_hours <= 0:
        logger.info("watch scheduler disabled (%s=%s)", WATCH_INTERVAL_ENV, interval_hours)
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_watch,
        "interval",
        args=[storage],
        hours=interval_hours,
        id=WATCH_JOB_ID,
        coalesce=True,  # after downtime, run once — never queue a backlog
        max_instances=1,
    )
    scheduler.start()
    logger.info("watch scheduler started: every %s hours", interval_hours)
    return scheduler


def _run_scheduled_delivery(storage: Storage) -> None:
    """One scheduled delivery cycle (F4). Never raises — see the engine."""
    run_delivery_cycle(storage)


def build_notify_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the notification delivery scheduler (F4), or ``None`` when disabled.

    Minutes, not hours: the watch cycle is a daily poll of a slow-moving
    upstream, but once an event *exists* the user is waiting for it. Fifteen
    minutes is the default compromise between "instant" meaning something and
    a cycle that costs nothing when there is no work (a cycle with no due
    deliveries makes no outbound request at all). ``<= 0`` disables it.

    Unlike the sync scheduler there is no credential gate — a fresh install
    with no channels simply delivers nothing, and picks up the first channel
    on the next cycle without a restart.
    """
    interval_minutes = _configured_interval(NOTIFY_INTERVAL_ENV, DEFAULT_NOTIFY_INTERVAL_MINUTES)
    if interval_minutes <= 0:
        logger.info("notify scheduler disabled (%s=%s)", NOTIFY_INTERVAL_ENV, interval_minutes)
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_delivery,
        "interval",
        args=[storage],
        minutes=interval_minutes,
        id=NOTIFY_JOB_ID,
        coalesce=True,  # after downtime, run once — the queue is in the DB
        max_instances=1,
    )
    scheduler.start()
    logger.info("notify scheduler started: every %s minutes", interval_minutes)
    return scheduler


def _run_scheduled_match(storage: Storage) -> None:
    """One scheduled matching pass: fresh MB client, backlog, close (F2).

    Runs the same `run_matching_pass` as `encore match`, so a freshly synced
    artist reaches an identity decision — and then the watch cycle's next
    poll list — without anyone remembering to run a command. A run with zero
    unmatched artists is a free no-op: `Storage.list_unmatched_artists`
    excludes every artist that already has a decision, so steady state costs
    no MusicBrainz requests at all.
    """
    client = MusicBrainzClient()
    try:
        run_matching_pass(storage, client)
    finally:
        client.close()


def build_match_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the identity-matching scheduler (F2), or ``None`` when disabled.

    No credential gate — MusicBrainz is keyless and the backlog is empty on
    a fresh install — so the only ``None`` case is disabling via
    ``$ENCORE_MATCH_INTERVAL_HOURS <= 0``. Same conservative posture as its
    siblings: first run one interval away, ``coalesce`` collapses downtime
    to one run, and per-artist failures are skipped inside the pass (risk
    R8), retried naturally by the next cycle.
    """
    interval_hours = _configured_interval(MATCH_INTERVAL_ENV, DEFAULT_MATCH_INTERVAL_HOURS)
    if interval_hours <= 0:
        logger.info("match scheduler disabled (%s=%s)", MATCH_INTERVAL_ENV, interval_hours)
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_match,
        "interval",
        args=[storage],
        hours=interval_hours,
        id=MATCH_JOB_ID,
        coalesce=True,  # after downtime, run once — never queue a backlog
        max_instances=1,
    )
    scheduler.start()
    logger.info("match scheduler started: every %s hours", interval_hours)
    return scheduler


def _run_scheduled_recommend(storage: Storage) -> None:
    """One scheduled recommendation refresh: fresh LB client, close (F7)."""
    client = ListenBrainzClient()
    try:
        refresh_recommendations(storage, client)
    finally:
        client.close()


def build_rec_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the weekly recommendation-refresh scheduler (F7), or ``None``.

    No credential gate — the labs API is keyless and a refresh over zero
    watched artists costs nothing — so the only ``None`` case is disabling
    via ``$ENCORE_REC_INTERVAL_HOURS <= 0``. Same conservative posture as
    its siblings: first run one interval away, ``coalesce`` after downtime,
    per-batch failures skipped inside the refresh.
    """
    interval_hours = _configured_interval(REC_INTERVAL_ENV, DEFAULT_REC_INTERVAL_HOURS)
    if interval_hours <= 0:
        logger.info("recommend scheduler disabled (%s=%s)", REC_INTERVAL_ENV, interval_hours)
        return None
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        _run_scheduled_recommend,
        "interval",
        args=[storage],
        hours=interval_hours,
        id=REC_JOB_ID,
        coalesce=True,  # after downtime, run once — never queue a backlog
        max_instances=1,
    )
    scheduler.start()
    logger.info("recommend scheduler started: every %s hours", interval_hours)
    return scheduler
