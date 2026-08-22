"""The storage layer: SQLite (WAL) via SQLModel — one file, one data directory.

Design (docs/adr/0005 + docs/adr/0008): a single SQLite database in a mounted
volume is the only datastore; a Fernet key file sits beside it and encrypts
the secret-bearing columns (the Plex token since F0, Apprise channel URLs
since F4, the feed token since F5). Schema changes run as ordered
forward migrations tracked in SQLite's ``PRAGMA user_version`` — there is no
down-migration story, matching the single-operator deployment model
(backup = copy the directory).
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, SQLModel, col, create_engine, select

from encore.artistsettings import (
    PRIORITY_DIGEST,
    PRIORITY_INSTANT,
    PRIORITY_NORMAL,
    ArtistWatchSettings,
    SettingsError,
    SettingsOverride,
    canonical_override_json,
    parse_settings_json,
    resolve_effective,
)
from encore.models import (
    CHANNEL_MODES,
    DELIVERY_STATUSES,
    MATCH_STATUSES,
    RECOMMENDATION_STATUSES,
    RELEASE_EVENT_KINDS,
    SETTINGS_ROW_ID,
    AppSettings,
    Artist,
    ArtistMatch,
    Delivery,
    EventView,
    NotificationChannel,
    Recommendation,
    ReleaseEvent,
    ReleaseGroup,
    UpcomingReleaseView,
    utcnow,
)
from encore.secretstore import SecretCipher, SecretKeyError

__all__ = [
    "DATA_DIR_ENV",
    "DB_FILENAME",
    "KEY_FILENAME",
    "MIGRATIONS",
    "Storage",
    "StorageError",
    "resolve_data_dir",
]

DB_FILENAME = "encore.db"
KEY_FILENAME = "encore.key"
DATA_DIR_ENV = "ENCORE_DATA_DIR"
DEFAULT_DATA_DIR = "data"


class StorageError(Exception):
    """The data directory or database is missing, unusable, or incompatible."""


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Resolve the data directory: explicit argument > ``$ENCORE_DATA_DIR`` > ``./data``."""
    if explicit is not None:
        return Path(explicit)
    env_value = os.environ.get(DATA_DIR_ENV)
    if env_value:
        return Path(env_value)
    return Path(DEFAULT_DATA_DIR)


def _migration_0001_initial_schema(connection: Connection) -> None:
    """v1 (F0): create the initial schema from the current model metadata.

    ``create_all`` (checkfirst) creates whatever the *current* metadata
    declares, so a fresh database gets the up-to-date schema here and later
    create-only migrations no-op. Databases written by an older build enter
    at their recorded ``user_version`` and run only what follows it.
    """
    SQLModel.metadata.create_all(connection)


def _migration_0002_artists_and_library_selection(connection: Connection) -> None:
    """v2 (F1): create ``artists``; add ``settings.plex_library_keys``.

    Both steps are guarded so this is correct for a fresh database (v1
    already created everything from current metadata) and for a real v1
    database written by the F0 build (which lacks both).
    """
    SQLModel.metadata.create_all(connection)
    settings_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(settings)")}
    if "plex_library_keys" not in settings_columns:
        connection.exec_driver_sql("ALTER TABLE settings ADD COLUMN plex_library_keys VARCHAR")


def _migration_0003_artist_matches(connection: Connection) -> None:
    """v3 (F2): create ``artist_matches`` (guarded — no-op on a fresh database)."""
    SQLModel.metadata.create_all(connection)


def _migration_0004_release_watching(connection: Connection) -> None:
    """v4 (F3): create ``release_groups`` + ``events`` (guarded — no-op on fresh)."""
    SQLModel.metadata.create_all(connection)


def _migration_0005_notifications(connection: Connection) -> None:
    """v5 (F4): create ``channels`` + ``deliveries``; add the Plex machine id.

    Both steps are guarded, so this is correct for a fresh database (v1
    already created everything from current metadata) and for a real v4
    database written by the F3 build (which has neither).
    """
    SQLModel.metadata.create_all(connection)
    settings_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(settings)")}
    if "plex_machine_identifier" not in settings_columns:
        connection.exec_driver_sql(
            "ALTER TABLE settings ADD COLUMN plex_machine_identifier VARCHAR"
        )


def _migration_0006_feed_token(connection: Connection) -> None:
    """v6 (F5): add ``settings.feed_token_cipher`` (guarded — no-op on fresh).

    The token itself is *not* generated here: it is minted lazily by
    `Storage.ensure_feed_token` the first time the user asks for their feed
    URLs, so a database that has never served a feed holds no capability to
    leak.
    """
    SQLModel.metadata.create_all(connection)
    settings_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(settings)")}
    if "feed_token_cipher" not in settings_columns:
        connection.exec_driver_sql("ALTER TABLE settings ADD COLUMN feed_token_cipher BLOB")


def _migration_0007_shared_release_groups(connection: Connection) -> None:
    """v7 (F3 fix): release-group rows are unique per (artist, group), not per group.

    A release-group credited to several watched artists (a split single, a
    collab live EP) needs one row per artist so each artist's baseline and
    events stay independent; the v4 schema's globally-unique ``mbid`` index
    made the second artist's insert an IntegrityError that killed the whole
    watch cycle. Both steps are guarded, so this is correct for a fresh
    database (v1 already created the current shape) and for a real v6
    database written by the F5 build (unique ``mbid``, no composite index).
    """
    indexes = {
        row[1]: bool(row[2])
        for row in connection.exec_driver_sql("PRAGMA index_list(release_groups)")
    }
    if indexes.get("ix_release_groups_mbid"):
        connection.exec_driver_sql("DROP INDEX ix_release_groups_mbid")
        connection.exec_driver_sql("CREATE INDEX ix_release_groups_mbid ON release_groups (mbid)")
    if "uq_release_group_artist_group" not in indexes:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_release_group_artist_group "
            "ON release_groups (artist_mbid, mbid)"
        )


def _migration_0008_watch_settings(connection: Connection) -> None:
    """v8 (F10): per-artist ``artists.settings_json`` + global watch defaults.

    Both steps are guarded, so this is correct for a fresh database (v1
    already created everything from current metadata) and for a real v7
    database written by the F5 build (which has neither column). The blobs
    themselves are written only through `Storage` methods that validate via
    `encore.artistsettings` — a raw hand-edited value fails closed at read
    time with `SettingsError` instead of silently changing behavior.
    """
    SQLModel.metadata.create_all(connection)
    artists_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(artists)")}
    if "settings_json" not in artists_columns:
        connection.exec_driver_sql("ALTER TABLE artists ADD COLUMN settings_json VARCHAR")
    settings_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(settings)")}
    if "watch_defaults_json" not in settings_columns:
        connection.exec_driver_sql("ALTER TABLE settings ADD COLUMN watch_defaults_json VARCHAR")


