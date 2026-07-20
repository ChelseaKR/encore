"""SQLModel table definitions — the schema's first cut (encore-plans/04).

M1 scope so far: the ``settings`` singleton (F0), the ``artists`` inventory
(F1), and the ``artist_matches`` identity cache + review queue (F2).
Release-groups, events, channels, and recommendations land with the features
that read and write them (F3-F7), each added by its own migration in
`encore.storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

__all__ = ["MATCH_STATUSES", "SETTINGS_ROW_ID", "AppSettings", "Artist", "ArtistMatch"]

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
