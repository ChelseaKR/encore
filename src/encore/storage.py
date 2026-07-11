"""The storage layer: SQLite (WAL) via SQLModel — one file, one data directory.

Design (docs/adr/0005 + docs/adr/0008): a single SQLite database in a mounted
volume is the only datastore; a Fernet key file sits beside it and encrypts
the secret-bearing columns (Plex token today; Apprise URLs and feed tokens
when F4/F5 land). Schema changes run as ordered forward migrations tracked in
SQLite's ``PRAGMA user_version`` — there is no down-migration story, matching
the single-operator deployment model (backup = copy the directory).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, event
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine

from encore.models import SETTINGS_ROW_ID, AppSettings, utcnow
from encore.secretstore import SecretCipher

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
    """v1: create the F0 tables (currently just the ``settings`` singleton)."""
    SQLModel.metadata.create_all(connection)


# Ordered forward migrations; index+1 is the schema version they produce.
# Append-only: released migrations are never edited, only extended.
MIGRATIONS: tuple[Callable[[Connection], None], ...] = (_migration_0001_initial_schema,)


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
        self.cipher = SecretCipher.load_or_create(self.data_dir / KEY_FILENAME)
        self.db_path = self.data_dir / DB_FILENAME
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            # The FastAPI threadpool serves requests from worker threads; SQLite
            # connections are pooled per-engine, not per-thread.
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _sqlite_on_connect)
        self._migrate()

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
