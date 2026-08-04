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

from encore.models import (
    CHANNEL_MODES,
    DELIVERY_STATUSES,
    MATCH_STATUSES,
    RELEASE_EVENT_KINDS,
    SETTINGS_ROW_ID,
    AppSettings,
    Artist,
    ArtistMatch,
    Delivery,
    EventView,
    NotificationChannel,
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


# Ordered forward migrations; index+1 is the schema version they produce.
# Append-only: released migrations are never edited, only extended.
MIGRATIONS: tuple[Callable[[Connection], None], ...] = (
    _migration_0001_initial_schema,
    _migration_0002_artists_and_library_selection,
    _migration_0003_artist_matches,
    _migration_0004_release_watching,
    _migration_0005_notifications,
    _migration_0006_feed_token,
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

    def ensure_deliveries(self, now: datetime | None = None) -> int:
        """Materialize missing (event, channel) delivery rows; return how many.

        Only events created *after* a channel was added fan out to it. Adding
        a channel therefore never replays history — the same
        don't-flood-on-first-contact rule the F3 baseline applies to a newly
        watched artist (docs/adr/0011), applied to a newly added channel.

        ``now`` stamps ``next_attempt_at`` so a row created during a cycle is
        due *in* that cycle; without it a brand-new delivery would be a few
        microseconds in the future and wait a whole interval for nothing.
        """
        if now is None:
            now = utcnow()
        created = 0
        with self.session() as session:
            channels = session.exec(
                select(NotificationChannel).where(NotificationChannel.enabled)
            ).all()
            if not channels:
                return 0
            events = session.exec(
                select(ReleaseEvent).where(col(ReleaseEvent.notified_at).is_(None))
            ).all()
            if not events:
                return 0
            event_ids = [event.id for event in events if event.id is not None]
            existing = {
                (delivery.event_id, delivery.channel_id)
                for delivery in session.exec(
                    select(Delivery).where(col(Delivery.event_id).in_(event_ids))
                ).all()
            }
            for event in events:
                if event.id is None:  # pragma: no cover - persisted rows always have one
                    continue
                for channel in channels:
                    if channel.id is None:  # pragma: no cover - same
                        continue
                    if event.created_at < channel.created_at:
                        continue
                    if (event.id, channel.id) in existing:
                        continue
                    session.add(
                        Delivery(event_id=event.id, channel_id=channel.id, next_attempt_at=now)
                    )
                    created += 1
            session.commit()
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

    def list_upcoming_releases(self, today: date | None = None) -> list[UpcomingReleaseView]:
        """Announced releases from today forward, for the iCal feed (F5).

        Only **day-precision** dates qualify: a bare ``2027`` or ``2027-03``
        announcement cannot become a calendar entry without inventing a day
        MusicBrainz did not publish (the same no-invented-precision rule the
        F4 renderer follows), so partial dates stay in the RSS feed only.
        Scope matches the watch list: matched artists still present in Plex —
        an unwatched artist's stored announcements drop off the calendar.
        Full ISO dates compare correctly as text, so the cut-off is SQL;
        rows are ordered by date then title.
        """
        if today is None:
            today = utcnow().date()
        with self.session() as session:
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
                .order_by(col(ReleaseGroup.first_release_date), col(ReleaseGroup.title))
            )
            views: list[UpcomingReleaseView] = []
            seen_mbids: set[str] = set()
            for group, match in session.exec(statement).all():
                if group.mbid in seen_mbids:
                    # Two Plex rows matched to one MBID (e.g. a duplicate
                    # library entry) must not duplicate the calendar entry.
                    continue
                seen_mbids.add(group.mbid)
                try:
                    date.fromisoformat(group.first_release_date)
                except ValueError:  # pragma: no cover - MB dates are ISO; belt and braces
                    continue
                secondary = (
                    tuple(json.loads(group.secondary_types_json))
                    if group.secondary_types_json
                    else ()
                )
                views.append(
                    UpcomingReleaseView(
                        release_group_mbid=group.mbid,
                        title=group.title,
                        primary_type=group.primary_type,
                        secondary_types=secondary,
                        first_release_date=group.first_release_date,
                        artist_mbid=group.artist_mbid,
                        artist_name=match.artist_name,
                    )
                )
            return views

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
        """Assemble `EventView` rows with three bounded lookups, no ORM joins."""
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
                    artist_name=match.artist_name if match is not None else "",
                    plex_rating_key=match.artist_key if match is not None else None,
                )
            )
        return views
