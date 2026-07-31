"""MusicBrainz release-group browse tests (F3): pagination, budget, privacy."""

from __future__ import annotations

import logging

import pytest
from pytest_httpx import HTTPXMock

from encore.matching.mb import (
    USER_AGENT,
    MusicBrainzClient,
    MusicBrainzError,
    RateLimiter,
    ReleaseGroupInfo,
)
from tests.mb_fixtures import mb_browse_response, mb_release_group


def _quiet_client(
    rate_limiter: RateLimiter | None = None, sleeps: list[float] | None = None
) -> MusicBrainzClient:
    """Build a client with no rate-limit waiting and recorded (not real) sleeps."""
    return MusicBrainzClient(
        rate_limiter=rate_limiter if rate_limiter is not None else RateLimiter(min_interval=0),
        sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
    )


def test_browse_sends_descriptive_user_agent_and_browse_params(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=mb_browse_response(mb_release_group("rg-1", "OK Computer")))
    client = _quiet_client()
    groups = client.browse_release_groups("mb-artist-1")
    client.close()

    request = httpx_mock.get_requests()[0]
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.url.path.endswith("/release-group")
    assert request.url.params["artist"] == "mb-artist-1"
    assert request.url.params["fmt"] == "json"
    assert request.url.params["limit"] == "100"
    assert request.url.params["offset"] == "0"
    assert groups == [ReleaseGroupInfo(mbid="rg-1", title="OK Computer", primary_type="Album")]


def test_browse_paginates_until_the_reported_total(httpx_mock: HTTPXMock) -> None:
    page_one = [mb_release_group(f"rg-{i}", f"Album {i}") for i in range(100)]
    page_two = [mb_release_group(f"rg-{i}", f"Album {i}") for i in range(100, 150)]
    httpx_mock.add_response(json=mb_browse_response(*page_one, total=150, offset=0))
    httpx_mock.add_response(json=mb_browse_response(*page_two, total=150, offset=100))

    client = _quiet_client()
    groups = client.browse_release_groups("mb-artist-1")
    client.close()

    assert len(groups) == 150
    requests = httpx_mock.get_requests()
    assert [request.url.params["offset"] for request in requests] == ["0", "100"]


def test_browse_parses_fields_and_skips_malformed_entries(httpx_mock: HTTPXMock) -> None:
    payload = mb_browse_response(
        mb_release_group(
            "rg-live",
            "Live in Reykjavík",
            primary_type="Album",
            secondary_types=("Live",),
            first_release_date="2026-11",
        ),
        {"id": 123, "title": "non-string id"},  # malformed
        {"title": "no id"},  # malformed
        {"id": "rg-undated", "title": "Undated", "primary-type": None},
    )
    httpx_mock.add_response(json=payload)
    client = _quiet_client()
    groups = client.browse_release_groups("mb-artist-1")
    client.close()

    assert groups == [
        ReleaseGroupInfo(
            mbid="rg-live",
            title="Live in Reykjavík",
            primary_type="Album",
            secondary_types=("Live",),
            first_release_date="2026-11",
        ),
        ReleaseGroupInfo(mbid="rg-undated", title="Undated", primary_type=None),
    ]


def test_browse_shares_the_process_global_rate_limiter_budget(httpx_mock: HTTPXMock) -> None:
    # The load-bearing politeness invariant (encore-plans/04): search and
    # browse must draw from ONE budget — every request claims the limiter.
    waits: list[float] = []

    class CountingLimiter(RateLimiter):
        def wait(self) -> None:
            waits.append(1.0)

    limiter = CountingLimiter(min_interval=0)
    httpx_mock.add_response(json=mb_browse_response(*[], total=0))
    httpx_mock.add_response(json=mb_browse_response(mb_release_group("rg-1", "x"), total=1))
    client = _quiet_client(rate_limiter=limiter)
    client.browse_release_groups("mb-artist-1")
    client.browse_release_groups("mb-artist-2")
    client.close()

    assert len(waits) == len(httpx_mock.get_requests())
    assert len(waits) >= 2


def test_browse_honors_retry_after_then_succeeds(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(status_code=503, headers={"Retry-After": "3"})
    httpx_mock.add_response(json=mb_browse_response(mb_release_group("rg-1", "x")))
    sleeps: list[float] = []
    client = _quiet_client(sleeps=sleeps)
    groups = client.browse_release_groups("mb-artist-1")
    client.close()

    assert sleeps == [3.0]
    assert len(groups) == 1


def test_browse_raises_after_bounded_retries(httpx_mock: HTTPXMock) -> None:
    for _ in range(3):
        httpx_mock.add_response(status_code=503)
    client = _quiet_client()
    with pytest.raises(MusicBrainzError, match="request failed"):
        client.browse_release_groups("mb-artist-1")
    client.close()


@pytest.mark.no_outing
def test_browse_logs_carry_no_mbids_or_titles(
    httpx_mock: HTTPXMock, caplog: pytest.LogCaptureFixture
) -> None:
    # MBIDs and release titles are taste data (dpia.md §4): even DEBUG log
    # output must carry counts only.
    httpx_mock.add_response(
        json=mb_browse_response(mb_release_group("rg-sentinel-needle", "Sentinel Album Needle"))
    )
    client = _quiet_client()
    with caplog.at_level(logging.DEBUG):
        client.browse_release_groups("mb-artist-sentinel-needle")
    client.close()

    assert "rg-sentinel-needle" not in caplog.text
    assert "Sentinel Album Needle" not in caplog.text
    assert "mb-artist-sentinel-needle" not in caplog.text
