"""F1 — Plex library sync: inventory artists, upsert, tombstone removals.

Pipeline step 1 of encore-plans/04: pull the artist inventory from the
configured music libraries, upsert on the Plex rating key, and tombstone
artists that disappeared (a non-NULL ``removed_at`` unwatches them without
losing their row). Compilation pseudo-artists ("Various Artists") are
skipped — they are not artists anyone wants release alerts for.

Logging policy (OBS-11): INFO lines carry counts and library keys only;
artist names appear at DEBUG only; the Plex token never appears at any
level. Enforced by the privacy regressions in `tests/test_scheduler.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sqlmodel import col, select

from encore.models import Artist, utcnow
from encore.plex import PlexArtist, PlexMusicClient
from encore.storage import Storage

__all__ = ["SyncError", "SyncReport", "is_compilation_artist", "sync_artists"]

logger = logging.getLogger(__name__)

# Plex represents compilations under a library-wide pseudo-artist. Matching or
# watching it would spray irrelevant alerts (roadmap F1's compilation guard).
_COMPILATION_NAMES = frozenset({"various artists"})


class SyncError(Exception):
    """The sync could not run (bad library selection, no music libraries)."""


def is_compilation_artist(name: str) -> bool:
    """Return True for compilation pseudo-artists ("Various Artists")."""
    return name.strip().casefold() in _COMPILATION_NAMES


@dataclass(frozen=True)
class SyncReport:
    """Counts from one sync run — what INFO-level logging is allowed to say."""

    library_keys: tuple[str, ...]
    seen: int
    added: int
    updated: int
    resurrected: int
    tombstoned: int
    skipped_compilations: int


def _resolve_library_keys(
    storage: Storage, client: PlexMusicClient, requested: Sequence[str] | None
) -> list[str]:
    """Pick the libraries to sync: explicit arg > stored selection > all music.

    Raises:
        SyncError: a requested key is not a music library, or none exist.
    """
    available = {library.key for library in client.music_libraries()}
    if not available:
        raise SyncError("the Plex server reports no music libraries to sync")
    selection = list(requested) if requested is not None else storage.get_plex_libraries()
    if selection is None:
        return sorted(available)
    unknown = sorted(set(selection) - available)
    if unknown:
        raise SyncError(
            f"library key(s) {', '.join(unknown)} are not music libraries on this "
            f"Plex server (available: {', '.join(sorted(available))})"
        )
    return list(dict.fromkeys(selection))  # de-dupe, keep order


def _upsert_artist(existing: dict[str, Artist], entry: PlexArtist) -> tuple[Artist, str]:
    """Apply one inventoried artist; return the row and what happened to it."""
    now = utcnow()
    row = existing.get(entry.rating_key)
    if row is None:
        return (
            Artist(
                plex_rating_key=entry.rating_key,
                name=entry.name,
                plex_guid=entry.guid,
                library_key=entry.library_key,
                first_seen_at=now,
                last_seen_at=now,
            ),
            "added",
        )
    outcome = "unchanged"
    if row.removed_at is not None:
        row.removed_at = None
        outcome = "resurrected"
    elif (row.name, row.plex_guid, row.library_key) != (
        entry.name,
        entry.guid,
        entry.library_key,
    ):
        outcome = "updated"
    row.name = entry.name
    row.plex_guid = entry.guid
    row.library_key = entry.library_key
    row.last_seen_at = now
    return row, outcome


def sync_artists(
    storage: Storage,
    client: PlexMusicClient,
    library_keys: Sequence[str] | None = None,
) -> SyncReport:
    """Run one full inventory sync and return the counts.

    ``library_keys`` overrides the stored selection; ``None`` falls back to
    the stored selection, and to *all* music libraries if none is stored.
    Artists present in the database (for the synced libraries) but absent
    from this run's inventory are tombstoned — unwatched on the very next
    sync after removal, as the roadmap acceptance requires.

    Raises:
        SyncError: the library selection is invalid.
    """
    keys = _resolve_library_keys(storage, client, library_keys)
    inventory: list[PlexArtist] = []
    skipped = 0
    for key in keys:
        entries = client.artists(key)
        for entry in entries:
            if is_compilation_artist(entry.name):
                logger.debug("sync: skipping compilation pseudo-artist %r", entry.name)
                skipped += 1
                continue
            inventory.append(entry)
        logger.info("sync: library %s inventoried, %d artist entries", key, len(entries))

    counts = {"added": 0, "updated": 0, "resurrected": 0, "unchanged": 0}
    now = utcnow()
    with storage.session() as session:
        # Match on library membership OR rating key: an artist that moved into
        # a synced library from an unsynced one must update its existing row
        # (plex_rating_key is unique) rather than insert a duplicate.
        inventory_keys = {entry.rating_key for entry in inventory}
        rows = session.exec(
            select(Artist).where(
                col(Artist.library_key).in_(keys) | col(Artist.plex_rating_key).in_(inventory_keys)
            )
        ).all()
        existing = {row.plex_rating_key: row for row in rows}
        seen_keys: set[str] = set()
        for entry in inventory:
            row, outcome = _upsert_artist(existing, entry)
            counts[outcome] += 1
            seen_keys.add(entry.rating_key)
            session.add(row)
        tombstoned = 0
        for rating_key, row in existing.items():
            if (
                rating_key not in seen_keys
                and row.library_key in keys  # never tombstone unsynced libraries
                and row.removed_at is None
            ):
                row.removed_at = now
                session.add(row)
                tombstoned += 1
        session.commit()

    report = SyncReport(
        library_keys=tuple(keys),
        seen=len(inventory),
        added=counts["added"],
        updated=counts["updated"],
        resurrected=counts["resurrected"],
        tombstoned=tombstoned,
        skipped_compilations=skipped,
    )
    logger.info(
        "sync: done — libraries=%s seen=%d added=%d updated=%d resurrected=%d "
        "tombstoned=%d skipped_compilations=%d",
        ",".join(report.library_keys),
        report.seen,
        report.added,
        report.updated,
        report.resurrected,
        report.tombstoned,
        report.skipped_compilations,
    )
    return report
