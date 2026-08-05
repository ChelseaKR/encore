"""Minimal MusicBrainz WS/2 client: artist search (F2) + release-group browse (F3).

MetaBrainz politeness is architecture, not etiquette (roadmap risk R8): every
request goes through a process-global rate limiter (default one request per
second — the documented MusicBrainz budget), carries a descriptive
``User-Agent``, and honors ``Retry-After`` on 429/503 with a bounded number
of retries. The F3 release-group poller reuses `MB_RATE_LIMITER` (it shares
this client), so the whole process shares one budget — no per-job limiters
that can sum past 1 req/s (encore-plans/04 §API budget).

Privacy (no-outing lens): artist names are taste data. This module never
logs query text, artist names, or MBIDs — log lines carry only operational
metadata (status codes, counts, delays). Proven by a marker test in
`tests/test_matching_engine.py`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from encore import __version__

__all__ = [
    "MB_BASE_URL",
    "MB_RATE_LIMITER",
    "USER_AGENT",
    "ArtistCandidate",
    "MusicBrainzClient",
    "MusicBrainzError",
    "RateLimiter",
    "ReleaseGroupInfo",
]

logger = logging.getLogger(__name__)

MB_BASE_URL = "https://musicbrainz.org/ws/2"
# Descriptive, contactable User-Agent — the MetaBrainz API citizenship
# requirement (roadmap §5, risk R8).
USER_AGENT = f"encore/{__version__} (https://github.com/ChelseaKR/encore)"

_MAX_ATTEMPTS = 3
# Release-group browse pagination (F3): MB's maximum page size, and a
# defensive cap on pages per artist so one discography can't monopolize the
# shared 1 req/s budget (1,000 groups covers all but pathological artists).
_BROWSE_PAGE_LIMIT = 100
_MAX_BROWSE_PAGES = 10
_MAX_RETRY_AFTER_SECONDS = 30.0
_DEFAULT_BACKOFF_SECONDS = 2.0
_REQUEST_TIMEOUT_SECONDS = 10.0

# Lucene special characters, per the MusicBrainz search documentation.
_LUCENE_SPECIALS = '+-&|!(){}[]^"~*?:\\/'


class MusicBrainzError(Exception):
    """The MusicBrainz API could not be reached or answered unusably."""


class RateLimiter:
    """A minimum-interval limiter: at most one request per ``min_interval`` seconds.

    ``clock``/``sleep`` are injectable so tests can verify pacing without
    real waiting. A single shared instance (`MB_RATE_LIMITER`) is the
    process-wide MusicBrainz budget.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure the interval and (optionally) inject clock/sleep for tests."""
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None

    def wait(self) -> None:
        """Block until the next request is allowed, then claim the slot."""
        if self.min_interval > 0 and self._last_request_at is not None:
            elapsed = self._clock() - self._last_request_at
            remaining = self.min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._clock()


MB_RATE_LIMITER = RateLimiter()


@dataclass(frozen=True)
class ReleaseGroupInfo:
    """One release-group row from a MusicBrainz browse response (F3).

    ``first_release_date`` is MusicBrainz's partial date verbatim (``YYYY``,
    ``YYYY-MM``, ``YYYY-MM-DD``, or ``""`` when MB has none).
    """

    mbid: str
    title: str
    primary_type: str | None = None
    secondary_types: tuple[str, ...] = ()
    first_release_date: str = ""


@dataclass(frozen=True)
class ArtistCandidate:
    """One artist row from a MusicBrainz search response."""

    mbid: str
    name: str
    sort_name: str = ""
    mb_score: int = 0
    artist_type: str | None = None
    country: str | None = None
    disambiguation: str | None = None
    aliases: tuple[str, ...] = field(default_factory=tuple)


def escape_lucene(text: str) -> str:
    """Escape Lucene query syntax so an artist name is matched literally."""
    escaped = []
    for char in text:
        if char in _LUCENE_SPECIALS:
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def _parse_aliases(raw: object) -> tuple[str, ...]:
    """Extract alias names from the raw ``aliases`` list, tolerating junk."""
    if not isinstance(raw, list):
        return ()
    names = []
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.append(entry["name"])
    return tuple(names)


def _parse_release_group(raw: object) -> ReleaseGroupInfo | None:
    """Turn one raw release-group object into an info row, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    mbid = raw.get("id")
    title = raw.get("title")
    if not isinstance(mbid, str) or not isinstance(title, str):
        return None
    primary_type = raw.get("primary-type")
    first_release_date = raw.get("first-release-date")
    secondary_raw = raw.get("secondary-types")
    secondary_types = (
        tuple(entry for entry in secondary_raw if isinstance(entry, str))
        if isinstance(secondary_raw, list)
        else ()
    )
    return ReleaseGroupInfo(
        mbid=mbid,
        title=title,
        primary_type=primary_type if isinstance(primary_type, str) else None,
        secondary_types=secondary_types,
        first_release_date=first_release_date if isinstance(first_release_date, str) else "",
    )


