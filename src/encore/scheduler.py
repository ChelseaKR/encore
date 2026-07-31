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

Two independent schedulers live here since F3: the Plex sync (F1, needs
credentials) and the MusicBrainz release watcher (F3, keyless — it polls
whatever artists are matched, through the process-global MetaBrainz rate
limiter in `encore.matching.mb`). Both share the conservative posture:
first run one interval away, coalesce after downtime, never a backlog.
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from encore.matching.mb import MusicBrainzClient
from encore.plex import PlexMusicClient
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage
from encore.sync import SyncError, sync_artists
from encore.watch import watch_all_artists

__all__ = [
    "DEFAULT_SYNC_INTERVAL_HOURS",
    "DEFAULT_WATCH_INTERVAL_HOURS",
    "SYNC_INTERVAL_ENV",
    "SYNC_JOB_ID",
    "WATCH_INTERVAL_ENV",
    "WATCH_JOB_ID",
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


def _configured_interval_hours(env_var: str, default: float) -> float:
    """Read a scheduler interval from the environment (default: daily)."""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid %s=%r — falling back to %s hours", env_var, raw, default)
        return default


def build_sync_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the background sync scheduler, or return ``None`` when it can't run.

    ``None`` cases (all logged): scheduling disabled via
    ``$ENCORE_SYNC_INTERVAL_HOURS <= 0``; no Plex credentials stored yet; the
    stored token can't be decrypted (restored DB without its key file —
    the server still boots so the operator can repair, docs/adr/0008).
    """
    interval_hours = _configured_interval_hours(SYNC_INTERVAL_ENV, DEFAULT_SYNC_INTERVAL_HOURS)
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
    interval_hours = _configured_interval_hours(WATCH_INTERVAL_ENV, DEFAULT_WATCH_INTERVAL_HOURS)
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
