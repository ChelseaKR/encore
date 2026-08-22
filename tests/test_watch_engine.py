"""Release-watch engine tests (F3): baseline, diffing, events, skip-don't-queue."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from encore.matching.mb import MB_BASE_URL, MusicBrainzClient, RateLimiter
from encore.models import Artist
from encore.storage import Storage, StorageError
from encore.watch import watch_all_artists, watch_artist
from encore.watch.engine import parse_earliest_date
from tests.mb_fixtures import mb_browse_response, mb_release_group

TODAY = dt.date(2026, 7, 31)


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _client() -> MusicBrainzClient:
    return MusicBrainzClient(rate_limiter=RateLimiter(min_interval=0), sleep=lambda _s: None)


def _browse_url(artist_mbid: str) -> str:
    return f"{MB_BASE_URL}/release-group?artist={artist_mbid}&fmt=json&limit=100&offset=0"


def _add_watched_artist(
    storage: Storage, key: str, name: str, mbid: str, removed: bool = False
) -> None:
    with storage.session() as session:
        session.add(
            Artist(
                plex_rating_key=key,
                name=name,
                library_key="1",
                removed_at=dt.datetime.now(dt.UTC) if removed else None,
            )
        )
        session.commit()
    storage.save_artist_match(key, name, "auto", mbid=mbid, confidence=0.99)


def test_parse_earliest_date_partial_forms() -> None:
    assert parse_earliest_date("2026") == dt.date(2026, 1, 1)
    assert parse_earliest_date("2026-09") == dt.date(2026, 9, 1)
    assert parse_earliest_date("2026-09-18") == dt.date(2026, 9, 18)
    assert parse_earliest_date("") is None
    assert parse_earliest_date("not-a-date") is None
    assert parse_earliest_date("2026-13-99") is None


def test_first_poll_baselines_catalog_silently_except_upcoming(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # A 3-album back catalog plus one future-dated announcement: the baseline
    # must not flood F4 with "new" events for old albums, but the
    # announcement is news — it becomes the calendar's "upcoming" entry.
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-old-1", "Debut", first_release_date="1999-06-01"),
            mb_release_group("rg-old-2", "Sophomore", first_release_date="2004"),
            mb_release_group("rg-undated", "Rarities"),
            mb_release_group("rg-future", "Announced", first_release_date="2026-11-20"),
        )
    )
    result = watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    assert result.baselined is True
    assert result.groups_seen == 4
    assert result.events_new == 0
    assert result.events_upcoming == 1
    events = storage.list_events()
    assert [event.kind for event in events] == ["upcoming"]
    assert {row.mbid for row in storage.list_release_groups("mb-artist-1")} == {
        "rg-old-1",
        "rg-old-2",
        "rg-undated",
        "rg-future",
    }


def test_new_group_after_baseline_becomes_a_new_event(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-old", "Debut", first_release_date="1999-06-01")
        )
    )
    watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-old", "Debut", first_release_date="1999-06-01"),
            mb_release_group("rg-new", "Surprise Drop", first_release_date="2026-07-30"),
        )
    )
    result = watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    assert result.baselined is False
    assert result.events_new == 1
    assert result.events_upcoming == 0
    assert [event.kind for event in storage.list_events()] == ["new"]


def test_reissues_and_unchanged_groups_never_realert(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # A reissue/edition-add changes nothing at release-group level
    # (docs/adr/0001): polling the identical group list twice emits nothing.
    payload = mb_browse_response(
        mb_release_group("rg-old", "Debut", first_release_date="1999-06-01")
    )
    httpx_mock.add_response(json=payload)
    httpx_mock.add_response(json=payload)
    watch_artist(storage, _client(), "mb-artist-1", today=TODAY)
    result = watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    assert result.events_new == 0
    assert result.events_upcoming == 0
    assert result.events_date_changed == 0
    assert storage.list_events() == []


def test_date_revision_emits_date_changed_and_updates_the_row(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-future", "Announced", first_release_date="2026-11-20")
        )
    )
    watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-future", "Announced", first_release_date="2026-12-04")
        )
    )
    result = watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    assert result.events_date_changed == 1
    kinds = [event.kind for event in storage.list_events()]
    assert kinds == ["upcoming", "date_changed"]
    (row,) = storage.list_release_groups("mb-artist-1")
    assert row.first_release_date == "2026-12-04"


def test_watched_mbids_join_excludes_tombstoned_pending_and_skipped(
    storage: Storage,
) -> None:
    _add_watched_artist(storage, "key-1", "Present", "mb-present")
    _add_watched_artist(storage, "key-2", "Removed", "mb-removed", removed=True)
    _add_watched_artist(storage, "key-3", "Manual", "mb-manual")
    storage.resolve_artist_match("key-3", "mb-manual")
    with storage.session() as session:
        session.add(Artist(plex_rating_key="key-4", name="Pending", library_key="1"))
        session.commit()
    storage.save_artist_match("key-4", "Pending", "pending")

    watched = storage.list_watched_artist_mbids()

    assert sorted(watched) == ["mb-manual", "mb-present"]


def test_shared_release_group_tracks_per_artist(storage: Storage, httpx_mock: HTTPXMock) -> None:
    # One release-group credited to two watched artists (a split single, a
    # collab live EP — e.g. a live cover credited to both bands): the second
    # artist's poll must record its own row instead of dying on a globally
    # unique mbid. This is the real-world crash migration v7 exists for.
    shared = mb_release_group("rg-shared", "Live Split", first_release_date="2023-11-02")
    httpx_mock.add_response(url=_browse_url("mb-artist-1"), json=mb_browse_response(shared))
    httpx_mock.add_response(url=_browse_url("mb-artist-2"), json=mb_browse_response(shared))

    watch_artist(storage, _client(), "mb-artist-1", today=TODAY)
    result = watch_artist(storage, _client(), "mb-artist-2", today=TODAY)

    assert result.baselined is True
    assert {row.mbid for row in storage.list_release_groups("mb-artist-1")} == {"rg-shared"}
    assert {row.mbid for row in storage.list_release_groups("mb-artist-2")} == {"rg-shared"}


def test_shared_new_group_after_baseline_alerts_each_artist(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # Both artists are past baseline; a new group credited to both is news
    # for each of them — one event per watched artist, on that artist's row.
    for artist in ("mb-artist-1", "mb-artist-2"):
        httpx_mock.add_response(
            url=_browse_url(artist),
            json=mb_browse_response(
                mb_release_group(f"rg-{artist}", "Debut", first_release_date="1999-06-01")
            ),
        )
        watch_artist(storage, _client(), artist, today=TODAY)
    for artist in ("mb-artist-1", "mb-artist-2"):
        httpx_mock.add_response(
            url=_browse_url(artist),
            json=mb_browse_response(
                mb_release_group(f"rg-{artist}", "Debut", first_release_date="1999-06-01"),
                mb_release_group("rg-collab", "Joint Single", first_release_date="2026-07-30"),
            ),
        )

    results = [
        watch_artist(storage, _client(), a, today=TODAY) for a in ("mb-artist-1", "mb-artist-2")
    ]

    assert [r.events_new for r in results] == [1, 1]
    assert [event.kind for event in storage.list_events()] == ["new", "new"]


def test_watch_all_skips_failed_artists_and_keeps_polling(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # Skip-don't-queue (risk R8): one artist's MetaBrainz failure must not
    # wedge the cycle — the healthy artist still gets polled and recorded.
    _add_watched_artist(storage, "key-1", "Broken", "mb-broken")
    _add_watched_artist(storage, "key-2", "Healthy", "mb-healthy")
    for _ in range(3):  # exhaust the client's bounded retries
        httpx_mock.add_response(url=_browse_url("mb-broken"), status_code=503)
    httpx_mock.add_response(
        url=_browse_url("mb-healthy"),
        json=mb_browse_response(mb_release_group("rg-1", "Debut", first_release_date="1999")),
    )

    report = watch_all_artists(storage, _client())

    assert report.artists_failed == 1
    assert report.artists_polled == 1
    assert report.artists_baselined == 1
    assert report.groups_seen == 1
    assert storage.list_release_groups("mb-healthy") != []


def test_watch_all_skips_storage_failures_and_keeps_polling(
    storage: Storage, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Skip-don't-queue covers storage failures too: before v7's composite
    # index, one shared release-group's IntegrityError killed the whole
    # cycle. A StorageError from one artist must leave the rest polled.
    _add_watched_artist(storage, "key-1", "Broken", "mb-broken")
    _add_watched_artist(storage, "key-2", "Healthy", "mb-healthy")
    for artist in ("mb-broken", "mb-healthy"):
        httpx_mock.add_response(
            url=_browse_url(artist),
            json=mb_browse_response(
                mb_release_group(f"rg-{artist}", "Debut", first_release_date="1999")
            ),
        )
    original = storage.add_release_group

    def failing(**kwargs: object) -> object:
        if kwargs["artist_mbid"] == "mb-broken":
            raise StorageError("synthetic storage failure")
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(storage, "add_release_group", failing)

    report = watch_all_artists(storage, _client())

    assert report.artists_failed == 1
    assert report.artists_polled == 1
    assert storage.list_release_groups("mb-healthy") != []
    assert storage.list_release_groups("mb-broken") == []


@pytest.mark.no_outing
def test_watch_cycle_logs_counts_never_identifiers(
    storage: Storage, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    _add_watched_artist(storage, "key-1", "Sentinel Artist Needle", "mb-sentinel-needle")
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group(
                "rg-sentinel-needle", "Sentinel Title Needle", first_release_date="2026-12-01"
            )
        )
    )
    with caplog.at_level(logging.DEBUG):
        report = watch_all_artists(storage, _client())

    assert report.events_upcoming == 1
    assert "watch cycle" in caplog.text
    assert "Sentinel Artist Needle" not in caplog.text
    assert "Sentinel Title Needle" not in caplog.text
    assert "mb-sentinel-needle" not in caplog.text
    assert "rg-sentinel-needle" not in caplog.text


def test_event_kind_validation_and_missing_row_errors(storage: Storage) -> None:
    with pytest.raises(StorageError, match="invalid event kind"):
        storage.add_event(1, "reissue")
    with pytest.raises(StorageError, match="no release-group row"):
        storage.update_release_group_date("mb-artist-1", "rg-none", "2026-01-01")


def test_list_events_filters_by_kind(storage: Storage, httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("rg-future", "Announced", first_release_date="2026-11-20")
        )
    )
    watch_artist(storage, _client(), "mb-artist-1", today=TODAY)

    assert [event.kind for event in storage.list_events(kind="upcoming")] == ["upcoming"]
    assert storage.list_events(kind="new") == []


def test_type_policy_filters_events_but_still_records_groups(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # Albums-only default: an EP and a live album are recorded (the diff
    # must stay exact) but raise no events.
    from encore.artistsettings import SettingsOverride

    _add_watched_artist(storage, "key-1", "Low", "mbid-low")
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("g-album", "Album", primary_type="Album"),
            mb_release_group("g-ep", "Some EP", primary_type="EP", first_release_date="2026-09-01"),
            mb_release_group(
                "g-live", "Live Thing", primary_type="Album", secondary_types=("Live",)
            ),
        )
    )
    result = watch_artist(storage, _client(), "mbid-low", today=TODAY)

    assert result.groups_seen == 3
    assert result.events_filtered == 2
    assert result.events_new == 0  # first poll baselines silently anyway
    assert len(storage.list_release_groups("mbid-low")) == 3

    # A later opt-in applies going forward: the stored groups diff to
    # nothing (no back-catalog flood), a genuinely new EP becomes news.
    storage.set_artist_settings("key-1", SettingsOverride(allow_primary=("album", "ep")))
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("g-album", "Album", primary_type="Album"),
            mb_release_group("g-ep", "Some EP", primary_type="EP", first_release_date="2026-09-01"),
            mb_release_group(
                "g-live", "Live Thing", primary_type="Album", secondary_types=("Live",)
            ),
            mb_release_group("g-new", "Newer EP", primary_type="EP"),
        )
    )
    second = watch_artist(storage, _client(), "mbid-low", today=TODAY)
    assert second.events_new == 1
    assert second.events_filtered == 0


def test_date_changed_on_a_filtered_type_raises_no_event(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    from encore.artistsettings import SettingsOverride

    _add_watched_artist(storage, "key-1", "Low", "mbid-low")
    storage.set_artist_settings("key-1", SettingsOverride(allow_primary=("album",)))
    httpx_mock.add_response(json=mb_browse_response(mb_release_group("g-1", "Only")))
    watch_artist(storage, _client(), "mbid-low", today=TODAY)
    baseline = len(storage.list_events())

    # An EP's date moved: recorded, but the artist allows albums only.
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group("g-1", "Only"),
            mb_release_group("g-2", "Moved EP", primary_type="EP", first_release_date="2030-01-01"),
        )
    )
    result = watch_artist(storage, _client(), "mbid-low", today=TODAY)

    # The moved date is recorded on the group row, but the event is gated:
    # albums-only means an EP's schedule change is not news.
    assert result.events_filtered == 1
    assert result.events_new == 0 and result.events_upcoming == 0
    assert len(storage.list_events()) == baseline


def test_upcoming_announcements_respect_the_type_policy_too(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    # Announcements are news even at baseline — unless the type is one the
    # listener opted out of; F10 gates every event kind uniformly.
    _add_watched_artist(storage, "key-1", "Low", "mbid-low")
    httpx_mock.add_response(
        json=mb_browse_response(
            mb_release_group(
                "g-future-live",
                "Future Live",
                primary_type="Album",
                secondary_types=("Live",),
                first_release_date="2031-03-01",
            ),
        )
    )
    result = watch_artist(storage, _client(), "mbid-low", today=TODAY)
    assert result.events_upcoming == 0
    assert result.events_filtered == 1
