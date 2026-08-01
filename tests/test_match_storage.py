"""Storage tests for the F2 artist-match table and the v1→v3 migration path."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import select

from encore.models import ArtistMatch
from encore.storage import MIGRATIONS, Storage, StorageError


def test_save_and_get_artist_match_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    saved = storage.save_artist_match(
        artist_key="key-1",
        artist_name="Radiohead",
        status="auto",
        mbid="mb-radiohead",
        confidence=0.97,
        candidates_json='[{"mbid": "mb-radiohead", "name": "Radiohead", "score": 0.97}]',
    )
    assert saved.id is not None

    fetched = storage.get_artist_match("key-1")
    assert fetched is not None
    assert fetched.status == "auto"
    assert fetched.mbid == "mb-radiohead"
    assert fetched.confidence == 0.97
    assert storage.get_artist_match("key-unknown") is None
    storage.close()


def test_save_artist_match_upserts_in_place(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    first = storage.save_artist_match("key-1", "Bush", "pending")
    second = storage.save_artist_match("key-1", "Bush", "auto", mbid="mb-bush-gb", confidence=1.0)
    assert second.id == first.id  # same row, overwritten — not a duplicate
    with storage.session() as session:
        rows = session.exec(select(ArtistMatch)).all()
        assert len(rows) == 1
    storage.close()


def test_save_artist_match_rejects_unknown_status(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    with pytest.raises(StorageError, match="invalid match status"):
        storage.save_artist_match("key-1", "Bush", "definitely-not-a-status")
    storage.close()


def test_review_queue_lists_only_pending_oldest_first(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.save_artist_match("key-a", "Artist A", "auto", mbid="mb-a")
    storage.save_artist_match("key-b", "Artist B", "pending")
    storage.save_artist_match("key-c", "Artist C", "pending")
    storage.save_artist_match("key-d", "Artist D", "skipped")
    # Force distinct created_at values so the ordering claim is actually tested.
    with storage.session() as session:
        for row in session.exec(select(ArtistMatch)).all():
            if row.artist_key == "key-c":
                row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
                session.add(row)
        session.commit()

    queue = storage.list_review_queue()
    assert [row.artist_key for row in queue] == ["key-c", "key-b"]
    storage.close()


def test_resolve_and_skip_transitions(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.save_artist_match("key-1", "John Williams", "pending", confidence=0.7)

    resolved = storage.resolve_artist_match("key-1", "mb-jw-guitarist")
    assert resolved.status == "manual"
    assert resolved.mbid == "mb-jw-guitarist"
    assert resolved.confidence is None  # a human decision, not a scored one

    skipped = storage.skip_artist_match("key-1")
    assert skipped.status == "skipped"
    assert skipped.mbid is None

    with pytest.raises(StorageError, match="no artist match row"):
        storage.resolve_artist_match("key-missing", "mb-x")
    with pytest.raises(StorageError, match="no artist match row"):
        storage.skip_artist_match("key-missing")
    storage.close()


def test_v1_database_upgrades_to_current_preserving_data(tmp_path: Path) -> None:
    # Simulate a database written by the F0-only build: schema v1, no
    # artist_matches table, with real settings data that must survive.
    storage = Storage(tmp_path)
    storage.set_plex_credentials("http://plex.local:32400", "token-abc")
    with storage.engine.connect() as connection:
        connection.exec_driver_sql("DROP TABLE artist_matches")
        connection.exec_driver_sql("PRAGMA user_version = 1")
        connection.commit()
    storage.close()

    upgraded = Storage(tmp_path)
    with upgraded.engine.connect() as connection:
        version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        assert version == len(MIGRATIONS) == 5
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "artist_matches" in tables
    assert upgraded.get_plex_credentials() == ("http://plex.local:32400", "token-abc")
    assert upgraded.get_artist_match("key-any") is None  # table empty but usable
    upgraded.close()
