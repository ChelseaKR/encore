"""Engine tests (F2): fixture-battery precision, permanent cache, review queue.

The battery test is the CI half of F2's acceptance criterion: ≥95% correct
terminal decisions on the known-nasty fixture library AND zero wrong
auto-matches (a wrong auto-match is strictly worse than a queued one). The
≥90% field rate on a real library is the U8 validation spike — not covered
here, and the thresholds stay provisional until it runs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
from pytest_httpx import HTTPXMock

from encore.matching.engine import MatchEngine, candidates_from_json
from encore.matching.mb import MusicBrainzClient, RateLimiter
from encore.matching.scoring import ArtistHints
from encore.storage import Storage, StorageError
from tests.mb_fixtures import MATCH_CASES, mb_artist, mb_search_response


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _engine(storage: Storage) -> MatchEngine:
    client = MusicBrainzClient(rate_limiter=RateLimiter(min_interval=0), sleep=lambda _s: None)
    return MatchEngine(storage, client)


def test_fixture_battery_precision_and_zero_wrong_auto_matches(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    engine = _engine(storage)
    correct = 0
    wrong_auto: list[str] = []
    failures: list[str] = []
    for case in MATCH_CASES:
        httpx_mock.add_response(json=case.response)
        row = engine.match_artist(f"key-{case.case_id}", case.hints)
        if row.status == "auto" and (
            case.expected_status != "auto" or row.mbid != case.expected_mbid
        ):
            wrong_auto.append(case.case_id)
        if row.status == case.expected_status and row.mbid == case.expected_mbid:
            correct += 1
        else:
            failures.append(f"{case.case_id}: got {row.status}/{row.mbid}")

    rate = correct / len(MATCH_CASES)
    assert wrong_auto == [], f"wrong auto-matches (must be zero): {wrong_auto}"
    assert rate >= 0.95, f"fixture precision {rate:.2%} < 95%; failures: {failures}"


def test_pending_cases_land_in_review_queue_with_displayable_candidates(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    engine = _engine(storage)
    pending_cases = [case for case in MATCH_CASES if case.expected_status == "pending"]
    for case in pending_cases:
        httpx_mock.add_response(json=case.response)
        engine.match_artist(f"key-{case.case_id}", case.hints)

    queue = engine.review_queue()
    assert {row.artist_key for row in queue} == {f"key-{case.case_id}" for case in pending_cases}
    for row in queue:
        candidates = candidates_from_json(row.candidates_json)
        artists_in_response = next(
            case.response["artists"]
            for case in pending_cases
            if f"key-{case.case_id}" == row.artist_key
        )
        assert isinstance(artists_in_response, list)
        assert len(candidates) == len(artists_in_response)
        for entry in candidates:
            assert {"mbid", "name", "score"} <= set(entry)


def test_match_is_cached_permanently_one_mb_query_per_artist(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    engine = _engine(storage)
    httpx_mock.add_response(json=mb_search_response(mb_artist("mb-radiohead", "Radiohead", 100)))
    hints = ArtistHints(name="Radiohead")
    first = engine.match_artist("key-1", hints)
    second = engine.match_artist("key-1", hints)

    assert len(httpx_mock.get_requests()) == 1
    assert first.status == "auto"
    assert second.mbid == first.mbid
    # Pending decisions are cached too — a nightly sync must not re-hammer
    # MusicBrainz for artists a human hasn't reviewed yet.
    httpx_mock.add_response(json=mb_search_response())
    engine.match_artist("key-unmatched", ArtistHints(name="Unknown Artist"))
    engine.match_artist("key-unmatched", ArtistHints(name="Unknown Artist"))
    assert len(httpx_mock.get_requests()) == 2


def test_force_rematch_requeries_and_overwrites(storage: Storage, httpx_mock: HTTPXMock) -> None:
    engine = _engine(storage)
    httpx_mock.add_response(json=mb_search_response())
    row = engine.match_artist("key-1", ArtistHints(name="Boards of Canada"))
    assert row.status == "pending"

    httpx_mock.add_response(json=mb_search_response(mb_artist("mb-boc", "Boards of Canada", 100)))
    row = engine.match_artist("key-1", ArtistHints(name="Boards of Canada"), force=True)
    assert row.status == "auto"
    assert row.mbid == "mb-boc"
    assert len(httpx_mock.get_requests()) == 2


def test_resolve_fixes_wrong_match_and_review_entries(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    engine = _engine(storage)
    httpx_mock.add_response(
        json=mb_search_response(
            mb_artist("mb-jw-composer", "John Williams", 100),
            mb_artist("mb-jw-guitarist", "John Williams", 97),
        )
    )
    row = engine.match_artist("key-jw", ArtistHints(name="John Williams"))
    assert row.status == "pending"

    resolved = engine.resolve("key-jw", "mb-jw-guitarist")
    assert resolved.status == "manual"
    assert resolved.mbid == "mb-jw-guitarist"
    assert engine.review_queue() == []

    # Manual re-match overrides any prior decision — no status is final.
    rematched = engine.resolve("key-jw", "mb-jw-composer")
    assert rematched.status == "manual"
    assert rematched.mbid == "mb-jw-composer"

    # And the manual decision is served from cache afterwards (no new query).
    requests_before = len(httpx_mock.get_requests())
    cached = engine.match_artist("key-jw", ArtistHints(name="John Williams"))
    assert cached.mbid == "mb-jw-composer"
    assert len(httpx_mock.get_requests()) == requests_before


def test_skip_leaves_artist_unmatched_without_requery(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    engine = _engine(storage)
    httpx_mock.add_response(json=mb_search_response())
    engine.match_artist("key-demo", ArtistHints(name="My Garage Demos"))

    skipped = engine.skip("key-demo")
    assert skipped.status == "skipped"
    assert skipped.mbid is None
    assert engine.review_queue() == []
    cached = engine.match_artist("key-demo", ArtistHints(name="My Garage Demos"))
    assert cached.status == "skipped"
    assert len(httpx_mock.get_requests()) == 1


def test_resolve_unknown_key_raises_storage_error(storage: Storage) -> None:
    engine = _engine(storage)
    with pytest.raises(StorageError, match="no artist match row"):
        engine.resolve("key-nope", "mb-x")


def test_candidates_from_json_tolerates_absent_and_malformed() -> None:
    assert candidates_from_json(None) == []
    assert candidates_from_json('"not-a-list"') == []
    assert candidates_from_json('[{"mbid": "a"}, "junk"]') == [{"mbid": "a"}]


@pytest.mark.no_outing
def test_matching_logs_never_contain_artist_names_or_mbids(
    storage: Storage, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    # Sentinel artist: a name that would be outing-relevant if it leaked into
    # logs. Neither the name nor any MBID may appear at any log level.
    sentinel_name = "SENTINEL Outing Choir"
    sentinel_mbid = "mb-sentinel-outing"
    httpx_mock.add_response(json=mb_search_response(mb_artist(sentinel_mbid, sentinel_name, 100)))
    with caplog.at_level(logging.DEBUG):
        engine = _engine(storage)
        row = engine.match_artist("key-sentinel", ArtistHints(name=sentinel_name))
        engine.match_artist("key-sentinel", ArtistHints(name=sentinel_name))  # cache path
        engine.resolve("key-sentinel", sentinel_mbid)

    assert row.mbid == sentinel_mbid  # the match itself worked
    # URL-encoded forms count as leaks too (httpx logs request URLs — the
    # client deliberately suppresses that logger for exactly this reason).
    encoded_forms = (
        sentinel_name,
        quote(sentinel_name),
        quote_plus(sentinel_name),
        sentinel_mbid,
    )
    assert caplog.records, "expected log records from the matching flow"
    for record in caplog.records:
        message = record.getMessage()
        for leak in encoded_forms:
            assert leak not in message


@pytest.mark.no_secrets_in_logs
def test_matching_never_logs_or_sends_the_plex_token(
    storage: Storage, httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    sentinel_token = "plex-sentinel-token-1234"  # noqa: S105 — sentinel fixture, not a credential
    storage.set_plex_credentials("http://plex.local:32400", sentinel_token)
    httpx_mock.add_response(json=mb_search_response(mb_artist("mb-a", "Low", 100)))

    with caplog.at_level(logging.DEBUG):
        engine = _engine(storage)
        engine.match_artist("key-low", ArtistHints(name="Low"))

    for record in caplog.records:
        assert sentinel_token not in record.getMessage()
    request = httpx_mock.get_requests()[0]
    assert sentinel_token not in str(request.url)
    assert sentinel_token.encode() not in request.content
    for header_value in request.headers.values():
        assert sentinel_token not in header_value
