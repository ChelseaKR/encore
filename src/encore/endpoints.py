"""Where encore's MetaBrainz-family reads actually go, and how fast.

MetaBrainz asks heavy users to run a mirror, and encore is a heavy user: a
1,000-artist library is roughly 17 minutes of polling per cycle under the
public 1 req/s budget (ADR-0003, `matching/mb.py`). The three base URLs
already existed as module constants; this module is the operator-facing
configuration around them, plus the two rules that keep it safe.

**Rule one: the public host is pinned at 1 req/s, whatever the operator
sets.** `ENCORE_MB_RATE_LIMIT` is honoured only when the configured
MusicBrainz host is not MetaBrainz's own. Against `musicbrainz.org` the
operator's value is discarded and said so, once, rather than quietly
obeyed — an installation that hammers donation-funded infrastructure
because someone exported a variable is a failure encore should not be able
to have.

**Rule two: encore never silently falls back to the public host.** An
operator who points encore at a mirror has moved their library's artist
names and MBIDs onto their own network (see `docs/audits/dpia.md`); a
fallback would move them back off it without anyone being told. So a
configured endpoint that is malformed, unreachable, or not speaking the
MusicBrainz web service is an error that surfaces — in `/readyz`, in
`encore doctor`, and by MB-dependent polling not starting at all — and is
never repaired by substituting the public host.

Two smaller decisions worth knowing:

*A base URL carrying userinfo is refused, and never echoed.* `https://
user:pass@mirror.example/ws/2` is a credential in an environment variable;
accepting it would put it in every log line that names the endpoint in use.
The refusal message names the variable, not the value.

*The public endpoint is not probed.* `probe_metadata_endpoint` returns
`not_applicable` for it, with that reason, rather than a comfortable
`ok` — a check that did not run is not a check that passed. It also does
not make a request to MetaBrainz at every boot, which is the point.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

__all__ = [
    "COVER_ART_BASE_URL_ENV",
    "LB_BASE_URL_ENV",
    "MB_BASE_URL_ENV",
    "MB_RATE_LIMIT_ENV",
    "PROBE_ARTIST_MBID",
    "PROBE_INVALID",
    "PROBE_NOT_APPLICABLE",
    "PROBE_OK",
    "PROBE_STATUSES",
    "PROBE_UNREACHABLE",
    "PUBLIC_COVER_ART_BASE_URL",
    "PUBLIC_LB_BASE_URL",
    "PUBLIC_MB_BASE_URL",
    "PUBLIC_MB_HOST",
    "PUBLIC_MB_RATE_LIMIT",
    "EndpointConfigError",
    "EndpointProbe",
    "Endpoints",
    "probe_metadata_endpoint",
    "resolve_endpoints",
]

logger = logging.getLogger(__name__)

MB_BASE_URL_ENV = "ENCORE_MB_BASE_URL"
LB_BASE_URL_ENV = "ENCORE_LB_BASE_URL"
COVER_ART_BASE_URL_ENV = "ENCORE_COVER_ART_BASE_URL"
MB_RATE_LIMIT_ENV = "ENCORE_MB_RATE_LIMIT"

PUBLIC_MB_BASE_URL = "https://musicbrainz.org/ws/2"
PUBLIC_LB_BASE_URL = "https://labs.api.listenbrainz.org"
PUBLIC_COVER_ART_BASE_URL = "https://coverartarchive.org"

#: MetaBrainz's own host. This exact name, and anything under it, is treated
#: as the public endpoint and is rate-pinned no matter what is configured.
PUBLIC_MB_HOST = "musicbrainz.org"

#: Requests per second against the public host. Not configurable, on purpose.
PUBLIC_MB_RATE_LIMIT = 1.0

#: An upper bound on a mirror's configured rate. Not politeness — a guard on
#: a fat-fingered value that would turn the limiter into a busy loop.
MAX_MIRROR_RATE_LIMIT = 1000.0

#: "Various Artists": a MusicBrainz special-purpose artist whose MBID is
#: stable and reproduced by every mirror of the database. Fetching it is how
#: the probe distinguishes "speaks the MB web service" from "answers HTTPS".
PROBE_ARTIST_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"

PROBE_OK = "ok"
PROBE_INVALID = "invalid"
PROBE_UNREACHABLE = "unreachable"
PROBE_NOT_APPLICABLE = "not_applicable"

#: The closed probe vocabulary. `not_applicable` is a status rather than an
#: omission for the same reason `doctor.STATUSES` carries `skipped`.
PROBE_STATUSES = (PROBE_OK, PROBE_INVALID, PROBE_UNREACHABLE, PROBE_NOT_APPLICABLE)

_PROBE_TIMEOUT_SECONDS = 5.0


class EndpointConfigError(ValueError):
    """An endpoint environment variable is not something encore can act on."""


class _JsonFetcher(Protocol):
    """The one operation the probe needs, so tests need no server."""

    def __call__(self, url: str, *, timeout: float) -> tuple[int, str, bytes]:
        """Return ``(status_code, content_type, body)`` or raise on transport failure."""


@dataclass(frozen=True, slots=True)
class Endpoints:
    """The resolved endpoint configuration for this process."""

    mb_base_url: str
    lb_base_url: str
    cover_art_base_url: str
    #: Requests per second actually permitted against `mb_base_url`.
    mb_rate_limit: float
    #: True when `mb_base_url` is NOT MetaBrainz's own host.
    mb_is_mirror: bool
    #: Everything that was overridden, ignored, or defaulted. Log once; show
    #: in `encore doctor`. Never contains a configured value verbatim beyond
    #: a scheme+host, so a note can be logged without leaking userinfo.
    notes: tuple[str, ...] = ()

    @property
    def mb_host(self) -> str:
        """Return the hostname encore's MusicBrainz reads are pointed at."""
        return urlsplit(self.mb_base_url).hostname or ""

    @property
    def mb_min_interval(self) -> float:
        """The limiter interval, in seconds, implied by `mb_rate_limit`."""
        return 1.0 / self.mb_rate_limit

    def describe(self) -> str:
        """One line naming the endpoint in use. Safe to log and to print."""
        kind = "self-hosted mirror" if self.mb_is_mirror else "public MetaBrainz"
        return (
            f"metadata endpoint: {self.mb_base_url} ({kind}), "
            f"{self.mb_rate_limit:g} req/s; "
            f"listenbrainz: {self.lb_base_url}; cover art: {self.cover_art_base_url}"
        )


