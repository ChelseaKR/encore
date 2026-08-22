"""Sync-engine tests (F1): upsert, tombstone, resurrect, guards, selection."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import col, select

from encore.models import Artist, utcnow
from encore.plex import PlexMusicClient
from encore.storage import Storage
from encore.sync import SyncError, sync_artists
from tests.plex_fixtures import FakeArtist, FakeLibrary, make_client_session


def _client(libraries: list[FakeLibrary]) -> PlexMusicClient:
    session, base_url = make_client_session(libraries)
    return PlexMusicClient(base_url, "fixture-token", session=session)


def _all_rows(storage: Storage) -> list[Artist]:
    with storage.session() as session:
        return list(session.exec(select(Artist)).all())


def test_initial_sync_inventories_a_thousand_artists_in_one_run(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    library = FakeLibrary(
        key="1",
        title="Music",
        artists=[FakeArtist(rating_key=str(1000 + i), name=f"Artist {i:04d}") for i in range(1000)],
    )
    report = sync_artists(storage, _client([library]))
    assert report.added == 1000
    assert report.seen == 1000
    assert report.tombstoned == 0
    assert len(_all_rows(storage)) == 1000
    storage.close()


def test_removal_tombstones_on_next_sync_and_resurrects_on_return(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    stays = FakeArtist(rating_key="101", name="Stays")
    leaves = FakeArtist(rating_key="102", name="Leaves")

    sync_artists(storage, _client([FakeLibrary(key="1", title="Music", artists=[stays, leaves])]))

    # Next sync: 102 is gone from Plex -> tombstoned (unwatched), row kept.
    report = sync_artists(storage, _client([FakeLibrary(key="1", title="Music", artists=[stays])]))
    assert report.tombstoned == 1
    rows = {row.plex_rating_key: row for row in _all_rows(storage)}
    assert rows["102"].removed_at is not None
    assert rows["101"].removed_at is None

    # Third sync: 102 is back -> resurrected, same row.
    report = sync_artists(
        storage, _client([FakeLibrary(key="1", title="Music", artists=[stays, leaves])])
    )
    assert report.resurrected == 1
    rows = {row.plex_rating_key: row for row in _all_rows(storage)}
    assert rows["102"].removed_at is None
    assert len(rows) == 2
    storage.close()


def test_rename_updates_in_place(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    sync_artists(
        storage,
        _client([FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "Old Name")])]),
    )
    report = sync_artists(
        storage,
        _client([FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "New Name")])]),
    )
    assert report.updated == 1
    assert report.added == 0
    rows = _all_rows(storage)
    assert len(rows) == 1
    assert rows[0].name == "New Name"
    storage.close()


def test_various_artists_guard_skips_compilations(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    library = FakeLibrary(
        key="1",
        title="Music",
        artists=[
            FakeArtist("101", "Boards of Canada"),
            FakeArtist("102", "Various Artists"),
            FakeArtist("103", "  various artists  "),
        ],
    )
    report = sync_artists(storage, _client([library]))
    assert report.skipped_compilations == 2
    assert report.added == 1
    assert [row.name for row in _all_rows(storage)] == ["Boards of Canada"]
    storage.close()


def test_cross_library_move_updates_in_place_under_partial_selection(tmp_path: Path) -> None:
    """An artist that moved into the synced library must not insert a duplicate.

    ``plex_rating_key`` is unique; the sync must find the existing row even
    when its stored ``library_key`` is outside the current selection, and it
    must never tombstone rows belonging to libraries that were not synced.
    """
    storage = Storage(tmp_path)
    sync_artists(
        storage,
        _client(
            [
                FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "Mover")]),
                FakeLibrary(key="3", title="Vinyl", artists=[FakeArtist("301", "Stays Behind")]),
            ]
        ),
    )

    # 101 moves from library 1 to library 3; only library 3 is synced.
    report = sync_artists(
        storage,
        _client(
            [
                FakeLibrary(key="1", title="Music", artists=[]),
                FakeLibrary(
                    key="3",
                    title="Vinyl",
                    artists=[FakeArtist("301", "Stays Behind"), FakeArtist("101", "Mover")],
                ),
            ]
        ),
        library_keys=["3"],
    )

    assert report.added == 0
    assert report.updated == 1
    assert report.tombstoned == 0
    rows = {row.plex_rating_key: row for row in _all_rows(storage)}
    assert len(rows) == 2
    assert rows["101"].library_key == "3"
    assert rows["101"].removed_at is None
    storage.close()


def test_explicit_library_selection_limits_the_sync(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    libraries = [
        FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "In Selection")]),
        FakeLibrary(key="3", title="Vinyl", artists=[FakeArtist("301", "Not Synced")]),
    ]
    report = sync_artists(storage, _client(libraries), library_keys=["1"])
    assert report.library_keys == ("1",)
    assert [row.name for row in _all_rows(storage)] == ["In Selection"]
    storage.close()


def test_stored_selection_is_used_when_no_explicit_keys(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.set_plex_libraries(["3"])
    libraries = [
        FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "Skipped")]),
        FakeLibrary(key="3", title="Vinyl", artists=[FakeArtist("301", "Synced")]),
    ]
    report = sync_artists(storage, _client(libraries))
    assert report.library_keys == ("3",)
    assert [row.name for row in _all_rows(storage)] == ["Synced"]
    storage.close()


def test_default_syncs_all_music_libraries(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    libraries = [
        FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "A")]),
        FakeLibrary(key="2", title="Movies", type="movie"),
        FakeLibrary(key="3", title="Vinyl", artists=[FakeArtist("301", "B")]),
    ]
    report = sync_artists(storage, _client(libraries))
    assert report.library_keys == ("1", "3")
    assert len(_all_rows(storage)) == 2
    storage.close()


def test_unknown_library_key_raises(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    client = _client([FakeLibrary(key="1", title="Music")])
    with pytest.raises(SyncError, match="not music libraries"):
        sync_artists(storage, client, library_keys=["1", "99"])
    storage.close()


def test_no_music_libraries_raises(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    client = _client([FakeLibrary(key="2", title="Movies", type="movie")])
    with pytest.raises(SyncError, match="no music libraries"):
        sync_artists(storage, client)
    storage.close()


def test_sync_records_the_plex_machine_identifier_for_deep_links(tmp_path: Path) -> None:
    # F4's "open in Plex" line needs the server's machine identifier; the sync
    # already holds a connection, so it is learned there rather than guessed.
    storage = Storage(tmp_path)
    assert storage.get_plex_machine_identifier() is None

    sync_artists(storage, _client([FakeLibrary(key="1", title="Music", artists=[])]))

    assert storage.get_plex_machine_identifier() == "fixture-machine-id"
    storage.close()


def test_sync_carries_play_counts_and_reports_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F9: lifetime plays ride every sync; a changed count is an update.
    from tests.plex_fixtures import FakeArtist, FakeLibrary, make_client_session

    session, base_url = make_client_session(
        [
            FakeLibrary(
                key="1",
                title="Music",
                artists=[
                    FakeArtist("101", "Played A Lot", play_count=142),
                    FakeArtist("102", "Never Played"),
                ],
            )
        ]
    )
    storage = Storage(tmp_path)
    storage.set_plex_credentials(base_url, "fixture-token")
    client = PlexMusicClient(base_url, "fixture-token", session=session)
    monkeypatch.setattr(PlexMusicClient, "__init__", PlexMusicClient.__init__)

    first = sync_artists(storage, client)

    assert first.added == 2
    weights = storage.listening_weights()
    assert weights["101"] == 1.0  # the top-played artist weighs exactly 1
    assert weights["102"] == 0.0

    # Plays went up: an update, and the new truth is stored (even a drop).
    session2, base_url2 = make_client_session(
        [
            FakeLibrary(
                key="1",
                title="Music",
                artists=[FakeArtist("101", "Played A Lot", play_count=150)],
            )
        ]
    )
    client2 = PlexMusicClient(base_url2, "fixture-token", session=session2)
    second = sync_artists(storage, client2)
    assert second.updated == 1
    assert storage.listening_weights()["101"] == 1.0
    storage.close()


def test_listening_weights_degrade_to_all_zero_without_history(tmp_path: Path) -> None:
    from encore.models import Artist

    storage = Storage(tmp_path)
    with storage.session() as session:
        session.add(Artist(plex_rating_key="a", name="A", library_key="1"))
        session.commit()
    assert storage.listening_weights() == {"a": 0.0}
    with storage.session() as session:
        row = session.exec(select(Artist).where(col(Artist.plex_rating_key) == "a")).first()
        assert row is not None
        row.removed_at = utcnow()
        session.add(row)
        session.commit()
    assert storage.listening_weights() == {}  # tombstoned rows own nothing
    storage.close()
