"""F5 storage: the feed token (encrypted, rotatable) and the upcoming-releases query."""

from __future__ import annotations

import datetime as dt
import string
from pathlib import Path

from encore.models import Artist
from encore.storage import DB_FILENAME, MIGRATIONS, Storage

TODAY = dt.date(2026, 8, 1)
ARTIST_MBID = "11111111-2222-3333-4444-555555555555"
ARTIST_NAME = "Sentinel Artist needle-6b2e"
RATING_KEY = "4242"


def _raw_db_bytes(data_dir: Path) -> bytes:
    """Concatenate the database file and any WAL/SHM siblings."""
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"{DB_FILENAME}{suffix}"
        if candidate.exists():
            blob += candidate.read_bytes()
    return blob


def _seed_watched_artist(
    storage: Storage,
    rating_key: str = RATING_KEY,
    name: str = ARTIST_NAME,
    mbid: str = ARTIST_MBID,
    status: str = "auto",
    removed: bool = False,
) -> None:
    with storage.session() as session:
        session.add(
            Artist(
                plex_rating_key=rating_key,
                name=name,
                library_key="1",
                removed_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC) if removed else None,
            )
        )
        session.commit()
    storage.save_artist_match(rating_key, name, status, mbid=mbid, confidence=0.99)


def _seed_group(
    storage: Storage,
    mbid: str,
    date: str,
    title: str = "Sentinel Album needle-9d7a",
    artist_mbid: str = ARTIST_MBID,
    secondary_types: tuple[str, ...] = (),
) -> None:
    storage.add_release_group(
        artist_mbid=artist_mbid,
        mbid=mbid,
        title=title,
        primary_type="Album",
        secondary_types=secondary_types,
        first_release_date=date,
    )


def test_no_token_exists_until_minted(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    assert storage.get_feed_token() is None
    minted = storage.ensure_feed_token()
    assert storage.get_feed_token() == minted
    # Idempotent: asking again must not rotate.
    assert storage.ensure_feed_token() == minted
    storage.close()


def test_token_is_long_and_url_safe(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    token = storage.ensure_feed_token()
    storage.close()
    assert len(token) >= 40
    allowed = set(string.ascii_letters + string.digits + "-_")
    assert set(token) <= allowed


def test_rotate_replaces_the_token(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    first = storage.ensure_feed_token()
    second = storage.rotate_feed_token()
    assert first != second
    assert storage.get_feed_token() == second
    storage.close()


def test_feed_token_never_appears_in_db_file_plaintext(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    token = storage.ensure_feed_token()
    storage.close()
    assert token.encode() not in _raw_db_bytes(tmp_path)


def test_migration_v6_upgrades_a_v5_database(tmp_path: Path) -> None:
    # Simulate a database written by the F4 build: current schema minus the
    # feed-token column, stamped v5.
    storage = Storage(tmp_path)
    storage.close()
    reopened = Storage(tmp_path)
    with reopened.engine.connect() as connection:
        connection.exec_driver_sql("ALTER TABLE settings DROP COLUMN feed_token_cipher")
        connection.exec_driver_sql("PRAGMA user_version = 5")
        connection.commit()
    reopened.close()

    upgraded = Storage(tmp_path)
    with upgraded.engine.connect() as connection:
        version = connection.exec_driver_sql("PRAGMA user_version").scalar_one()
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(settings)")}
    assert version == len(MIGRATIONS)
    assert "feed_token_cipher" in columns
    assert upgraded.get_feed_token() is None
    upgraded.close()


def test_upcoming_includes_today_and_future_full_dates_in_order(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    _seed_watched_artist(storage)
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000001", "2026-09-15", title="Later")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000002", "2026-08-01", title="Today")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000003", "2026-07-31", title="Yesterday")
    upcoming = storage.list_upcoming_releases(today=TODAY)
    storage.close()
    assert [release.title for release in upcoming] == ["Today", "Later"]
    assert upcoming[0].artist_name == ARTIST_NAME
    assert upcoming[0].first_release_date == "2026-08-01"


def test_upcoming_excludes_partial_dates(tmp_path: Path) -> None:
    # A bare year or year-month cannot become a calendar day without
    # inventing precision MusicBrainz did not publish.
    storage = Storage(tmp_path)
    _seed_watched_artist(storage)
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000001", "2027")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000002", "2027-03")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000003", "")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000004", "2027-03-14")
    upcoming = storage.list_upcoming_releases(today=TODAY)
    storage.close()
    assert [release.first_release_date for release in upcoming] == ["2027-03-14"]


def test_upcoming_excludes_unwatched_artists(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    # Tombstoned in Plex: announcements drop off the calendar.
    _seed_watched_artist(
        storage, rating_key="1", mbid="aaaaaaaa-1111-0000-0000-000000000000", removed=True
    )
    _seed_group(
        storage,
        "bbbbbbbb-0000-0000-0000-000000000001",
        "2026-09-01",
        artist_mbid="aaaaaaaa-1111-0000-0000-000000000000",
    )
    # Pending review: not watched, so not on the calendar.
    _seed_watched_artist(
        storage, rating_key="2", mbid="aaaaaaaa-2222-0000-0000-000000000000", status="pending"
    )
    _seed_group(
        storage,
        "bbbbbbbb-0000-0000-0000-000000000002",
        "2026-09-02",
        artist_mbid="aaaaaaaa-2222-0000-0000-000000000000",
    )
    assert storage.list_upcoming_releases(today=TODAY) == []
    storage.close()


def test_upcoming_carries_secondary_types_through_to_the_view(tmp_path: Path) -> None:
    # The JSON-encoded column must come back as the tuple the renderer needs,
    # or every announced live record silently becomes a plain "Album".
    # `live` and `compilation` are opted in explicitly: since issue #34 the
    # calendar honours the F10 type policy, and under the albums-only default
    # this group is correctly absent — which is what this test used to prove
    # was broken without meaning to.
    storage = Storage(tmp_path)
    storage.set_watch_default_types(allow_secondary=("live", "compilation"))
    _seed_watched_artist(storage)
    _seed_group(
        storage,
        "aaaaaaaa-0000-0000-0000-000000000001",
        "2026-09-15",
        secondary_types=("Live", "Compilation"),
    )
    (upcoming,) = storage.list_upcoming_releases(today=TODAY)
    storage.close()
    assert upcoming.secondary_types == ("Live", "Compilation")


def test_upcoming_omits_a_secondary_type_the_policy_excludes(tmp_path: Path) -> None:
    # Companion to the test above and the iCal half of issue #34: with the
    # albums-only default in force, the same Live/Compilation announcement is
    # absent from the calendar rather than silently present.
    storage = Storage(tmp_path)
    _seed_watched_artist(storage)
    _seed_group(
        storage,
        "aaaaaaaa-0000-0000-0000-000000000001",
        "2026-09-15",
        secondary_types=("Live", "Compilation"),
    )
    assert storage.list_upcoming_releases(today=TODAY) == []
    storage.close()


def test_upcoming_deduplicates_a_double_matched_mbid(tmp_path: Path) -> None:
    # Two Plex rows (a duplicate library entry) matched to one MBID must not
    # duplicate the calendar entry.
    storage = Storage(tmp_path)
    _seed_watched_artist(storage, rating_key="1")
    _seed_watched_artist(storage, rating_key="2")
    _seed_group(storage, "aaaaaaaa-0000-0000-0000-000000000001", "2026-09-15")
    upcoming = storage.list_upcoming_releases(today=TODAY)
    storage.close()
    assert len(upcoming) == 1