@dataclass(frozen=True, slots=True)
class EndpointProbe:
    """What a startup probe of the configured metadata endpoint found."""

    status: str
    detail: str

    def __post_init__(self) -> None:
        """Refuse a status outside the closed vocabulary."""
        if self.status not in PROBE_STATUSES:
            raise ValueError(f"unknown probe status {self.status!r}")

    @property
    def blocks_polling(self) -> bool:
        """Whether MB-dependent work must not start.

        `not_applicable` does not block, and that is a policy decision rather
        than an unchecked pass: it is only ever returned for the pinned public
        endpoint, whose address is a constant in this file. Every endpoint
        encore was *told* about is probed before anything polls it.
        """
        return self.status in {PROBE_INVALID, PROBE_UNREACHABLE}


def _clean_base_url(raw: str, env_var: str, default: str) -> tuple[str, str | None]:
    """Validate one configured base URL. Returns ``(url, note_or_None)``."""
    value = raw.strip()
    if not value:
        return default, None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise EndpointConfigError(
            f"{env_var} must be an absolute http(s) URL with a host; "
            f"encore does not fall back to {default} when it is not"
        )
    if parts.username or parts.password:
        # Deliberately does not echo the value: it holds a credential, and
        # this message is destined for logs, `/readyz` and `encore doctor`.
        raise EndpointConfigError(
            f"{env_var} carries userinfo (user:password@host). encore will not "
            f"accept a credential in an endpoint URL, and will not print it. "
            f"Put the mirror behind a network boundary or a proxy instead."
        )
    if parts.query or parts.fragment:
        raise EndpointConfigError(f"{env_var} must be a base URL, with no query or fragment")
    cleaned = f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
    return cleaned, f"{env_var} is set: using {cleaned} instead of {default}"


