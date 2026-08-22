"""Storage-layer tests (F0): data-dir resolution, migrations, WAL, key file."""

from __future__ import annotations

import stat
from datetime import date
from pathlib import Path

import pytest
from sqlmodel import select

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


# -- watch settings (F10) ------------------------------------------------------


def _seed_owned_artist(
    storage: Storage, rating_key: str, name: str, mbid: str, removed: bool = False
) -> None:
    from datetime import UTC, datetime

    from encore.models import Artist

    with storage.session() as session:
        session.add(Artist(plex_rating_key=rating_key, name=name, library_key="1"))
        if removed:
            row = session.exec(select(Artist).where(Artist.plex_rating_key == rating_key)).first()
            assert row is not None
            row.removed_at = datetime.now(UTC)
        session.commit()
    storage.save_artist_match(rating_key, name, "auto", mbid=mbid)


def test_watch_default_types_round_trip_and_merge(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    assert storage.get_watch_defaults().is_empty()

    storage.set_watch_default_types(allow_primary=("album",), allow_secondary=())
    defaults = storage.get_watch_defaults()
    assert defaults.allow_primary == ("album",)
    assert defaults.allow_secondary == ()

    # Omitted lists are left as stored; a new list replaces wholesale.
    storage.set_watch_default_types(allow_secondary=("live",))
    defaults = storage.get_watch_defaults()
    assert defaults.allow_primary == ("album",)
    assert defaults.allow_secondary == ("live",)

    with pytest.raises(StorageError):
        storage.set_watch_default_types(allow_primary=("boxset",))
    storage.close()


def test_artist_settings_round_trip_requires_a_real_row(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    _seed_owned_artist(storage, "key-1", "Low", "mbid-low")
    from encore.artistsettings import SettingsOverride

    stored = storage.set_artist_settings("key-1", SettingsOverride(muted=True))
    assert stored.muted is True
    again = storage.get_artist_settings("key-1")
    assert again == stored

    with pytest.raises(StorageError):
        storage.get_artist_settings("missing-key")
    with pytest.raises(StorageError):
        storage.set_artist_settings("missing-key", SettingsOverride(muted=True))
    storage.close()


def test_effective_settings_aggregate_across_owners_of_one_mbid(tmp_path: Path) -> None:
    # Two Plex rows matched to one MusicBrainz identity (a duplicate entry).
    from encore.artistsettings import SettingsOverride

    storage = Storage(tmp_path)
    _seed_owned_artist(storage, "key-a", "Low A", "mbid-low")
    _seed_owned_artist(storage, "key-b", "Low B", "mbid-low")

    # Types: most-permissive owner wins.
    storage.set_artist_settings("key-a", SettingsOverride(allow_primary=("album",)))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None
    assert "album" in resolved.allow_primary
    storage.set_artist_settings("key-b", SettingsOverride(allow_primary=("album", "ep")))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None
    assert resolved.allow_primary == frozenset({"album", "ep"})

    # Muting: suppressed only when *every* owner is muted.
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None and not resolved.is_muted_on(date.today())
    storage.set_artist_settings("key-a", SettingsOverride(muted=True))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None and not resolved.is_muted_on(date.today())
    storage.set_artist_settings("key-b", SettingsOverride(muted=True))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None and resolved.is_muted_on(date.today())

    # Priority: instant beats digest beats normal.
    storage.set_artist_settings("key-b", SettingsOverride(priority="digest"))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None and resolved.priority == "digest"
    storage.set_artist_settings("key-b", SettingsOverride(priority="instant"))
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None and resolved.priority == "instant"

    # Tombstoned owners own nothing: unmuting via removal is immediate.
    from datetime import UTC, datetime

    from sqlmodel import col
    from sqlmodel import select as sel

    from encore.models import Artist

    with storage.session() as session:
        row = session.exec(sel(Artist).where(col(Artist.plex_rating_key) == "key-a")).first()
        assert row is not None
        row.removed_at = datetime.now(UTC)
        session.add(row)
        session.commit()
    resolved = storage.effective_watch_settings("mbid-low")
    assert resolved is not None
    storage.close()


def test_ensure_deliveries_settles_muted_events_without_creating_rows(tmp_path: Path) -> None:
    from encore.artistsettings import SettingsOverride
    from tests.notify_fixtures import seed_event

    storage = Storage(tmp_path)
    storage.add_channel("phone", "ntfy://user:pass@ntfy.example/encore")
    seed_event(storage, rating_key="1", artist_mbid="mbid-muted-artist")
    seed_event(storage, rating_key="2", artist_mbid="mbid-loud-artist")
    storage.set_artist_settings("1", SettingsOverride(muted=True))

    created, muted_skipped = storage.ensure_deliveries()

    assert created > 0
    assert muted_skipped == 1
    events = {event.id: event for event in storage.list_events()}
    muted_event, loud_event = events[1], events[2]
    assert muted_event.notified_at is not None  # settled-without-delivery
    deliveries = []
    with storage.session() as session:
        from encore.models import Delivery

        deliveries = list(session.exec(select(Delivery)).all())
    assert {d.event_id for d in deliveries} == {loud_event.id}
    storage.close()


def test_an_expired_mute_stops_suppressing_but_never_replays_history(tmp_path: Path) -> None:
    from datetime import timedelta

    from encore.artistsettings import SettingsOverride
    from tests.notify_fixtures import seed_event

    storage = Storage(tmp_path)
    storage.add_channel("phone", "ntfy://user:pass@ntfy.example/encore")
    seed_event(storage, rating_key="1", artist_mbid="mbid-x")
    yesterday = date.today() - timedelta(days=1)
    storage.set_artist_settings("1", SettingsOverride(mute_until=yesterday))

    created, muted_skipped = storage.ensure_deliveries()
    assert created >= 0
    # The mute had already expired when this event fanned out, so it is
    # delivered normally...
    assert muted_skipped == 0

    # ...and a *fresh* mute that has expired never resurrects old events,
    # because suppression stamps them settled the day they were skipped.
    seed_event(
        storage,
        rating_key="2",
        artist_mbid="mbid-y",
        group_mbid="ffff0000-bbbb-cccc-dddd-eeeeeeeeeeee",
    )
    storage.set_artist_settings("2", SettingsOverride(mute_until=yesterday))
    created, muted_skipped = storage.ensure_deliveries()
    assert muted_skipped == 0
    storage.close()


def test_a_muted_artist_unmuted_later_does_not_replay_the_muted_release(tmp_path: Path) -> None:
    from encore.artistsettings import SettingsOverride
    from tests.notify_fixtures import seed_event

    storage = Storage(tmp_path)
    storage.add_channel("phone", "ntfy://user:pass@ntfy.example/encore")
    seed_event(storage, rating_key="1", artist_mbid="mbid-z")
    storage.set_artist_settings("1", SettingsOverride(muted=True))
    created, muted_skipped = storage.ensure_deliveries()
    assert muted_skipped == 1 and created == 0

    storage.set_artist_settings("1", SettingsOverride(muted=False))
    created, muted_skipped = storage.ensure_deliveries()
    assert created == 0  # the release stays history; only future ones page
    assert muted_skipped == 0
    storage.close()