def _migration_0009_play_counts(connection: Connection) -> None:
    """v9 (F9): add ``artists.play_count`` (guarded — no-op on fresh)."""
    SQLModel.metadata.create_all(connection)
    artists_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(artists)")}
    if "play_count" not in artists_columns:
        connection.exec_driver_sql(
            "ALTER TABLE artists ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0"
        )


def _migration_0010_recommendations(connection: Connection) -> None:
    """v10 (F7): create ``recommendations`` (guarded — no-op on fresh)."""
    SQLModel.metadata.create_all(connection)


# Ordered forward migrations; index+1 is the schema version they produce.
# Append-only: released migrations are never edited, only extended.
MIGRATIONS: tuple[Callable[[Connection], None], ...] = (
    _migration_0001_initial_schema,
    _migration_0002_artists_and_library_selection,
    _migration_0003_artist_matches,
    _migration_0004_release_watching,
    _migration_0005_notifications,
    _migration_0006_feed_token,
    _migration_0007_shared_release_groups,
    _migration_0008_watch_settings,
    _migration_0009_play_counts,
    _migration_0010_recommendations,
)


def _sqlite_on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
    """Per-connection PRAGMAs (WAL is persistent in the file; this one is not)."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Storage:
    """Owns the data directory, the SQLite engine, migrations, and the cipher."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        """Create/open the data directory, key file, and database; run migrations.

        Raises:
            StorageError: the data directory cannot be created, or the database
                schema is newer than this build understands.
        """
        self.data_dir = resolve_data_dir(data_dir)
        self._ensure_data_dir()
        self.db_path = self.data_dir / DB_FILENAME
        key_path = self.data_dir / KEY_FILENAME
        try:
            database_exists = self._entry_exists(self.db_path)
            key_exists = self._entry_exists(key_path)
        except OSError as exc:
            raise StorageError(f"cannot inspect data directory {self.data_dir}: {exc}") from exc
        if database_exists and not key_exists:
            raise StorageError(
                f"database exists at {self.db_path}, but its companion Fernet key is missing "
                f"at {key_path}; refusing to create a replacement key. Restore the database "
                "and key together from the same backup."
            )
        try:
            self.cipher = SecretCipher.load_or_create(key_path)
        except SecretKeyError as exc:
            raise StorageError(f"cannot use Fernet key at {key_path}: {exc}") from exc
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            # The FastAPI threadpool serves requests from worker threads; SQLite
            # connections are pooled per-engine, not per-thread.
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _sqlite_on_connect)
        self._migrate()

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        """Test for a directory entry without following a possible symlink."""
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _ensure_data_dir(self) -> None:
        """Create the data directory (mode 0700) if needed; fail fast if unusable."""
        try:
            self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"cannot create data directory {self.data_dir}: {exc}") from exc

    def _migrate(self) -> None:
        """Bring the schema to the current version; refuse a newer-than-us database."""
        try:
            with self.engine.connect() as connection:
                # WAL is set once and persists in the database file (ADR-0005).
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                row = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
                current = int(row)
                if current > len(MIGRATIONS):
                    raise StorageError(
                        f"database schema is v{current}, but this build of encore only "
                        f"understands up to v{len(MIGRATIONS)} — upgrade encore instead "
                        "of downgrading the database."
                    )
                for version, migration in enumerate(MIGRATIONS[current:], start=current + 1):
                    migration(connection)
                    connection.exec_driver_sql(f"PRAGMA user_version = {version:d}")
                    connection.commit()
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot open database at {self.db_path}: {exc}") from exc

    def session(self) -> Session:
        """Open a new ORM session (caller is responsible for closing it)."""
        return Session(self.engine)

    def check_ready(self) -> None:
        """Probe the database with a trivial query (the ``/readyz`` DB check).

        Raises:
            StorageError: the query could not be executed.
        """
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").scalar_one()
        except SQLAlchemyError as exc:
            raise StorageError(f"database at {self.db_path} is not ready: {exc}") from exc

    def close(self) -> None:
        """Dispose the engine's connection pool."""
        self.engine.dispose()

    # -- settings ------------------------------------------------------------

    def get_settings(self, session: Session) -> AppSettings:
        """Fetch the singleton settings row, creating it on first access."""
        settings = session.get(AppSettings, SETTINGS_ROW_ID)
        if settings is None:
            settings = AppSettings(id=SETTINGS_ROW_ID)
            session.add(settings)
            session.commit()
            session.refresh(settings)
        return settings

    def set_plex_credentials(self, base_url: str, token: str) -> None:
        """Store the Plex base URL and token; the token is encrypted at rest."""
        with self.session() as session:
            settings = self.get_settings(session)
            settings.plex_base_url = base_url
            settings.plex_token_cipher = self.cipher.encrypt(token)
            settings.updated_at = utcnow()
            session.add(settings)
            session.commit()

    def get_plex_credentials(self) -> tuple[str, str] | None:
        """Return ``(base_url, token)`` if configured, else ``None``.

        Raises:
            SecretDecryptionError: the key file does not match the ciphertext
                (e.g. a database restored without its key — docs/adr/0008).
        """
        with self.session() as session:
            settings = self.get_settings(session)
            if settings.plex_base_url is None or settings.plex_token_cipher is None:
                return None
            return settings.plex_base_url, self.cipher.decrypt(settings.plex_token_cipher)

    def set_plex_libraries(self, library_keys: Sequence[str] | None) -> None:
        """Persist the selected music-library keys; ``None`` means all (F1)."""
        with self.session() as session:
            settings = self.get_settings(session)
            settings.plex_library_keys = (
                None if library_keys is None else json.dumps(list(library_keys))
            )
            settings.updated_at = utcnow()
            session.add(settings)
            session.commit()

    def get_plex_libraries(self) -> list[str] | None:
        """Return the selected music-library keys, or ``None`` for "all"."""
        with self.session() as session:
            settings = self.get_settings(session)
            if settings.plex_library_keys is None:
                return None
            keys = json.loads(settings.plex_library_keys)
            return [str(key) for key in keys]

    # -- artist matches (F2) --------------------------------------------------

    def get_artist_match(self, artist_key: str) -> ArtistMatch | None:
        """Return the cached match decision for ``artist_key``, if any."""
        with self.session() as session:
            statement = select(ArtistMatch).where(ArtistMatch.artist_key == artist_key)
            return session.exec(statement).first()

    def save_artist_match(
        self,
        artist_key: str,
        artist_name: str,
        status: str,
        mbid: str | None = None,
        confidence: float | None = None,
        candidates_json: str | None = None,
    ) -> ArtistMatch:
        """Insert or overwrite the match row for ``artist_key`` (the upsert).

        Raises:
            StorageError: ``status`` is not one of `encore.models.MATCH_STATUSES`.
        """
        if status not in MATCH_STATUSES:
            raise StorageError(f"invalid match status {status!r}; expected {MATCH_STATUSES}")
        with self.session() as session:
            statement = select(ArtistMatch).where(ArtistMatch.artist_key == artist_key)
            row = session.exec(statement).first()
            if row is None:
                row = ArtistMatch(artist_key=artist_key, artist_name=artist_name, status=status)
            row.artist_name = artist_name
            row.status = status
            row.mbid = mbid
            row.confidence = confidence
            row.candidates_json = candidates_json
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def list_review_queue(self) -> list[ArtistMatch]:
        """All artists awaiting review (status ``pending``), oldest first."""
        with self.session() as session:
            statement = (
                select(ArtistMatch)
                .where(ArtistMatch.status == "pending")
                .order_by(ArtistMatch.created_at)  # type: ignore[arg-type]
            )
            return list(session.exec(statement).all())

    def resolve_artist_match(self, artist_key: str, mbid: str) -> ArtistMatch:
        """Manually match ``artist_key`` to ``mbid`` — resolution or re-match.

        Works from any prior status: it resolves a pending review, and it
        overrides a wrong auto/manual match (the roadmap's manual re-match).

        Raises:
            StorageError: no match row exists for ``artist_key``.
        """
        with self.session() as session:
            statement = select(ArtistMatch).where(ArtistMatch.artist_key == artist_key)
            row = session.exec(statement).first()
            if row is None:
                raise StorageError(f"no artist match row exists for key {artist_key!r}")
            row.status = "manual"
            row.mbid = mbid
            row.confidence = None
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def skip_artist_match(self, artist_key: str) -> ArtistMatch:
        """Mark ``artist_key`` deliberately unmatched (kept, so no re-query).

        Raises:
            StorageError: no match row exists for ``artist_key``.
        """
        with self.session() as session:
            statement = select(ArtistMatch).where(ArtistMatch.artist_key == artist_key)
            row = session.exec(statement).first()
            if row is None:
                raise StorageError(f"no artist match row exists for key {artist_key!r}")
            row.status = "skipped"
            row.mbid = None
            row.confidence = None
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def list_unmatched_artists(self) -> list[Artist]:
        """Present (non-tombstoned) artists with no match decision yet.

        The backlog `encore match` (F2) works through: every synced artist
        that has never been through the matching engine, oldest-seen first,
        so a fresh install's first match run has a stable order and the same
        artist is never silently skipped. An artist that already has *any*
        `ArtistMatch` row — auto, manual, pending, or skipped — is excluded;
        re-matching a resolved or skipped decision is the explicit ``force``
        path (`MatchEngine.match_artist`), never an implicit side effect of
        this list.
        """
        with self.session() as session:
            already_matched = select(ArtistMatch.artist_key)
            statement = (
                select(Artist)
                .where(
                    Artist.removed_at.is_(None),  # type: ignore[union-attr]
                    col(Artist.plex_rating_key).not_in(already_matched),
                )
                .order_by(Artist.first_seen_at)  # type: ignore[arg-type]
            )
            return list(session.exec(statement).all())

    # -- release watching (F3) -------------------------------------------------

    def list_watched_artist_mbids(self) -> list[str]:
        """Distinct MBIDs to poll: matched artists still present in Plex,
        plus promoted recommendation candidates (F8).

        Joins ``artist_matches`` (status ``auto``/``manual``, non-NULL MBID)
        to ``artists`` on the Plex rating key and excludes tombstoned rows —
        this is what makes "removal unwatches on next sync" (F1 acceptance)
        true without F3 keeping its own bookkeeping. F8 adds the MBIDs the
        user explicitly **promoted** from recommendations: promotion *is*
        the opt-in, so a promoted artist's releases flow through the same
        watch → event → channel pipeline as owned music — never silently.
        """
        with self.session() as session:
            owned = session.exec(
                select(ArtistMatch.mbid)
                .join(Artist, Artist.plex_rating_key == ArtistMatch.artist_key)  # type: ignore[arg-type]
                .where(
                    ArtistMatch.status.in_(("auto", "manual")),  # type: ignore[attr-defined]
                    ArtistMatch.mbid.is_not(None),  # type: ignore[union-attr]
                    Artist.removed_at.is_(None),  # type: ignore[union-attr]
                )
                .distinct()
            )
            watched = {mbid for mbid in owned if mbid is not None}
            promoted = session.exec(
                select(Recommendation.mbid).where(
                    Recommendation.status == "promoted",
                    col(Recommendation.mbid).not_in(watched),
                )
            ).all()
            watched.update(mbid for mbid in promoted if mbid)
            return sorted(watched)

    def list_release_groups(self, artist_mbid: str) -> list[ReleaseGroup]:
        """All release-groups already recorded for one artist MBID."""
        with self.session() as session:
            statement = select(ReleaseGroup).where(ReleaseGroup.artist_mbid == artist_mbid)
            return list(session.exec(statement).all())

    def has_release_groups(self, artist_mbid: str) -> bool:
        """Whether any release-group row exists for this artist (baseline test)."""
        with self.session() as session:
            statement = (
                select(ReleaseGroup.id).where(ReleaseGroup.artist_mbid == artist_mbid).limit(1)
            )
            return session.exec(statement).first() is not None

    def add_release_group(
        self,
        artist_mbid: str,
        mbid: str,
        title: str,
        primary_type: str | None,
        secondary_types: Sequence[str],
        first_release_date: str,
    ) -> ReleaseGroup:
        """Record a newly seen release-group."""
        row = ReleaseGroup(
            artist_mbid=artist_mbid,
            mbid=mbid,
            title=title,
            primary_type=primary_type,
            secondary_types_json=json.dumps(list(secondary_types)) if secondary_types else None,
            first_release_date=first_release_date,
        )
        with self.session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def update_release_group_date(
        self, artist_mbid: str, mbid: str, first_release_date: str
    ) -> ReleaseGroup:
        """Record a revised first-release date on an already-seen group.

        Scoped to one artist's row: a group shared between watched artists
        has one row per artist (v7), and each artist's poll revises its own.

        Raises:
            StorageError: no release-group row exists for this artist + MBID.
        """
        with self.session() as session:
            statement = select(ReleaseGroup).where(
                ReleaseGroup.artist_mbid == artist_mbid, ReleaseGroup.mbid == mbid
            )
            row = session.exec(statement).first()
            if row is None:
                # The offending MBID is deliberately not echoed — error strings
                # end up in logs, and MBIDs are taste data (dpia.md §4).
                raise StorageError("no release-group row exists for that MBID")
            row.first_release_date = first_release_date
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def add_event(self, release_group_id: int, kind: str) -> ReleaseEvent:
        """Append one release event (F4/F5 consume these; ``notified_at`` NULL).

        Raises:
            StorageError: ``kind`` is not one of `encore.models.RELEASE_EVENT_KINDS`.
        """
        if kind not in RELEASE_EVENT_KINDS:
            raise StorageError(f"invalid event kind {kind!r}; expected {RELEASE_EVENT_KINDS}")
        event_row = ReleaseEvent(release_group_id=release_group_id, kind=kind)
        with self.session() as session:
            session.add(event_row)
            session.commit()
            session.refresh(event_row)
        return event_row

    def list_events(self, kind: str | None = None) -> list[ReleaseEvent]:
        """Release events, oldest first, optionally filtered by kind."""
        with self.session() as session:
            statement = select(ReleaseEvent).order_by(ReleaseEvent.created_at)  # type: ignore[arg-type]
            if kind is not None:
                statement = statement.where(ReleaseEvent.kind == kind)
            return list(session.exec(statement).all())

    def set_plex_machine_identifier(self, machine_identifier: str) -> None:
        """Record the Plex server's machine identifier (for F4 deep links)."""
        with self.session() as session:
            settings = self.get_settings(session)
            settings.plex_machine_identifier = machine_identifier
            settings.updated_at = utcnow()
            session.add(settings)
            session.commit()

    def get_plex_machine_identifier(self) -> str | None:
        """Return the stored Plex machine identifier (``None`` before the first sync)."""
        with self.session() as session:
            return self.get_settings(session).plex_machine_identifier

    # -- notification channels (F4) -------------------------------------------

    def add_channel(
        self,
        name: str,
        url: str,
        mode: str = "instant",
        digest_interval_hours: float = 24.0,
    ) -> NotificationChannel:
        """Create a channel; the Apprise URL is encrypted at rest (docs/adr/0008).

        Raises:
            StorageError: ``mode`` is not one of `encore.models.CHANNEL_MODES`,
                ``digest_interval_hours`` is not positive, or ``name`` is taken.
        """
        if mode not in CHANNEL_MODES:
            raise StorageError(f"invalid channel mode {mode!r}; expected {CHANNEL_MODES}")
        if digest_interval_hours <= 0:
            raise StorageError("digest interval must be a positive number of hours")
        row = NotificationChannel(
            name=name,
            url_cipher=self.cipher.encrypt(url),
            mode=mode,
            digest_interval_hours=digest_interval_hours,
        )
        with self.session() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise StorageError(f"a notification channel named {name!r} already exists") from exc
            session.refresh(row)
        return row

    def list_channels(self, enabled_only: bool = False) -> list[NotificationChannel]:
        """Return channels in creation order; optionally only the enabled ones."""
        with self.session() as session:
            statement = select(NotificationChannel).order_by(col(NotificationChannel.created_at))
            if enabled_only:
                statement = statement.where(NotificationChannel.enabled)
            return list(session.exec(statement).all())

    def get_channel(self, name: str) -> NotificationChannel | None:
        """Return one channel by name, or ``None``."""
        with self.session() as session:
            statement = select(NotificationChannel).where(NotificationChannel.name == name)
            return session.exec(statement).first()

    def channel_url(self, channel: NotificationChannel) -> str:
        """Decrypt a channel's Apprise URL — the only place it exists in plaintext.

        Callers must treat the result as a credential: never log it, never
        print it, never include it in an error message.

        Raises:
            SecretDecryptionError: the key file does not match the ciphertext.
        """
        return self.cipher.decrypt(channel.url_cipher)

    def set_channel_enabled(self, name: str, enabled: bool) -> NotificationChannel:
        """Enable or disable a channel without deleting its history.

        Raises:
            StorageError: no channel with that name exists.
        """
        with self.session() as session:
            row = self._require_channel(session, name)
            row.enabled = enabled
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def remove_channel(self, name: str) -> None:
        """Delete a channel and the delivery rows that fanned out to it.

        Raises:
            StorageError: no channel with that name exists.
        """
        with self.session() as session:
            row = self._require_channel(session, name)
            for delivery in session.exec(
                select(Delivery).where(Delivery.channel_id == row.id)
            ).all():
                session.delete(delivery)
            session.delete(row)
            session.commit()

    @staticmethod
    def _require_channel(session: Session, name: str) -> NotificationChannel:
        """Fetch a channel by name inside an open session, or raise."""
        statement = select(NotificationChannel).where(NotificationChannel.name == name)
        row = session.exec(statement).first()
        if row is None:
            raise StorageError(f"no notification channel named {name!r}")
        return row

    def record_channel_result(
        self,
        channel_id: int,
        success: bool,
        error: str | None = None,
        digest_sent_at: datetime | None = None,
    ) -> None:
        """Update a channel's health after an attempt (F4's "surface it" half)."""
        now = utcnow()
        with self.session() as session:
            row = session.get(NotificationChannel, channel_id)
            if row is None:  # pragma: no cover - the channel was removed mid-cycle
                return
            if success:
                row.last_success_at = now
                row.consecutive_failures = 0
                row.last_error = None
            else:
                row.last_failure_at = now
                row.consecutive_failures += 1
                row.last_error = error
            if digest_sent_at is not None:
                row.last_digest_at = digest_sent_at
            row.updated_at = now
            session.add(row)
            session.commit()

    # -- delivery queue (F4) ---------------------------------------------------

    def ensure_deliveries(self, now: datetime | None = None) -> tuple[int, int]:
        """Materialize missing (event, channel) delivery rows; honor muting (F10).

        Only events created *after* a channel was added fan out to it. Adding
        a channel therefore never replays history — the same
        don't-flood-on-first-contact rule the F3 baseline applies to a newly
        watched artist (docs/adr/0011), applied to a newly added channel.

        Events from artists **muted on this cycle** are skipped, and their
        ``notified_at`` is stamped immediately with no deliveries at all:
        muting means "these releases never page me," so lifting the mute
        later must not replay what it suppressed. The events stay recorded
        and visible in the feeds — muting silences pings, not history.

        Returns:
            ``(created, muted_skipped)`` — delivery rows created and events
            suppressed as muted.

        ``now`` stamps ``next_attempt_at`` so a row created during a cycle is
        due *in* that cycle; without it a brand-new delivery would be a few
        microseconds in the future and wait a whole interval for nothing.
        """
        if now is None:
            now = utcnow()
        created = 0
        muted_skipped = 0
        with self.session() as session:
            channels = session.exec(
                select(NotificationChannel).where(NotificationChannel.enabled)
            ).all()
            if not channels:
                return 0, 0
            events = session.exec(
                select(ReleaseEvent).where(col(ReleaseEvent.notified_at).is_(None))
            ).all()
            if not events:
                return 0, 0
            event_ids = [event.id for event in events if event.id is not None]
            group_rows = {
                group.id: group
                for group in session.exec(
                    select(ReleaseGroup).where(
                        col(ReleaseGroup.id).in_({e.release_group_id for e in events})
                    )
                ).all()
            }
            policies = self.effective_watch_settings_for_mbids(
                [group.artist_mbid for group in group_rows.values()]
            )
            existing = {
                (delivery.event_id, delivery.channel_id)
                for delivery in session.exec(
                    select(Delivery).where(col(Delivery.event_id).in_(event_ids))
                ).all()
            }
            today = now.date()
            for event in events:
                if event.id is None:  # pragma: no cover - persisted rows always have one
                    continue
                group = group_rows.get(event.release_group_id)
                policy = policies.get(group.artist_mbid) if group is not None else None
                if policy is not None and policy.is_muted_on(today):
                    # Settled-without-delivery: visible in feeds, never sent.
                    event.notified_at = now
                    session.add(event)
                    muted_skipped += 1
                    continue
                created += self._create_missing_deliveries(session, event, channels, existing, now)
            session.commit()
        return created, muted_skipped

    @staticmethod
    def _create_missing_deliveries(
        session: Session,
        event: ReleaseEvent,
        channels: Sequence[NotificationChannel],
        existing: set[tuple[int, int]],
        now: datetime,
    ) -> int:
        """Insert absent (event, channel) rows; return how many were created."""
        if event.id is None:  # pragma: no cover - persisted rows always have one
            return 0
        created = 0
        for channel in channels:
            if channel.id is None:  # pragma: no cover - persisted rows have one
                continue
            if event.created_at < channel.created_at:
                continue
            if (event.id, channel.id) in existing:
                continue
            session.add(Delivery(event_id=event.id, channel_id=channel.id, next_attempt_at=now))
            created += 1
        return created

    def due_deliveries(self, channel_id: int, now: datetime) -> list[Delivery]:
        """Return pending deliveries for one channel whose backoff has elapsed."""
        with self.session() as session:
            statement = (
                select(Delivery)
                .where(
                    Delivery.channel_id == channel_id,
                    Delivery.status == "pending",
                    col(Delivery.next_attempt_at) <= now,
                )
                .order_by(col(Delivery.created_at))
            )
            return list(session.exec(statement).all())

    def update_delivery(
        self,
        delivery_id: int,
        status: str,
        attempts: int,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> None:
        """Write one delivery's outcome back (status, attempt count, backoff).

        Raises:
            StorageError: ``status`` is not one of
                `encore.models.DELIVERY_STATUSES`.
        """
        if status not in DELIVERY_STATUSES:
            raise StorageError(f"invalid delivery status {status!r}; expected {DELIVERY_STATUSES}")
        with self.session() as session:
            row = session.get(Delivery, delivery_id)
            if row is None:  # pragma: no cover - the delivery was removed mid-cycle
                return
            row.status = status
            row.attempts = attempts
            row.last_error = last_error
            if next_attempt_at is not None:
                row.next_attempt_at = next_attempt_at
            row.updated_at = utcnow()
            session.add(row)
            session.commit()

    def settle_events(self, event_ids: Sequence[int]) -> int:
        """Stamp ``notified_at`` on events with no pending deliveries left.

        "Settled" means encore is done trying, not that every channel
        succeeded — a channel that exhausted its retries is terminal too, and
        its failure is recorded on the channel row, not hidden in the event.
        """
        settled = 0
        now = utcnow()
        with self.session() as session:
            for event_id in dict.fromkeys(event_ids):
                deliveries = session.exec(
                    select(Delivery).where(Delivery.event_id == event_id)
                ).all()
                if not deliveries or any(row.status == "pending" for row in deliveries):
                    continue
                event = session.get(ReleaseEvent, event_id)
                if event is None or event.notified_at is not None:
                    continue
                event.notified_at = now
                session.add(event)
                settled += 1
            session.commit()
        return settled

    # -- watch settings (F10) ---------------------------------------------------

    def get_watch_defaults(self) -> SettingsOverride:
        """Return the global watch policy layer (type allowlists only, by validation).

        Raises:
            StorageError: the stored blob fails validation — it is honored
                by no consumer rather than half-honored by all of them.
        """
        with self.session() as session:
            raw = self.get_settings(session).watch_defaults_json
        try:
            return parse_settings_json(raw)
        except SettingsError as exc:
            raise StorageError(f"stored global watch defaults are invalid: {exc}") from exc

    def set_watch_default_types(
        self,
        allow_primary: Sequence[str] | None = None,
        allow_secondary: Sequence[str] | None = None,
    ) -> SettingsOverride:
        """Replace the global default type allowlists (both lists required together).

        ``None`` for a list means "leave that list as currently stored";
        pass an empty sequence explicitly to forbid that whole class (e.g.
        ``allow_secondary=()`` = no secondary types pass anywhere). The
        muting/priority fields of the layer stay unset — they are
        per-artist by design.

        Raises:
            StorageError: a slug is unknown or the resulting policy would
                be incoherent.
        """
        try:
            current = self.get_watch_defaults()
            primary = (
                tuple(dict.fromkeys(allow_primary))
                if allow_primary is not None
                else current.allow_primary
            )
            secondary = (
                tuple(dict.fromkeys(allow_secondary))
                if allow_secondary is not None
                else current.allow_secondary
            )
            override = SettingsOverride(allow_primary=primary, allow_secondary=secondary)
            # Validate through the same parser a read will use: an empty
            # primary allowlist is legal but must be deliberate, so it goes
            # through canonical JSON and back.
            canonical = canonical_override_json(override)
            recheck = parse_settings_json(canonical)
            if recheck.allow_primary is None and recheck.allow_secondary is None:
                canonical = None
        except SettingsError as exc:
            raise StorageError(str(exc)) from exc
        with self.session() as session:
            settings = self.get_settings(session)
            settings.watch_defaults_json = canonical
            settings.updated_at = utcnow()
            session.add(settings)
            session.commit()
        return parse_settings_json(canonical)

    def get_artist_settings(self, artist_key: str) -> SettingsOverride:
        """One Plex artist's stored override layer (empty when none).

        Raises:
            StorageError: the artist row does not exist, or its blob fails
                validation.
        """
        with self.session() as session:
            row = session.exec(select(Artist).where(Artist.plex_rating_key == artist_key)).first()
        if row is None:
            raise StorageError(f"no artist exists for key {artist_key!r}")
        try:
            return parse_settings_json(row.settings_json)
        except SettingsError as exc:
            raise StorageError(
                f"stored watch settings for {artist_key!r} are invalid: {exc}"
            ) from exc

    def set_artist_settings(self, artist_key: str, override: SettingsOverride) -> SettingsOverride:
        """Persist one artist's override layer (canonicalized; empty erases).

        The caller supplies the *complete* new layer — partial updates are
        the CLI's job (read-modify-write against `get_artist_settings`), so
        storage stays a dumb, honest writer.

        Raises:
            StorageError: the artist row does not exist or the override
                fails validation.
        """
        try:
            canonical = canonical_override_json(override)
            if canonical is not None:
                # Round-trip through the parser so nothing is stored that a
                # read would reject.
                canonical_override_json(parse_settings_json(canonical))
        except SettingsError as exc:
            raise StorageError(str(exc)) from exc
        with self.session() as session:
            row = session.exec(select(Artist).where(Artist.plex_rating_key == artist_key)).first()
            if row is None:
                raise StorageError(f"no artist exists for key {artist_key!r}")
            row.settings_json = canonical
            session.add(row)
            session.commit()
        return parse_settings_json(canonical)

    @staticmethod
    def _fold_owner_policies(
        candidates: Sequence[ArtistWatchSettings], today: date
    ) -> ArtistWatchSettings:
        """Fold several owners' resolved policies into one identity's policy.

        Types: most-permissive owner wins (union of allowlists). Muting: on
        only when *every* owner is muted. Priority: ``instant`` beats
        ``digest`` beats ``normal``.
        """
        merged_primary: frozenset[str] = frozenset()
        merged_secondary: frozenset[str] = frozenset()
        for candidate in candidates:
            merged_primary |= candidate.allow_primary
            merged_secondary |= candidate.allow_secondary
        muted = all(candidate.is_muted_on(today) for candidate in candidates)
        priority = PRIORITY_NORMAL
        for candidate in candidates:
            if candidate.priority == PRIORITY_INSTANT:
                priority = PRIORITY_INSTANT
                break
            if candidate.priority == PRIORITY_DIGEST:
                priority = PRIORITY_DIGEST
        return ArtistWatchSettings(
            allow_primary=merged_primary,
            allow_secondary=merged_secondary,
            muted=muted,
            mute_until=None,
            priority=priority,
        )

    def effective_watch_settings_for_mbids(
        self, artist_mbids: Sequence[str]
    ) -> dict[str, ArtistWatchSettings]:
        """Resolve effective settings for many artist MBIDs in one pass.

        One MBID can be owned by several Plex rows (a duplicate library
        entry matched to the same identity), so each MBID's policy folds
        its owners' layers together — see `_fold_owner_policies` for the
        per-field rules. Tombstoned rows own nothing (they are unwatched);
        owners with no override resolve through the global defaults like
        anyone else. Unknown MBIDs come back absent from the mapping, which
        callers treat as "fully default".
        """
        today = utcnow().date()
        unique_ids = list(dict.fromkeys(artist_mbids))
        if not unique_ids:
            return {}
        defaults = self.get_watch_defaults()
        with self.session() as session:
            matches = session.exec(
                select(ArtistMatch).where(
                    col(ArtistMatch.mbid).in_(unique_ids),
                    ArtistMatch.status.in_(("auto", "manual")),  # type: ignore[attr-defined]
                )
            ).all()
            keys_by_mbid: dict[str, list[str]] = {}
            for match in matches:
                if match.mbid is None:
                    continue
                keys_by_mbid.setdefault(match.mbid, []).append(match.artist_key)
            live_rows = {
                row.plex_rating_key: row
                for row in session.exec(
                    select(Artist).where(
                        col(Artist.plex_rating_key).in_(
                            {key for keys in keys_by_mbid.values() for key in keys}
                        )
                    )
                ).all()
                if row.removed_at is None
            }
        resolved: dict[str, ArtistWatchSettings] = {}
        for mbid in unique_ids:
            owner_keys = [key for key in keys_by_mbid.get(mbid, []) if key in live_rows]
            if not owner_keys:
                continue
            candidates: list[ArtistWatchSettings] = []
            for key in owner_keys:
                try:
                    layer = parse_settings_json(live_rows[key].settings_json)
                except SettingsError as exc:
                    raise StorageError(f"stored watch settings are invalid: {exc}") from exc
                candidates.append(resolve_effective(defaults, layer))
            resolved[mbid] = self._fold_owner_policies(candidates, today)
        return resolved

    def effective_watch_settings(self, artist_mbid: str) -> ArtistWatchSettings | None:
        """Single-MBID convenience over `effective_watch_settings_for_mbids`."""
        return self.effective_watch_settings_for_mbids([artist_mbid]).get(artist_mbid)

    def list_artist_directory(self) -> list[tuple[Artist, str | None]]:
        """Every artist row (tombstones included) with its match status, if any.

        The `encore artists` directory view's read model: one row per Plex
        entry — the entity users think about and configure — joined to its
        identity decision so the listing can show *and* configure in the
        same vocabulary. Tombstoned rows are included (their settings
        survive a temporary removal, like their matches do).
        """
        with self.session() as session:
            statuses = {
                match.artist_key: match.status for match in session.exec(select(ArtistMatch)).all()
            }
            rows = list(session.exec(select(Artist).order_by(col(Artist.name))).all())
        return [(row, statuses.get(row.plex_rating_key)) for row in rows]

    def listening_weights(self) -> dict[str, float]:
        """F9 listening weight per live artist: plays normalized to 0..1.

        The most-played artist weighs 1.0 and everyone else scales against
        them; an artist with zero plays (or an entirely play-free library)
        weighs 0.0, so consumers can degrade to unweighted behavior by
        checking for an all-zero mapping rather than re-deriving it.
        Purely local computation over counts the sync already fetched.
        """
        with self.session() as session:
            artists = session.exec(select(Artist)).all()
        live = [row for row in artists if row.removed_at is None]
        top = max((row.play_count for row in live), default=0)
        if top <= 0:
            return {row.plex_rating_key: 0.0 for row in live}
        return {row.plex_rating_key: row.play_count / top for row in live}

    # -- recommendations (F7) ---------------------------------------------------

    def watched_seed_weights(self) -> dict[str, float]:
        """Watched artist MBIDs with F9 listening weights, for the rec seeder.

        Joins matched identities to their Plex rows' play counts and
        normalizes across the watched set. An entirely play-free set
        degrades to equal weights of 1.0 — unweighted seeding, the F9
        acceptance rule — rather than a useless all-zero map.
        """
        with self.session() as session:
            pairs = session.exec(
                select(ArtistMatch.mbid, Artist.play_count)
                .join(Artist, Artist.plex_rating_key == ArtistMatch.artist_key)  # type: ignore[arg-type]
                .where(
                    ArtistMatch.status.in_(("auto", "manual")),  # type: ignore[attr-defined]
                    ArtistMatch.mbid.is_not(None),  # type: ignore[union-attr]
                    Artist.removed_at.is_(None),  # type: ignore[union-attr]
                )
            ).all()
        weights: dict[str, float] = {}
        for mbid, play_count in pairs:
            if mbid is None:
                continue
            weights[mbid] = float(play_count or 0)
        top = max(weights.values(), default=0.0)
        if top <= 0:
            return {mbid: 1.0 for mbid in weights}
        return {mbid: weight / top for mbid, weight in weights.items()}

    @staticmethod
    def _require_recommendation(session: Session, mbid: str) -> Recommendation:
        """Fetch one recommendation by MBID inside an open session, or raise."""
        row = session.exec(select(Recommendation).where(Recommendation.mbid == mbid)).first()
        if row is None:
            # MBIDs are taste data — never echoed into error strings.
            raise StorageError("no recommendation exists for that MBID")
        return row

    def upsert_recommendations(self, rows: Sequence[Recommendation]) -> int:
        """Write a refresh's candidates; sticky statuses survive untouched.

        A ``new`` row is updated in place (score, provenance, name); an
        absent candidate is inserted; a ``dismissed`` or ``promoted`` row
        is *not* modified — a user decision outlives any recomputation,
        which is what makes dismissals worth having.
        """
        written = 0
        now = utcnow()
        with self.session() as session:
            for candidate in rows:
                existing = session.exec(
                    select(Recommendation).where(Recommendation.mbid == candidate.mbid)
                ).first()
                if existing is not None and existing.status != "new":
                    continue
                if existing is None:
                    session.add(
                        Recommendation(
                            mbid=candidate.mbid,
                            name=candidate.name,
                            comment=candidate.comment,
                            score=candidate.score,
                            provenance_json=candidate.provenance_json,
                            status="new",
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    existing.name = candidate.name
                    existing.comment = candidate.comment
                    existing.score = candidate.score
                    existing.provenance_json = candidate.provenance_json
                    existing.updated_at = now
                    session.add(existing)
                written += 1
            session.commit()
        return written

    def list_recommendations(self, status: str = "new", limit: int = 50) -> list[Recommendation]:
        """Recommendations in one status, best first."""
        if status not in RECOMMENDATION_STATUSES:
            raise StorageError(
                f"invalid recommendation status {status!r}; expected {RECOMMENDATION_STATUSES}"
            )
        with self.session() as session:
            statement = (
                select(Recommendation)
                .where(Recommendation.status == status)
                .order_by(col(Recommendation.score).desc())
            )
            return list(session.exec(statement).all()[:limit])

    def set_recommendation_status(self, mbid: str, status: str) -> Recommendation:
        """Pin a user decision on one candidate.

        Raises:
            StorageError: no such candidate or an unknown status.
        """
        if status not in ("dismissed", "promoted"):
            raise StorageError(f"cannot set recommendation status to {status!r}")
        with self.session() as session:
            row = self._require_recommendation(session, mbid)
            row.status = status
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
            session.refresh(row)
        return row

    def match_names_by_mbids(self, mbids: Sequence[str]) -> dict[str, str]:
        """Display names for owned artist MBIDs (provenance rendering)."""
        unique_ids = list(dict.fromkeys(mbids))
        if not unique_ids:
            return {}
        with self.session() as session:
            rows = session.exec(
                select(ArtistMatch).where(
                    col(ArtistMatch.mbid).in_(unique_ids),
                )
            ).all()
        return {row.mbid: row.artist_name for row in rows if row.mbid is not None}

    # -- standing feeds (F5) ---------------------------------------------------

    def get_feed_token(self) -> str | None:
        """Return the feed token, or ``None`` if none has been minted yet.

        Callers must treat the result as a credential: it is the entire
        access control on the F5 feed routes, so it is never logged and only
        printed by the CLI surface whose job is handing it to the user.

        Raises:
            SecretDecryptionError: the key file does not match the ciphertext.
        """
        with self.session() as session:
            settings = self.get_settings(session)
            if settings.feed_token_cipher is None:
                return None
            return self.cipher.decrypt(settings.feed_token_cipher)

    def ensure_feed_token(self) -> str:
        """Return the feed token, minting one on first use (encrypted at rest)."""
        existing = self.get_feed_token()
        if existing is not None:
            return existing
        return self.rotate_feed_token()

    def rotate_feed_token(self) -> str:
        """Replace the feed token with a fresh one — every old feed URL dies now.

        This is the revocation story the audits promise (residual-risk RR-06):
        a feed URL pasted somewhere regrettable stops working the moment the
        operator rotates. Revocation is all-or-nothing by design — one token
        gates both feeds and every subscriber (RR-07, docs/adr/0013).
        """
        token = secrets.token_urlsafe(32)
        with self.session() as session:
            settings = self.get_settings(session)
            settings.feed_token_cipher = self.cipher.encrypt(token)
            settings.updated_at = utcnow()
            session.add(settings)
            session.commit()
        return token

    def _upcoming_owned_rows(
        self, session: Session, today: date
    ) -> list[tuple[ReleaseGroup, str]]:
        """(group, display name) for owned artists' day-precision future groups."""
        statement = (
            select(ReleaseGroup, ArtistMatch)
            .join(
                ArtistMatch,
                ArtistMatch.mbid == ReleaseGroup.artist_mbid,  # type: ignore[arg-type]
            )
            .join(Artist, Artist.plex_rating_key == ArtistMatch.artist_key)  # type: ignore[arg-type]
            .where(
                ArtistMatch.status.in_(("auto", "manual")),  # type: ignore[attr-defined]
                Artist.removed_at.is_(None),  # type: ignore[union-attr]
                func.length(ReleaseGroup.first_release_date) == 10,
                ReleaseGroup.first_release_date >= today.isoformat(),
            )
        )
        return [
            (group, match.artist_name) for group, match in session.exec(statement).all()
        ]

    def _upcoming_promoted_rows(
        self, session: Session, today: date
    ) -> list[tuple[ReleaseGroup, str]]:
        """(group, candidate name) for promoted candidates' future groups (F8).

        Promoted candidates have no Plex row to join through — the
        recommendation row *is* the identity here, and its name is the
        display name. An inner join against ``artist_matches`` would drop
        these rows from the calendar entirely.
        """
        statement = select(ReleaseGroup, Recommendation).join(
            Recommendation,
            Recommendation.mbid == ReleaseGroup.artist_mbid,  # type: ignore[arg-type]
        ).where(
            Recommendation.status == "promoted",
            func.length(ReleaseGroup.first_release_date) == 10,
            ReleaseGroup.first_release_date >= today.isoformat(),
        )
        return [(group, rec.name) for group, rec in session.exec(statement).all()]

    def list_upcoming_releases(self, today: date | None = None) -> list[UpcomingReleaseView]:
        """Announced releases from today forward, for the iCal feed (F5).

        Only **day-precision** dates qualify: a bare ``2027`` or ``2027-03``
        announcement cannot become a calendar entry without inventing a day
        MusicBrainz did not publish (the same no-invented-precision rule the
        F4 renderer follows), so partial dates stay in the RSS feed only.
        Scope matches the watch list: matched artists still present in Plex,
        plus promoted recommendation candidates (F8) — an unwatched artist's
        stored announcements drop off the calendar.
        Full ISO dates compare correctly as text, so the cut-off is SQL;
        rows are ordered by date then title.
        """
        if today is None:
            today = utcnow().date()
        with self.session() as session:
            rows = self._upcoming_owned_rows(session, today) + self._upcoming_promoted_rows(
                session, today
            )
        views_by_group_mbid: dict[str, UpcomingReleaseView] = {}
        for group, artist_name in rows:
            if group.mbid in views_by_group_mbid:
                # Two Plex rows matched to one MBID (e.g. a duplicate
                # library entry) must not duplicate the calendar entry.
                continue
            try:
                date.fromisoformat(group.first_release_date)
            except ValueError:  # pragma: no cover - MB dates are ISO; belt and braces
                continue
            secondary = (
                tuple(json.loads(group.secondary_types_json))
                if group.secondary_types_json
                else ()
            )
            views_by_group_mbid[group.mbid] = UpcomingReleaseView(
                release_group_mbid=group.mbid,
                title=group.title,
                primary_type=group.primary_type,
                secondary_types=secondary,
                first_release_date=group.first_release_date,
                artist_mbid=group.artist_mbid,
                artist_name=artist_name,
            )
        return sorted(
            views_by_group_mbid.values(),
            key=lambda view: (view.first_release_date, view.title),
        )

    # -- the read model shared by notifications, the in-app feed, and F5 -------

    def list_event_views(self, limit: int = 50) -> list[EventView]:
        """Return the newest events joined to release-group and artist display data."""
        with self.session() as session:
            events = session.exec(
                select(ReleaseEvent).order_by(col(ReleaseEvent.created_at).desc()).limit(limit)
            ).all()
            return self._build_event_views(session, list(events))

    def event_views_for(self, event_ids: Sequence[int]) -> dict[int, EventView]:
        """Return read models for specific event ids, keyed by id."""
        ids = list(dict.fromkeys(event_ids))
        if not ids:
            return {}
        with self.session() as session:
            events = session.exec(select(ReleaseEvent).where(col(ReleaseEvent.id).in_(ids))).all()
            views = self._build_event_views(session, list(events))
        return {view.event_id: view for view in views}

    @staticmethod
    def _build_event_views(session: Session, events: Sequence[ReleaseEvent]) -> list[EventView]:
        """Assemble `EventView` rows with bounded lookups, no ORM joins."""
        if not events:
            return []
        group_ids = {event.release_group_id for event in events}
        groups = {
            group.id: group
            for group in session.exec(
                select(ReleaseGroup).where(col(ReleaseGroup.id).in_(group_ids))
            ).all()
        }
        artist_mbids = {group.artist_mbid for group in groups.values()}
        matches = {
            match.mbid: match
            for match in session.exec(
                select(ArtistMatch).where(col(ArtistMatch.mbid).in_(artist_mbids))
            ).all()
            if match.mbid is not None
        }
        # F8: promoted candidates have no ArtistMatch row — their display
        # name comes from the recommendation itself.
        candidate_names = {
            rec.mbid: rec.name
            for rec in session.exec(
                select(Recommendation).where(
                    col(Recommendation.mbid).in_(
                        artist_mbids - set(matches)
                    )
                )
            ).all()
        }
        views: list[EventView] = []
        for row in events:
            group = groups.get(row.release_group_id)
            if group is None or row.id is None:  # pragma: no cover - FK guarantees the group
                continue
            match = matches.get(group.artist_mbid)
            secondary = (
                tuple(json.loads(group.secondary_types_json)) if group.secondary_types_json else ()
            )
            views.append(
                EventView(
                    event_id=row.id,
                    kind=row.kind,
                    created_at=row.created_at,
                    release_group_mbid=group.mbid,
                    title=group.title,
                    primary_type=group.primary_type,
                    secondary_types=secondary,
                    first_release_date=group.first_release_date,
                    artist_mbid=group.artist_mbid,
                    artist_name=match.artist_name
                    if match is not None
                    else candidate_names.get(group.artist_mbid, ""),
                    plex_rating_key=match.artist_key if match is not None else None,
                )
            )
        return views