def _is_public_mb(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host == PUBLIC_MB_HOST or host.endswith(f".{PUBLIC_MB_HOST}")


def _resolve_rate_limit(raw: str, *, is_mirror: bool) -> tuple[float, str | None]:
    """Return the effective MB rate and why it is that, not what was asked for."""
    value = raw.strip()
    if not value:
        return PUBLIC_MB_RATE_LIMIT, None
    if not is_mirror:
        return (
            PUBLIC_MB_RATE_LIMIT,
            f"{MB_RATE_LIMIT_ENV}={value} IGNORED: the public MetaBrainz host is pinned at "
            f"{PUBLIC_MB_RATE_LIMIT:g} req/s. Set {MB_BASE_URL_ENV} to a mirror to raise it.",
        )
    try:
        parsed = float(value)
    except ValueError:
        return (
            PUBLIC_MB_RATE_LIMIT,
            f"{MB_RATE_LIMIT_ENV}={value!r} is not a number; falling back to the safe "
            f"{PUBLIC_MB_RATE_LIMIT:g} req/s rather than guessing a faster one.",
        )
    if not 0 < parsed <= MAX_MIRROR_RATE_LIMIT:
        return (
            PUBLIC_MB_RATE_LIMIT,
            f"{MB_RATE_LIMIT_ENV}={value} is outside (0, {MAX_MIRROR_RATE_LIMIT:g}]; falling "
            f"back to the safe {PUBLIC_MB_RATE_LIMIT:g} req/s.",
        )
    return parsed, f"{MB_RATE_LIMIT_ENV}={parsed:g} req/s honoured (mirror, not the public host)"


def resolve_endpoints(environ: Mapping[str, str] | None = None) -> Endpoints:
    """Resolve the three base URLs and the MB rate from the environment.

    Raises:
        EndpointConfigError: a configured URL is malformed or carries a
            credential. Deliberately an error and not a fallback — see the
            module docstring's rule two.
    """
    env = os.environ if environ is None else environ
    notes: list[str] = []

    mb_base_url, note = _clean_base_url(
        env.get(MB_BASE_URL_ENV, ""), MB_BASE_URL_ENV, PUBLIC_MB_BASE_URL
    )
    if note:
        notes.append(note)
    lb_base_url, note = _clean_base_url(
        env.get(LB_BASE_URL_ENV, ""), LB_BASE_URL_ENV, PUBLIC_LB_BASE_URL
    )
    if note:
        notes.append(note)
    cover_art_base_url, note = _clean_base_url(
        env.get(COVER_ART_BASE_URL_ENV, ""), COVER_ART_BASE_URL_ENV, PUBLIC_COVER_ART_BASE_URL
    )
    if note:
        notes.append(note)

    is_mirror = not _is_public_mb(mb_base_url)
    rate_limit, note = _resolve_rate_limit(env.get(MB_RATE_LIMIT_ENV, ""), is_mirror=is_mirror)
    if note:
        notes.append(note)

    return Endpoints(
        mb_base_url=mb_base_url,
        lb_base_url=lb_base_url,
        cover_art_base_url=cover_art_base_url,
        mb_rate_limit=rate_limit,
        mb_is_mirror=is_mirror,
        notes=tuple(notes),
    )


def _httpx_fetch(url: str, *, timeout: float) -> tuple[int, str, bytes]:
    """Fetch one URL with the encore User-Agent. Imported lazily to keep this module cheap."""
    import httpx

    from encore import __version__

    response = httpx.get(
        url,
        timeout=httpx.Timeout(timeout),
        headers={
            "User-Agent": f"encore/{__version__} (https://github.com/ChelseaKR/encore)",
            "Accept": "application/json",
        },
    )
    return response.status_code, response.headers.get("content-type", ""), response.content


def probe_metadata_endpoint(
    endpoints: Endpoints,
    fetch: _JsonFetcher | None = None,
) -> EndpointProbe:
    """Check that a configured mirror actually speaks the MusicBrainz web service.

    A mirror that is up, serving HTTPS, and answering an nginx welcome page is
    indistinguishable from a working one by any check that only asks whether
    the host is reachable — and encore would then poll it forever and record
    nothing. So the probe asks for a MusicBrainz resource and requires a
    MusicBrainz answer.

    The public endpoint is not probed. That is reported as `not_applicable`
    with the reason, never as `ok`.
    """
    if not endpoints.mb_is_mirror:
        return EndpointProbe(
            PROBE_NOT_APPLICABLE,
            "the public MetaBrainz endpoint is not probed: its address is a constant, and a "
            "request to donation-funded infrastructure on every boot is not free",
        )
    url = f"{endpoints.mb_base_url}/artist/{PROBE_ARTIST_MBID}?fmt=json"
    fetcher = fetch if fetch is not None else _httpx_fetch
    try:
        status_code, content_type, body = fetcher(url, timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        return EndpointProbe(
            PROBE_UNREACHABLE,
            f"{endpoints.mb_base_url} did not answer ({type(exc).__name__})",
        )
    if status_code != 200:
        return EndpointProbe(
            PROBE_INVALID,
            f"metadata endpoint invalid: {endpoints.mb_base_url} answered HTTP {status_code} "
            f"for a MusicBrainz artist lookup",
        )
    if "json" not in content_type.lower():
        return EndpointProbe(
            PROBE_INVALID,
            f"metadata endpoint invalid: {endpoints.mb_base_url} answered "
            f"{content_type or 'no content type'}, not JSON — this does not look like a "
            f"MusicBrainz web service",
        )
    import json

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return EndpointProbe(
            PROBE_INVALID,
            f"metadata endpoint invalid: {endpoints.mb_base_url} answered unparseable JSON ({exc})",
        )
    if not isinstance(payload, dict) or payload.get("id") != PROBE_ARTIST_MBID:
        return EndpointProbe(
            PROBE_INVALID,
            f"metadata endpoint invalid: {endpoints.mb_base_url} answered JSON that is not the "
            f"MusicBrainz artist that was asked for",
        )
    return EndpointProbe(
        PROBE_OK,
        f"{endpoints.mb_base_url} answered a MusicBrainz artist lookup at "
        f"{endpoints.mb_rate_limit:g} req/s",
    )
