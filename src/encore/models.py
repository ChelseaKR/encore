"""SQLModel table definitions — the schema's first cut (encore-plans/04).

Scope so far: the ``settings`` singleton (F0), the ``artists`` inventory
(F1), the ``artist_matches`` identity cache + review queue (F2), the
``release_groups`` + ``events`` watch tables (F3), and the ``channels`` +
``deliveries`` notification tables (F4). Feeds and recommendations land with
the features that read and write them (F5-F7), each added by its own
migration in `encore.storage`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

__all__ = [
    "CHANNEL_MODES",
    "DELIVERY_STATUSES",
    "MATCH_STATUSES",
    "RELEASE_EVENT_KINDS",
    "SETTINGS_ROW_ID",
    "AppSettings",
    "Artist",
    "ArtistMatch",
    "Delivery",
    "EventView",
    "NotificationChannel",
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
    ``plex_machine_identifier`` is the server's own public identifier, learned
    (read-only) during a sync and used to build the ``app.plex.tv`` deep links
    F4 notifications carry — it is not a secret and not taste data.
    """

    __tablename__ = "settings"

    id: int | None = Field(default=None, primary_key=True)
    plex_base_url: str | None = Field(default=None)
    plex_token_cipher: bytes | None = Field(default=None)
    plex_library_keys: str | None = Field(default=None)
    plex_machine_identifier: str | None = Field(default=None)
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

    ``notified_at`` stays ``NULL`` until F4 has finished with the event: it is
    stamped once every `Delivery` row fanned out from it has reached a
    terminal state. An event with no deliveries at all (no channel configured,
    or every channel created after the event) keeps ``NULL`` forever and is
    read only by the in-app feed — nothing was sent, and saying otherwise
    would be a lie in the one column an operator would check.
    """

    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    release_group_id: int = Field(foreign_key="release_groups.id", index=True)
    kind: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    notified_at: datetime | None = Field(default=None)


# Valid NotificationChannel.mode values. "instant" delivers each event as its
# own notification on the next cycle; "digest" rolls pending events into one
# message per ``digest_interval_hours`` (F4's two delivery cadences).
CHANNEL_MODES = ("instant", "digest")


class NotificationChannel(SQLModel, table=True):
    """One Apprise destination (ntfy, Discord, email, generic webhook…) — F4.

    ``url_cipher`` holds the Fernet ciphertext of the Apprise URL, never the
    URL itself: an Apprise URL *is* a credential (``ntfy://user:pass@…``,
    ``discord://webhook_id/webhook_token``), so it is encrypted at rest under
    the same scheme as the Plex token (docs/adr/0008) and never logged or
    printed. The ``last_*``/``consecutive_failures`` columns are the "surface
    the failure instead of dying silently" half of F4's acceptance: a channel
    that is failing says so, with its most recent error, in
    ``encore channels list`` today and in the F6 UI when one exists.
    """

    __tablename__ = "channels"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    url_cipher: bytes
    mode: str = Field(default="instant", index=True)
    enabled: bool = Field(default=True, index=True)
    digest_interval_hours: float = Field(default=24.0)
    last_digest_at: datetime | None = Field(default=None)
    last_success_at: datetime | None = Field(default=None)
    last_failure_at: datetime | None = Field(default=None)
    last_error: str | None = Field(default=None)
    consecutive_failures: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# Valid Delivery.status values. "pending" is owed (possibly after a backoff
# wait); "delivered" and "failed" are terminal — "failed" means the bounded
# retries were exhausted, not that the next cycle will try again.
DELIVERY_STATUSES = ("pending", "delivered", "failed")


class Delivery(SQLModel, table=True):
    """One (event, channel) delivery obligation with its retry state (F4).

    The fan-out is materialized rather than computed: one row per event per
    channel means a five-channel install can have a Discord delivery succeed
    while an email one is still backing off, which a single ``notified_at``
    flag on the event could never express. ``next_attempt_at`` is the backoff
    clock — a pending row is only tried once the cycle's ``now`` reaches it.
    """

    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("event_id", "channel_id", name="uq_delivery_event_channel"),)

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    channel_id: int = Field(foreign_key="channels.id", index=True)
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0)
    next_attempt_at: datetime = Field(default_factory=utcnow, index=True)
    last_error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


@dataclass(frozen=True)
class EventView:
    """One event joined to everything a renderer or feed needs — not a table.

    F4's notification text, the in-app feed, and (next) F5's RSS/iCal entries
    all want the same shape: the event, its release-group, and the artist's
    *display* name, which lives on `ArtistMatch` rather than on the group.
    Assembling it once in `encore.storage` keeps the join out of three
    consumers. Every field here is taste data — an `EventView` must never
    reach a log line (docs/audits/dpia.md §4).
    """

    event_id: int
    kind: str
    created_at: datetime
    release_group_mbid: str
    title: str
    primary_type: str | None
    secondary_types: tuple[str, ...]
    first_release_date: str
    artist_mbid: str
    artist_name: str
    plex_rating_key: str | None