def _parse_candidate(raw: object) -> ArtistCandidate | None:
    """Turn one raw artist object into a candidate, or ``None`` if malformed."""
    if not isinstance(raw, dict):
        return None
    mbid = raw.get("id")
    name = raw.get("name")
    if not isinstance(mbid, str) or not isinstance(name, str):
        return None
    mb_score = raw.get("score")
    artist_type = raw.get("type")
    country = raw.get("country")
    disambiguation = raw.get("disambiguation")
    sort_name = raw.get("sort-name")
    return ArtistCandidate(
        mbid=mbid,
        name=name,
        sort_name=sort_name if isinstance(sort_name, str) else "",
        mb_score=mb_score if isinstance(mb_score, int) else 0,
        artist_type=artist_type if isinstance(artist_type, str) else None,
        country=country if isinstance(country, str) else None,
        disambiguation=disambiguation if isinstance(disambiguation, str) else None,
        aliases=_parse_aliases(raw.get("aliases")),
    )


class MusicBrainzClient:
    """Search MusicBrainz for artists, politely (rate-limited, retrying)."""

    def __init__(
        self,
        base_url: str = MB_BASE_URL,
        rate_limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create the client; the default rate limiter is the process-global one."""
        self._base_url = base_url.rstrip("/")
        self._rate_limiter = rate_limiter if rate_limiter is not None else MB_RATE_LIMITER
        self._sleep = sleep
        # httpx/httpcore log full request URLs at INFO/DEBUG — and MusicBrainz
        # search URLs embed artist names, which are taste data (no-outing
        # lens, OBS-11). Suppress their request-line logging deliberately;
        # encore's own log lines carry only operational metadata. Enforced by
        # the ``no_outing`` marker test in `tests/test_matching_engine.py`.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=httpx.Timeout(_REQUEST_TIMEOUT_SECONDS),
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def search_artists(self, name: str, limit: int = 8) -> list[ArtistCandidate]:
        """Search for artists by name; returns candidates in server order.

        Raises:
            MusicBrainzError: the API was unreachable or kept answering
                429/503/5xx after the bounded retries.
        """
        query = f'artist:"{escape_lucene(name)}"'
        params = {"query": query, "fmt": "json", "limit": str(limit)}
        response = self._request_with_retries("/artist", params)
        payload: object = response.json()
        artists_raw: object = payload.get("artists", []) if isinstance(payload, dict) else []
        candidates = []
        if isinstance(artists_raw, list):
            for raw in artists_raw:
                candidate = _parse_candidate(raw)
                if candidate is not None:
                    candidates.append(candidate)
        logger.debug("MusicBrainz search returned %d candidate(s)", len(candidates))
        return candidates

    def browse_release_groups(self, artist_mbid: str) -> list[ReleaseGroupInfo]:
        """Browse all release-groups credited to an artist MBID (F3).

        Pages through WS/2 ``/release-group?artist=…`` under the shared rate
        limiter. Pagination is capped at `_MAX_BROWSE_PAGES` pages of
        `_BROWSE_PAGE_LIMIT` — a defensive bound so one pathologically large
        discography cannot eat the whole process's MetaBrainz budget; the
        truncation is logged (counts only) and the next poll resumes cheaply
        because already-seen groups diff to nothing.

        Raises:
            MusicBrainzError: the API was unreachable or kept answering
                429/503/5xx after the bounded retries.
        """
        groups: list[ReleaseGroupInfo] = []
        offset = 0
        for _page in range(_MAX_BROWSE_PAGES):
            params = {
                "artist": artist_mbid,
                "fmt": "json",
                "limit": str(_BROWSE_PAGE_LIMIT),
                "offset": str(offset),
            }
            response = self._request_with_retries("/release-group", params)
            payload: object = response.json()
            if not isinstance(payload, dict):
                break
            raw_groups = payload.get("release-groups", [])
            if not isinstance(raw_groups, list) or not raw_groups:
                break
            for raw in raw_groups:
                parsed = _parse_release_group(raw)
                if parsed is not None:
                    groups.append(parsed)
            offset += len(raw_groups)
            total = payload.get("release-group-count")
            if isinstance(total, int) and offset >= total:
                break
        else:
            logger.warning(
                "release-group browse truncated at %d entries (page cap %d)",
                len(groups),
                _MAX_BROWSE_PAGES,
            )
        logger.debug("MusicBrainz browse returned %d release-group(s)", len(groups))
        return groups

    def _request_with_retries(self, path: str, params: dict[str, str]) -> httpx.Response:
        """GET a WS/2 path under the rate limiter, honoring ``Retry-After``."""
        url = f"{self._base_url}{path}"
        last_status = 0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._rate_limiter.wait()
            try:
                response = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise MusicBrainzError(f"MusicBrainz request failed: {exc!r}") from exc
            if response.status_code == httpx.codes.OK:
                return response
            last_status = response.status_code
            if response.status_code in (429, 503) and attempt < _MAX_ATTEMPTS:
                delay = _retry_after_seconds(response)
                logger.debug(
                    "MusicBrainz answered %d; retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    delay,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                self._sleep(delay)
                continue
            break
        raise MusicBrainzError(f"MusicBrainz request failed with HTTP {last_status} after retries")


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
