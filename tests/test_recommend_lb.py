"""ListenBrainz labs client tests: parsing the verified response contract.

The fixture payload mirrors a real response from
`https://labs.api.listenbrainz.org/similar-artists/json?...` (recorded
shape, 2026-08-21): a flat array of rows carrying `artist_mbid`, `name`,
`comment`, `type`, an unbounded `score`, and `reference_mbid` naming the
seed each row is similar-to. If the labs API changes shape, these tests
fail in CI instead of in someone's weekly refresh.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from encore.matching.mb import RateLimiter
from encore.recommend.lb import (
    DEFAULT_ALGORITHM,
    ListenBrainzClient,
    ListenBrainzError,
)

SEED_MBID = "a74b1b7f-71a5-4011-9441-d0b5e4122711"
OTHER_SEED_MBID = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
SIMILAR_MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"

# Recorded live shape (2026-08-21), trimmed to two rows.
LABS_RESPONSE = [
    {
        "artist_mbid": SIMILAR_MBID,
        "name": "Nirvana",
        "comment": "1980s\u20131990s US grunge band",
        "type": "Group",
        "gender": None,
        "score": 11782,
        "reference_mbid": SEED_MBID,
    },
    {
        "artist_mbid": "8bfac288-ccc5-448d-9573-c33ea2aa5c30",
        "name": "Red Hot Chili Peppers",
        "comment": "",
        "type": None,
        "gender": None,
        "score": 11140,
        "reference_mbid": OTHER_SEED_MBID,
    },
]


def _client() -> ListenBrainzClient:
    return ListenBrainzClient(rate_limiter=RateLimiter(min_interval=0), sleep=lambda _s: None)


def test_similar_artists_parses_the_recorded_shape(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(json=LABS_RESPONSE)

    rows = _client().similar_artists([SEED_MBID, OTHER_SEED_MBID])

    assert [row.name for row in rows] == ["Nirvana", "Red Hot Chili Peppers"]
    first = rows[0]
    assert first.mbid == SIMILAR_MBID
    assert first.comment == "1980s\u20131990s US grunge band"
    assert first.score == 11782.0
    assert first.reference_mbid == SEED_MBID
    # The request carries both seeds in one polite call and names the algorithm.
    request = httpx_mock.get_requests()[0]
    assert request.url.params["artist_mbids"] == f"{SEED_MBID},{OTHER_SEED_MBID}"
    assert request.url.params["algorithm"] == DEFAULT_ALGORITHM


def test_malformed_rows_are_skipped_not_fatal(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(
        json=[
            {"artist_mbid": "only-three-fields"},
            {"artist_mbid": SIMILAR_MBID, "name": "", "score": 5, "reference_mbid": SEED_MBID},
            {
                "artist_mbid": SIMILAR_MBID,
                "name": "Nirvana",
                "score": "high",
                "reference_mbid": SEED_MBID,
            },
            LABS_RESPONSE[0],
        ]
    )

    rows = _client().similar_artists([SEED_MBID])

    assert len(rows) == 1 and rows[0].score == 11782.0


def test_more_than_one_batch_of_seeds_is_rejected_before_any_request() -> None:
    with pytest.raises(ValueError):
        _client().similar_artists([f"mbid-{i}" for i in range(51)])


def test_a_503_that_never_recovers_raises_after_bounded_retries(
    httpx_mock: HTTPXMock,
) -> None:
    for _attempt in range(3):
        httpx_mock.add_response(status_code=503)

    with pytest.raises(ListenBrainzError):
        client = ListenBrainzClient(rate_limiter=RateLimiter(min_interval=0))
        try:
            client.similar_artists([SEED_MBID])
        finally:
            client.close()
