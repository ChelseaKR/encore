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

This scheduler drives *Plex* sync only. The MusicBrainz release poller (F3,
M2) is a separate scheduler with the global MetaBrainz token bucket; the
``/readyz`` scheduler-heartbeat check lands with it.
"""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

from encore.plex import PlexMusicClient
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage
from encore.sync import SyncError, sync_artists

__all__ = [
    "DEFAULT_SYNC_INTERVAL_HOURS",
    "SYNC_INTERVAL_ENV",
    "SYNC_JOB_ID",
    "build_sync_scheduler",
]

logger = logging.getLogger(__name__)

SYNC_INTERVAL_ENV = "ENCORE_SYNC_INTERVAL_HOURS"
DEFAULT_SYNC_INTERVAL_HOURS = 24.0
SYNC_JOB_ID = "plex-sync"


def _run_scheduled_sync(storage: Storage) -> None:
    """One scheduled sync run: re-read credentials, sync, log counts only."""
    credentials = storage.get_plex_credentials()
    if credentials is None:
        logger.warning("scheduled sync skipped: Plex credentials were removed")
        return
    base_url, token = credentials
    try:
        client = PlexMusicClient(base_url, token)
        sync_artists(storage, client)
    except SyncError as exc:
        logger.error("scheduled sync failed: %s", exc)


def _configured_interval_hours() -> float:
    """Read the sync interval from the environment (default: daily)."""
    raw = os.environ.get(SYNC_INTERVAL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_SYNC_INTERVAL_HOURS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "invalid %s=%r — falling back to %s hours",
            SYNC_INTERVAL_ENV,
            raw,
            DEFAULT_SYNC_INTERVAL_HOURS,
        )
        return DEFAULT_SYNC_INTERVAL_HOURS


def build_sync_scheduler(storage: Storage) -> BackgroundScheduler | None:
    """Start the background sync scheduler, or return ``None`` when it can't run.

    ``None`` cases (all logged): scheduling disabled via
    ``$ENCORE_SYNC_INTERVAL_HOURS <= 0``; no Plex credentials stored yet; the
    stored token can't be decrypted (restored DB without its key file —
    the server still boots so the operator can repair, docs/adr/0008).
    """
    interval_hours = _configured_interval_hours()
    if interval_hours <= 0:
        logger.info("sync scheduler disabled (%s=%s)", SYNC_INTERVAL_ENV, interval_hours)
        return None
    try:
        credentials = storage.get_plex_credentials()
    except SecretDecryptionError as exc:
        logger.error("sync scheduler not started: %s", exc)
        return None
    if credentials is None:
        logger.info("sync scheduler idle: no Plex credentials configured yet")
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
