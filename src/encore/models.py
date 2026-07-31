"""SQLModel table definitions — the schema's first cut (encore-plans/04).

Scope so far: the ``settings`` singleton (F0), the ``artists`` inventory
(F1), the ``artist_matches`` identity cache + review queue (F2), and the
``release_groups`` + ``events`` watch tables (F3). Channels and
recommendations land with the features that read and write them (F4-F7),
each added by its own migration in `encore.storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

__all__ = [
    "MATCH_STATUSES",
    "RELEASE_EVENT_KINDS",
    "SETTINGS_ROW_ID",
    "AppSettings",
    "Artist",
    "ArtistMatch",
    "ReleaseEvent",
    "ReleaseGroup",
]

SETTINGS_ROW_ID = 1


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite stores it as ISO-8601 text)."""
    return datetime.now(UTC)


class AppSettings(SQLModel, table=True):
    """The singleton settings row (``id`` is always ``SETTINGS_ROW_ID``).

    Secret-bearing columns hold Fernet ciphertexts (``*_cipher``, bytes) —
    never plaintext. Encryption and decryption happen in `encore.storage`
    with the key file stored beside the database (docs/adr/0008).
    ``plex_library_keys`` is a JSON-encoded list of selected music-library
    keys (F1 multi-library pick); ``NULL`` means "all music libraries."
    """

    __tablename__ = "settings"

    id: int | None = Field(default=None, primary_key=True)
    plex_base_url: str | None = Field(default=None)
    plex_token_cipher: bytes | None = Field(default=None)
    plex_library_keys: str | None = Field(default=None)
    updated_at: datetime = Field(default_factory=utcnow)


class Artist(SQLModel, table=True):
    """One artist inventoried from a Plex music library (F1).

    Tombstoning: a non-NULL ``removed_at`` means the artist disappeared from
    Plex on a later sync — the row is kept (so F2 match results survive a
    temporary removal) but the artist is unwatched. MusicBrainz identity
    decisions live in `ArtistMatch`, keyed by the Plex rating key, per the
    one-migration-per-feature policy above.
    """

    __tablename__ = "artists"

    id: int | None = Field(default=None, primary_key=True)
    plex_rating_key: str = Field(unique=True, index=True)
    name: str
    plex_guid: str | None = Field(default=None)
    library_key: str = Field(index=True)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    removed_at: datetime | None = Field(default=None)


# Valid ArtistMatch.status values. "auto"/"manual" are matched; "pending" is
# the review queue; "skipped" is a deliberate leave-unmatched decision.
MATCH_STATUSES = ("auto", "manual", "pending", "skipped")


class ArtistMatch(SQLModel, table=True):
    """One artist's MusicBrainz identity decision (F2) — the permanent cache.

    ``artist_key`` is the caller's stable identifier for the artist (F1
    passes the Plex rating key; anything unique and stable works). The row
    is the cache the roadmap promises is permanent: any existing row
    short-circuits a re-query, and only an explicit re-match or resolution
    changes it. ``candidates_json`` keeps the ranked candidate list
    (mbid/name/score/disambiguation/type/country) so the review queue can
    display choices without re-hitting MusicBrainz. Artist names and MBIDs
    are taste data (docs/audits/dpia.md §4) — they live only in this local
    table and are never logged.
    """

    __tablename__ = "artist_matches"

    id: int | None = Field(default=None, primary_key=True)
    artist_key: str = Field(unique=True, index=True)
    artist_name: str
    status: str = Field(index=True)
    mbid: str | None = Field(default=None)
    confidence: float | None = Field(default=None)
    candidates_json: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ReleaseGroup(SQLModel, table=True):
    """One MusicBrainz release-group seen for a watched artist (F3).

    Release-*group* level, not release level (docs/adr/0001): the fourteen
    editions of the same album share one group, so reissues and edition-adds
    never look like news. ``first_release_date`` keeps MusicBrainz's partial
    date text verbatim (``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``; empty when
    MB has none) — parsing happens at diff time, never at storage time.
    ``artist_mbid`` links to the matched identity, not the Plex row, so a
    manual re-match (F2) naturally re-scopes the watch. Titles and MBIDs are
    taste data (docs/audits/dpia.md §4) — local only, never logged.
    """

    __tablename__ = "release_groups"

    id: int | None = Field(default=None, primary_key=True)
    mbid: str = Field(unique=True, index=True)
    artist_mbid: str = Field(index=True)
    title: str
    primary_type: str | None = Field(default=None)
    secondary_types_json: str | None = Field(default=None)
    first_release_date: str = Field(default="")
    first_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# Valid ReleaseEvent.kind values. "new" is a released (or undated) group first
# seen after the artist's baseline; "upcoming" is a future-dated group
# (announcements become calendar entries — F5); "date_changed" records a
# first-release-date revision on an already-seen group.
RELEASE_EVENT_KINDS = ("new", "upcoming", "date_changed")


class ReleaseEvent(SQLModel, table=True):
    """One release-watch observation for delivery (F3 writes, F4/F5 read).

    ``notified_at`` stays ``NULL`` until F4 delivers the event — it is the
    delivery queue's cursor, created here so the F3 diff and the F4 fan-out
    share one table instead of a table and a shadow queue.
    """

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    release_group_id: int = Field(foreign_key="release_groups.id", index=True)
    kind: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    notified_at: datetime | None = Field(default=None)
