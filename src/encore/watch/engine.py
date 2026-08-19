"""The F3 diff engine: browse → compare against stored state → emit events.

Semantics (docs/adr/0001 + docs/adr/0011):

- **Release-group level.** Reissues and edition-adds of an already-seen group
  change nothing we compare on, so they can never re-alert (F3 acceptance).
- **Baseline seeding.** The first poll of an artist inventories the whole
  back catalog *silently* — no ``new`` events for decades-old albums, which
  would otherwise flood F4 on day one. Future-dated groups are the exception:
  they are news even at baseline, and become ``upcoming`` events.
- **After baseline:** an unseen group becomes a ``new`` event (or ``upcoming``
  when future-dated); a changed first-release date on a seen group becomes a
  ``date_changed`` event.
- **Skip, don't queue.** A per-artist failure — MusicBrainz *or* storage —
  is counted and the poll moves on: one bad artist (or a MetaBrainz outage
  mid-run) must not wedge the whole cycle, and the next scheduled run
  retries naturally.

Privacy (no-outing lens): artist MBIDs, titles, and dates are taste data —
log lines here carry only counts and kinds, never identifiers. The only
outbound flow is the disclosed MetaBrainz browse (dpia.md §3), through the
shared 1 req/s limiter in `encore.matching.mb`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from encore.matching.mb import MusicBrainzClient, MusicBrainzError, ReleaseGroupInfo
from encore.storage import Storage, StorageError

__all__ = [
    "ArtistWatchResult",
    "WatchReport",
    "parse_earliest_date",
    "watch_all_artists",
    "watch_artist",
]

logger = logging.getLogger(__name__)


def parse_earliest_date(partial: str) -> dt.date | None:
    """Resolve an MB partial date to its earliest possible day, or ``None``.

    ``"2026"`` → 2026-01-01, ``"2026-09"`` → 2026-09-01, full dates parse
    as-is; empty or malformed text → ``None``. Earliest-possible is the
    conservative reading for "is this in the future?": a bare year is only
    ``upcoming`` until that year starts, never for the whole year.
    """
    if not partial:
        return None
    parts = partial.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return dt.date(year, month, day)
    except ValueError:
        return None


def _kind_for_unseen(info: ReleaseGroupInfo, today: dt.date) -> str:
    """``upcoming`` for a strictly future-dated group, else ``new``."""
    earliest = parse_earliest_date(info.first_release_date)
    if earliest is not None and earliest > today:
        return "upcoming"
    return "new"


def _row_id(row_id: int | None) -> int:
    """Narrow a persisted row's primary key for mypy (always set post-refresh)."""
    if row_id is None:
        raise StorageError("persisted release-group row has no primary key")
    return row_id


@dataclass
class ArtistWatchResult:
    """Counts from polling one artist (identifiers deliberately absent)."""

    baselined: bool = False
    groups_seen: int = 0
    events_new: int = 0
    events_upcoming: int = 0
    events_date_changed: int = 0


@dataclass
class WatchReport:
    """Counts from one full watch cycle across all watched artists."""

    artists_polled: int = 0
    artists_failed: int = 0
    artists_baselined: int = 0
    groups_seen: int = 0
    events_new: int = 0
    events_upcoming: int = 0
    events_date_changed: int = 0

    def add(self, result: ArtistWatchResult) -> None:
        """Fold one artist's counts into the cycle totals."""
        self.artists_polled += 1
        if result.baselined:
            self.artists_baselined += 1
        self.groups_seen += result.groups_seen
        self.events_new += result.events_new
        self.events_upcoming += result.events_upcoming
        self.events_date_changed += result.events_date_changed


def watch_artist(
    storage: Storage,
    client: MusicBrainzClient,
    artist_mbid: str,
    today: dt.date | None = None,
) -> ArtistWatchResult:
    """Poll one artist's release-groups and record what changed.

    Raises:
        MusicBrainzError: the browse failed after the client's bounded
            retries (callers decide whether to skip or surface it).
    """
    if today is None:
        today = dt.datetime.now(dt.UTC).date()
    result = ArtistWatchResult(baselined=not storage.has_release_groups(artist_mbid))
    fetched = client.browse_release_groups(artist_mbid)
    result.groups_seen = len(fetched)
    known = {row.mbid: row for row in storage.list_release_groups(artist_mbid)}
    for info in fetched:
        seen = known.get(info.mbid)
        if seen is None:
            row = storage.add_release_group(
                artist_mbid=artist_mbid,
                mbid=info.mbid,
                title=info.title,
                primary_type=info.primary_type,
                secondary_types=info.secondary_types,
                first_release_date=info.first_release_date,
            )
            kind = _kind_for_unseen(info, today)
            if kind == "upcoming":
                # Announcements are news even at baseline — they go to the
                # calendar/feeds (F5), not into the silent back catalog.
                storage.add_event(_row_id(row.id), "upcoming")
                result.events_upcoming += 1
            elif not result.baselined:
                storage.add_event(_row_id(row.id), "new")
                result.events_new += 1
        elif seen.first_release_date != info.first_release_date:
            storage.update_release_group_date(artist_mbid, info.mbid, info.first_release_date)
            storage.add_event(_row_id(seen.id), "date_changed")
            result.events_date_changed += 1
    return result


def watch_all_artists(storage: Storage, client: MusicBrainzClient) -> WatchReport:
    """One watch cycle: poll every watched artist, skip-don't-queue on failure.

    Sequential under the shared 1 req/s limiter — a 1,000-artist library is
    roughly 17 minutes of polite polling per cycle (encore-plans/04 §API
    budget). Per-artist failures are counted and skipped; the next scheduled
    cycle retries them without any backlog queue (risk R8).
    """
    report = WatchReport()
    for artist_mbid in storage.list_watched_artist_mbids():
        try:
            result = watch_artist(storage, client, artist_mbid)
        except (MusicBrainzError, StorageError):
            # Counts only — no MBID, no artist name (no-outing lens).
            # StorageError gets the same skip: one artist's bad row must not
            # kill the cycle for the other several hundred.
            report.artists_failed += 1
            continue
        report.add(result)
    logger.info(
        "watch cycle: polled=%d failed=%d baselined=%d groups=%d "
        "new=%d upcoming=%d date_changed=%d",
        report.artists_polled,
        report.artists_failed,
        report.artists_baselined,
        report.groups_seen,
        report.events_new,
        report.events_upcoming,
        report.events_date_changed,
    )
    return report
