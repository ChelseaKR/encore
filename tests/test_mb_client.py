"""MusicBrainz client tests (F2): politeness, retries, parsing, escaping."""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from encore.matching.mb import (
    USER_AGENT,
    ArtistCandidate,
    MusicBrainzClient,
    MusicBrainzError,
    RateLimiter,
    escape_lucene,
)
from tests.mb_fixtures import mb_artist, mb_search_response


def _quiet_client(sleeps: list[float] | None = None) -> MusicBrainzClient:
    """Build a client with no rate-limit waiting and recorded (not real) sleeps."""
    return MusicBrainzClient(
        rate_limiter=RateLimiter(min_interval=0),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def test_search_sends_descriptive_user_agent_and_json_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=mb_search_response())
    client = _quiet_client()
    client.search_artists("Radiohead")
    client.close()

    request = httpx_mock.get_requests()[0]
    assert request.headers["User-Agent"] == USER_AGENT
    assert "encore/" in USER_AGENT and "github.com/ChelseaKR/encore" in USER_AGENT
    assert request.headers["Accept"] == "application/json"
    assert request.url.host == "musicbrainz.org"
    assert request.url.params["fmt"] == "json"
    assert request.url.params["query"] == 'artist:"Radiohead"'


@pytest.mark.no_secrets_in_logs
def test_outbound_request_carries_no_credential_headers(httpx_mock: HTTPXMock) -> None:
    # The matching layer never receives the Plex token, so no request to
    # MetaBrainz can carry it — provable at the transport boundary.
    httpx_mock.add_response(json=mb_search_response())
    client = _quiet_client()
    client.search_artists("Radiohead")
    client.close()

    request = httpx_mock.get_requests()[0]
    assert "X-Plex-Token" not in request.headers
    assert "Authorization" not in request.headers
    assert "Cookie" not in request.headers


def test_lucene_escaping_of_special_characters(httpx_mock: HTTPXMock) -> None:
    assert escape_lucene('AC/DC "live"') == 'AC\\/DC \\"live\\"'
    httpx_mock.add_response(json=mb_search_response())
    client = _quiet_client()
    client.search_artists('AC/DC "live"')
    client.close()
    assert httpx_mock.get_requests()[0].url.params["query"] == 'artist:"AC\\/DC \\"live\\""'


def test_search_parses_candidates_and_skips_malformed_entries(httpx_mock: HTTPXMock) -> None:
    payload = mb_search_response(
        mb_artist("mb-bjork", "Björk", 97, "Person", "IS", "singer", aliases=("Bjork",)),
        {"id": 12345, "name": "not-a-string-id"},  # malformed: non-string id
        {"name": "no id at all"},  # malformed: missing id
    )
    httpx_mock.add_response(json=payload)
    client = _quiet_client()
    candidates = client.search_artists("Björk")
    client.close()

    assert candidates == [
        ArtistCandidate(
            mbid="mb-bjork",
            name="Björk",
            sort_name="Björk",
            mb_score=97,
            artist_type="Person",
            country="IS",
            disambiguation="singer",
            aliases=("Bjork",),
        )
    ]


def test_retry_after_is_honored_then_succeeds(httpx_mock: HTTPXMock) -> None:
    sleeps: list[float] = []
    httpx_mock.add_response(status_code=503, headers={"Retry-After": "3"})
    httpx_mock.add_response(json=mb_search_response(mb_artist("mb-x", "X")))
    client = _quiet_client(sleeps)
    candidates = client.search_artists("X")
    client.close()

    assert [candidate.mbid for candidate in candidates] == ["mb-x"]
    assert sleeps == [3.0]


def test_retry_after_is_capped_and_defaults_when_malformed(httpx_mock: HTTPXMock) -> None:
    sleeps: list[float] = []
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "9999"})
    httpx_mock.add_response(status_code=429, headers={"Retry-After": "not-a-number"})
    httpx_mock.add_response(json=mb_search_response())
    client = _quiet_client(sleeps)
    client.search_artists("X")
    client.close()

    assert sleeps == [30.0, 2.0]


def test_persistent_throttling_raises_after_bounded_retries(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_response(status_code=503, headers={"Retry-After": "1"})
    client = _quiet_client([])
    with pytest.raises(MusicBrainzError, match="HTTP 503"):
        client.search_artists("X")
    client.close()


def test_hard_error_status_raises_without_retry(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=400)
    client = _quiet_client()
    with pytest.raises(MusicBrainzError, match="HTTP 400"):
        client.search_artists("X")
    client.close()
    assert len(httpx_mock.get_requests()) == 1


def test_network_error_maps_to_musicbrainz_error(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("boom"))
    client = _quiet_client()
    with pytest.raises(MusicBrainzError, match="request failed"):
        client.search_artists("X")
    client.close()


def test_rate_limiter_spaces_requests_to_min_interval() -> None:
    now = {"t": 100.0}
    sleeps: list[float] = []

    def fake_clock() -> float:
        return now["t"]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["t"] += seconds

    limiter = RateLimiter(min_interval=1.0, clock=fake_clock, sleep=fake_sleep)
    limiter.wait()  # first call: no wait
    now["t"] += 0.25  # only a quarter second passes
    limiter.wait()  # must sleep the remaining 0.75s
    assert sleeps == [pytest.approx(0.75)]

    now["t"] += 5.0  # plenty of time passes
    limiter.wait()  # no additional sleep needed
    assert sleeps == [pytest.approx(0.75)]
