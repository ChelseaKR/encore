"""Minimal ListenBrainz labs client: batched similar-artists lookups (F7).

The intelligence behind recommendations is MetaBrainz's open dataset —
no ML in-process, no new AI surface (ADR-0009 stays true). Politeness is
architecture here exactly as it is for MusicBrainz (risk R8): one
process-global rate limiter (`LB_RATE_LIMITER`, 1 request/second), a
descriptive User-Agent, ``Retry-After`` honored on 429/503 with bounded
retries. Seeds are sent in batches (the endpoint accepts comma-separated
``artist_mbids``), so even a 1,000-artist library is ~20 requests per
weekly refresh.

Response contract (verified against the live labs endpoint): a flat JSON
array of rows shaped like::

    {"artist_mbid": "...", "name": "...", "comment": null|"...",
     "type": "Group"|null, "score": 11782,
     "reference_mbid": "<the seed artist this row is similar-to>"}

``score`` is an unbounded similarity figure where higher is closer; the
engine normalizes against the refresh maximum. Rows are attributed to
their seed via ``reference_mbid``, which is what makes batching safe.

Privacy (no-outing lens): artist names and MBIDs are taste data. This
module never logs them — log lines carry counts and status codes only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import httpx

from encore import __version__
from encore.matching.mb import RateLimiter

__all__ = [
    "DEFAULT_ALGORITHM",
    "LB_BASE_URL",
    "LB_RATE_LIMITER",
    "ListenBrainzClient",
    "ListenBrainzError",
    "SimilarArtistInfo",
]

logger = logging.getLogger(__name__)

LB_BASE_URL = "https://labs.api.listenbrainz.org"
# The default session-based similarity algorithm the labs UI exposes; the
# endpoint requires an explicit algorithm and answers 400 without one.
DEFAULT_ALGORITHM = (
    "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
)
LB_RATE_LIMITER = RateLimiter()

_MAX_ATTEMPTS = 3
# The endpoint accepts many comma-separated MBIDs; fifty keeps URLs sane
# and a full weekly refresh at ~20 requests for a 1,000-artist library.
_MAX_MBIDS_PER_REQUEST = 50
_MAX_RETRY_AFTER_SECONDS = 30.0
_DEFAULT_BACKOFF_SECONDS = 2.0
_REQUEST_TIMEOUT_SECONDS = 10.0


class ListenBrainzError(Exception):
    """The ListenBrainz labs API could not be reached or answered unusably."""


@dataclass(frozen=True)
class SimilarArtistInfo:
    """One row from the labs response: an artist similar to ``reference_mbid``."""

    mbid: str
    name: str
    comment: str | None
    artist_type: str | None
    score: float
    reference_mbid: str


def _parse_row(raw: object) -> SimilarArtistInfo | None:
    """Turn one raw JSON row into an info record, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    mbid = raw.get("artist_mbid")
    reference = raw.get("reference_mbid")
    score = raw.get("score")
    if not isinstance(mbid, str) or not isinstance(reference, str):
        return None
    if not isinstance(score, int | float):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        # An unnamed candidate cannot render with provenance — skip it.
        return None
    comment = raw.get("comment")
    artist_type = raw.get("type")
    return SimilarArtistInfo(
        mbid=mbid,
        name=name,
        comment=comment if isinstance(comment, str) and comment else None,
        artist_type=artist_type if isinstance(artist_type, str) and artist_type else None,
        score=float(score),
        reference_mbid=reference,
    )


class ListenBrainzClient:
    """Fetch similar artists from the labs API, politely."""

    def __init__(
        self,
        base_url: str = LB_BASE_URL,
        algorithm: str = DEFAULT_ALGORITHM,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client; the default rate limiter is the process-global one."""
        self._base_url = base_url.rstrip("/")
        self._algorithm = algorithm
        self._rate_limiter = rate_limiter if rate_limiter is not None else LB_RATE_LIMITER
        self._sleep = sleep
        # httpx/httpcore log full request URLs at INFO/DEBUG — and these URLs
        # embed artist MBIDs, which are taste data (OBS-11, no-outing lens).
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._client = httpx.Client(
            headers={"User-Agent": f"encore/{__version__} (https://github.com/ChelseaKR/encore)"},
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    @property
    def algorithm(self) -> str:
        """The similarity algorithm this client requests (recorded in provenance)."""
        return self._algorithm

    def similar_artists(self, artist_mbids: Sequence[str]) -> list[SimilarArtistInfo]:
        """Look up artists similar to the given seeds in one polite request.

        The caller batches (the engine caps batches at fifty); more than
        `_MAX_MBIDS_PER_REQUEST` seeds here raises rather than silently
        stretching the URL.

        Raises:
            ValueError: more seeds than one request may carry.
            ListenBrainzError: the API was unreachable or kept answering
                429/503 after the bounded retries.
        """
        unique_ids = list(dict.fromkeys(artist_mbids))
        if len(unique_ids) > _MAX_MBIDS_PER_REQUEST:
            raise ValueError(
                f"a similar-artists request carries at most {_MAX_MBIDS_PER_REQUEST} "
                f"seeds ({len(unique_ids)} given)"
            )
        params = {"artist_mbids": ",".join(unique_ids), "algorithm": self._algorithm}
        response = self._request("/similar-artists/json", params)
        payload: object = response.json()
        rows: list[SimilarArtistInfo] = []
        if isinstance(payload, list):
            for raw in payload:
                parsed = _parse_row(raw)
                if parsed is not None:
                    rows.append(parsed)
        logger.debug("ListenBrainz returned %d similar-artists row(s)", len(rows))
        return rows

    def _request(self, path: str, params: dict[str, str]) -> httpx.Response:
        """GET one labs path under the rate limiter, honoring ``Retry-After``."""
        url = f"{self._base_url}{path}"
        last_status = 0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._rate_limiter.wait()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise ListenBrainzError(f"ListenBrainz request failed: {exc!r}") from exc
            if response.status_code == httpx.codes.OK:
                return response
            last_status = response.status_code
            if response.status_code in (429, 503) and attempt < _MAX_ATTEMPTS:
                delay = _retry_after_seconds(response)
                logger.debug(
                    "ListenBrainz answered %d; retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    delay,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                self._sleep(delay)
                continue
            break
        raise ListenBrainzError(f"ListenBrainz request failed with HTTP {last_status}")


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse ``Retry-After`` (seconds form), bounded; default a short backoff."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_BACKOFF_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        return _DEFAULT_BACKOFF_SECONDS
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))
