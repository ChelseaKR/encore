"""Storage-layer tests (F0): data-dir resolution, migrations, WAL, key file."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from encore.storage import (
    DATA_DIR_ENV,
    DB_FILENAME,
    KEY_FILENAME,
    MIGRATIONS,
    Storage,
    StorageError,
    resolve_data_dir,
)


def test_init_from_empty_dir_creates_db_key_and_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    assert not data_dir.exists()

    storage = Storage(data_dir)

    assert (data_dir / DB_FILENAME).is_file()
    key_path = data_dir / KEY_FILENAME
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    with storage.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == len(MIGRATIONS)
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    storage.close()


def test_reopen_existing_dir_is_idempotent_and_persists(tmp_path: Path) -> None:
    first = Storage(tmp_path)
    first.set_plex_credentials("http://plex.local:32400", "token-abc")
    first.close()

    second = Storage(tmp_path)
    assert second.get_plex_credentials() == ("http://plex.local:32400", "token-abc")
    with second.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == len(MIGRATIONS)
    second.close()


def test_v6_release_groups_migrate_to_per_artist_uniqueness(tmp_path: Path) -> None:
    # Rebuild the v6 shape by hand — globally-unique mbid, no composite
    # index — then reopen: migration v7 must relax mbid and add the
    # per-(artist, group) unique index, so a release-group credited to two
    # watched artists can hold one row per artist.
    storage = Storage(tmp_path)
    storage.add_release_group(
        artist_mbid="mb-artist-1",
        mbid="rg-shared",
        title="Live Split",
        primary_type="EP",
        secondary_types=("Live",),
        first_release_date="2023-11-02",
    )
    with storage.engine.connect() as connection:
        connection.exec_driver_sql("DROP INDEX uq_release_group_artist_group")
        connection.exec_driver_sql("DROP INDEX ix_release_groups_mbid")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_release_groups_mbid ON release_groups (mbid)"
        )
        connection.exec_driver_sql("PRAGMA user_version = 6")
        connection.commit()
    storage.close()

    reopened = Storage(tmp_path)
    reopened.add_release_group(
        artist_mbid="mb-artist-2",
        mbid="rg-shared",
        title="Live Split",
        primary_type="EP",
        secondary_types=("Live",),
        first_release_date="2023-11-02",
    )
    assert [row.mbid for row in reopened.list_release_groups("mb-artist-1")] == ["rg-shared"]
    assert [row.mbid for row in reopened.list_release_groups("mb-artist-2")] == ["rg-shared"]
    with reopened.engine.connect() as connection:
        indexes = {
            row[1]: bool(row[2])
            for row in connection.exec_driver_sql("PRAGMA index_list(release_groups)")
        }
    assert indexes["uq_release_group_artist_group"] is True
    assert indexes["ix_release_groups_mbid"] is False
    reopened.close()


def test_newer_schema_than_build_refuses_to_open(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    with storage.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA user_version = 999")
        connection.commit()
    storage.close()

    with pytest.raises(StorageError, match="upgrade encore"):
        Storage(tmp_path)


def test_resolve_data_dir_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_data_dir(tmp_path / "explicit") == tmp_path / "explicit"
    assert resolve_data_dir(None) == tmp_path / "from-env"
    monkeypatch.delenv(DATA_DIR_ENV)
    assert resolve_data_dir(None) == Path("data")


def test_data_dir_blocked_by_file_raises_storage_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("this path is occupied by a file")

    with pytest.raises(StorageError, match="cannot create data directory"):
        Storage(blocker)


def test_check_ready_ok_and_settings_row_is_singleton(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.check_ready()  # must not raise

    with storage.session() as session:
        first = storage.get_settings(session)
        second = storage.get_settings(session)
        assert first.id == second.id == 1
    storage.close()
