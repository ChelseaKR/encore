"""The storage layer: SQLite (WAL) via SQLModel — one file, one data directory.

Design (docs/adr/0005 + docs/adr/0008): a single SQLite database in a mounted
volume is the only datastore; a Fernet key file sits beside it and encrypts
the secret-bearing columns (Plex token today; Apprise URLs and feed tokens
when F4/F5 land). Schema changes run as ordered forward migrations tracked in
SQLite's ``PRAGMA user_version`` — there is no down-migration story, matching
the single-operator deployment model (backup = copy the directory).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine, select

from encore.models import (
    MATCH_STATUSES,
    RELEASE_EVENT_KINDS,
    SETTINGS_ROW_ID,
    AppSettings,
    Artist,
    ArtistMatch,
    ReleaseEvent,
    ReleaseGroup,
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


# Ordered forward migrations; index+1 is the schema version they produce.
# Append-only: released migrations are never edited, only extended.
MIGRATIONS: tuple[Callable[[Connection], None], ...] = (
    _migration_0001_initial_schema,
    _migration_0002_artists_and_library_selection,
    _migration_0003_artist_matches,
    _migration_0004_release_watching,
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

    # -- release watching (F3) -------------------------------------------------

    def list_watched_artist_mbids(self) -> list[str]:
        """Distinct MBIDs to poll: matched artists still present in Plex.

        Joins ``artist_matches`` (status ``auto``/``manual``, non-NULL MBID)
        to ``artists`` on the Plex rating key and excludes tombstoned rows —
        this is what makes "removal unwatches on next sync" (F1 acceptance)
        true without F3 keeping its own bookkeeping.
        """
        with self.session() as session:
            statement = (
                select(ArtistMatch.mbid)
                .join(Artist, Artist.plex_rating_key == ArtistMatch.artist_key)  # type: ignore[arg-type]
                .where(
                    ArtistMatch.status.in_(("auto", "manual")),  # type: ignore[attr-defined]
                    ArtistMatch.mbid.is_not(None),  # type: ignore[union-attr]
                    Artist.removed_at.is_(None),  # type: ignore[union-attr]
                )
                .distinct()
            )
            return [mbid for mbid in session.exec(statement).all() if mbid is not None]

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

    def update_release_group_date(self, mbid: str, first_release_date: str) -> ReleaseGroup:
        """Record a revised first-release date on an already-seen group.

        Raises:
            StorageError: no release-group row exists for ``mbid``.
        """
        with self.session() as session:
            statement = select(ReleaseGroup).where(ReleaseGroup.mbid == mbid)
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
