"""Self-hosted mirrors (#63): where encore reads metadata, how fast, and what it refuses.

Two rules carry this file, and both are refusals rather than features.

**The public host is pinned at 1 req/s whatever the operator sets.** An
installation that hammers donation-funded infrastructure because somebody
exported a variable is a failure encore should not be able to have, so
`ENCORE_MB_RATE_LIMIT` is honoured only against a mirror — and the discarded
value is *named*, in `encore doctor` and in the startup log, because silently
ignoring configuration is how an afternoon disappears.

**encore never silently falls back to the public host.** Pointing encore at a
mirror moves a library's artist names and MBIDs onto the operator's own
network; a fallback would move them back off it without anyone being told. A
malformed, credential-bearing, unreachable or non-MusicBrainz endpoint is
therefore an error that surfaces at `/readyz`, in `doctor`, and by MB-dependent
polling not starting — never one that repairs itself by substituting
`musicbrainz.org`.

The probe exists because "the host is reachable" and "the host speaks the
MusicBrainz web service" are different questions, and a mirror serving an nginx
welcome page answers the first one perfectly while encore polls it forever and
records nothing.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from encore.app import _metadata_endpoint_statuses, create_app
from encore.doctor import CheckResult, run_checks
from encore.endpoints import (
    COVER_ART_BASE_URL_ENV,
    LB_BASE_URL_ENV,
    MB_BASE_URL_ENV,
    MB_RATE_LIMIT_ENV,
    PROBE_ARTIST_MBID,
    PROBE_INVALID,
    PROBE_NOT_APPLICABLE,
    PROBE_OK,
    PROBE_STATUSES,
    PROBE_UNREACHABLE,
    PUBLIC_COVER_ART_BASE_URL,
    PUBLIC_LB_BASE_URL,
    PUBLIC_MB_BASE_URL,
    PUBLIC_MB_RATE_LIMIT,
    EndpointConfigError,
    EndpointProbe,
    Endpoints,
    probe_metadata_endpoint,
    resolve_endpoints,
)
from encore.matching.mb import MB_RATE_LIMITER, MusicBrainzClient, mb_rate_limiter
from encore.notify.render import cover_art_url
from encore.recommend.lb import ListenBrainzClient
from tests.mb_fixtures import mb_search_response

MIRROR = "https://mb.lan.example/ws/2"
ENDPOINT_ENV = (MB_BASE_URL_ENV, LB_BASE_URL_ENV, COVER_ART_BASE_URL_ENV, MB_RATE_LIMIT_ENV)


@pytest.fixture(autouse=True)
def _clean_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No endpoint variable leaks in, and the shared limiter leaks nothing out.

    `mb_rate_limiter()` mutates the process-wide `MB_RATE_LIMITER` — there is
    one MusicBrainz endpoint per process, so there is one budget — which means
    a test that speeds it up would otherwise hand the next module a limiter
    that no longer paces anything.
    """
    for name in ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    original = MB_RATE_LIMITER.min_interval
    yield
    MB_RATE_LIMITER.min_interval = original


def _artist_payload(mbid: str = PROBE_ARTIST_MBID) -> dict[str, object]:
    return {"id": mbid, "name": "Various Artists", "sort-name": "Various Artists"}


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------


def test_an_unconfigured_install_reads_the_public_metabrainz_endpoints() -> None:
    resolved = resolve_endpoints()
    assert resolved.mb_base_url == PUBLIC_MB_BASE_URL
    assert resolved.lb_base_url == PUBLIC_LB_BASE_URL
    assert resolved.cover_art_base_url == PUBLIC_COVER_ART_BASE_URL
    assert resolved.mb_is_mirror is False
    assert resolved.mb_rate_limit == PUBLIC_MB_RATE_LIMIT
    assert resolved.notes == ()


def test_a_configured_mirror_is_used_and_recognised_as_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR + "/")
    resolved = resolve_endpoints()
    assert resolved.mb_base_url == MIRROR
    assert resolved.mb_host == "mb.lan.example"
    assert resolved.mb_is_mirror is True
    assert any(MB_BASE_URL_ENV in note for note in resolved.notes)


def test_a_subdomain_of_musicbrainz_org_is_still_the_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise `beta.musicbrainz.org` would quietly unpin the rate limit."""
    monkeypatch.setenv(MB_BASE_URL_ENV, "https://beta.musicbrainz.org/ws/2")
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "50")
    resolved = resolve_endpoints()
    assert resolved.mb_is_mirror is False
    assert resolved.mb_rate_limit == PUBLIC_MB_RATE_LIMIT


def test_the_public_host_ignores_an_operator_rate_and_says_which_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "25")
    resolved = resolve_endpoints()
    assert resolved.mb_rate_limit == PUBLIC_MB_RATE_LIMIT
    assert resolved.mb_min_interval == 1.0
    ignored = [note for note in resolved.notes if "IGNORED" in note]
    assert len(ignored) == 1
    assert "25" in ignored[0] and MB_BASE_URL_ENV in ignored[0]


def test_a_mirror_honours_the_configured_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "20")
    resolved = resolve_endpoints()
    assert resolved.mb_rate_limit == 20.0
    assert resolved.mb_min_interval == pytest.approx(0.05)
    assert not any("IGNORED" in note for note in resolved.notes)


@pytest.mark.parametrize("raw", ["fast", "0", "-3", "100000"])
def test_an_unusable_rate_falls_back_to_the_polite_one_and_names_itself(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """The fallback direction matters: never toward a faster rate than was proved safe."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, raw)
    resolved = resolve_endpoints()
    assert resolved.mb_rate_limit == PUBLIC_MB_RATE_LIMIT
    assert any(MB_RATE_LIMIT_ENV in note for note in resolved.notes)


@pytest.mark.parametrize(
    "raw", ["mb.lan.example", "ftp://mb.lan.example/ws/2", "https:///ws/2", "not a url"]
)
def test_a_malformed_base_url_is_an_error_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, raw)
    with pytest.raises(EndpointConfigError) as raised:
        resolve_endpoints()
    assert MB_BASE_URL_ENV in str(raised.value)
    assert PUBLIC_MB_BASE_URL in str(raised.value)


@pytest.mark.no_secrets_in_logs
def test_a_base_url_carrying_a_credential_is_refused_and_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This message goes to logs, `/readyz` and `doctor`. It must not carry the password."""
    monkeypatch.setenv(MB_BASE_URL_ENV, "https://admin:hunter2@mb.lan.example/ws/2")
    with pytest.raises(EndpointConfigError) as raised:
        resolve_endpoints()
    message = str(raised.value)
    assert "userinfo" in message
    assert "hunter2" not in message
    assert "admin" not in message


def test_a_query_or_fragment_makes_it_not_a_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LB_BASE_URL_ENV, "https://lb.lan.example/?token=abc")
    with pytest.raises(EndpointConfigError):
        resolve_endpoints()


def test_describe_names_the_endpoint_and_the_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "10")
    described = resolve_endpoints().describe()
    assert MIRROR in described
    assert "self-hosted mirror" in described
    assert "10 req/s" in described


# ---------------------------------------------------------------------------
# the probe
# ---------------------------------------------------------------------------


def test_the_public_endpoint_is_not_probed_and_reports_that_rather_than_ok() -> None:
    """`not_applicable` is a status, not a comfortable pass — and it opens no socket."""
    probe = probe_metadata_endpoint(resolve_endpoints())
    assert probe.status == PROBE_NOT_APPLICABLE
    assert probe.status != PROBE_OK
    assert "not probed" in probe.detail
    assert probe.blocks_polling is False


def test_a_mirror_that_answers_the_artist_lookup_probes_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    asked: list[str] = []

    def fetch(url: str, *, timeout: float) -> tuple[int, str, bytes]:
        asked.append(url)
        import json

        return 200, "application/json; charset=utf-8", json.dumps(_artist_payload()).encode()

    probe = probe_metadata_endpoint(resolve_endpoints(), fetch)
    assert probe.status == PROBE_OK
    assert probe.blocks_polling is False
    assert asked == [f"{MIRROR}/artist/{PROBE_ARTIST_MBID}?fmt=json"]


def test_a_mirror_serving_html_is_invalid_with_the_reason_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nginx-welcome-page case: reachable, HTTPS, and not MusicBrainz."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)

    def fetch(url: str, *, timeout: float) -> tuple[int, str, bytes]:
        return 200, "text/html", b"<html><body>Welcome to nginx!</body></html>"

    probe = probe_metadata_endpoint(resolve_endpoints(), fetch)
    assert probe.status == PROBE_INVALID
    assert "metadata endpoint invalid" in probe.detail
    assert probe.blocks_polling is True


@pytest.mark.parametrize(
    ("status_code", "content_type", "body"),
    [
        (503, "application/json", b"{}"),
        (200, "application/json", b"not json at all"),
        (200, "application/json", b'{"id": "00000000-0000-0000-0000-000000000000"}'),
        (200, "application/json", b"[]"),
    ],
)
def test_every_wrong_shaped_answer_is_invalid(
    monkeypatch: pytest.MonkeyPatch, status_code: int, content_type: str, body: bytes
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)

    def fetch(url: str, *, timeout: float) -> tuple[int, str, bytes]:
        return status_code, content_type, body

    assert probe_metadata_endpoint(resolve_endpoints(), fetch).status == PROBE_INVALID


def test_a_mirror_that_does_not_answer_is_unreachable_not_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different findings, different repairs: check the network, or check the software."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)

    def fetch(url: str, *, timeout: float) -> tuple[int, str, bytes]:
        raise ConnectionRefusedError("nothing listening")

    probe = probe_metadata_endpoint(resolve_endpoints(), fetch)
    assert probe.status == PROBE_UNREACHABLE
    assert "ConnectionRefusedError" in probe.detail
    assert probe.blocks_polling is True


def test_blocks_polling_partitions_the_whole_probe_vocabulary() -> None:
    """Every status has a decided answer, so a new one cannot default to "keep polling"."""
    blocking = {status for status in PROBE_STATUSES if EndpointProbe(status, "x").blocks_polling}
    assert blocking == {PROBE_INVALID, PROBE_UNREACHABLE}
    assert set(PROBE_STATUSES) - blocking == {PROBE_OK, PROBE_NOT_APPLICABLE}
    with pytest.raises(ValueError, match="unknown probe status"):
        EndpointProbe("probably fine", "x")


# ---------------------------------------------------------------------------
# the clients
# ---------------------------------------------------------------------------


def test_no_request_reaches_musicbrainz_org_when_a_mirror_is_configured(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """The privacy claim, at the transport boundary: artist names stay on the mirror."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("a real socket was opened")

    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    httpx_mock.add_response(json=mb_search_response())

    client = MusicBrainzClient(rate_limiter=type(MB_RATE_LIMITER)(min_interval=0))
    client.search_artists("Radiohead")
    client.close()

    hosts = {request.url.host for request in httpx_mock.get_requests()}
    assert hosts == {"mb.lan.example"}
    assert "musicbrainz.org" not in hosts


def test_the_shared_limiter_is_paced_for_the_configured_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "20")
    assert mb_rate_limiter().min_interval == pytest.approx(0.05)


def test_the_shared_limiter_stays_at_one_per_second_on_the_public_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "20")
    assert mb_rate_limiter().min_interval == 1.0


def test_the_shared_limiter_stays_polite_when_the_configuration_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken variable must not leave the limiter at whatever it was last set to."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "20")
    assert mb_rate_limiter().min_interval == pytest.approx(0.05)
    monkeypatch.setenv(MB_BASE_URL_ENV, "not a url")
    assert mb_rate_limiter().min_interval == 1.0


def test_a_broken_endpoint_variable_stops_a_client_from_being_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, "not a url")
    with pytest.raises(EndpointConfigError):
        MusicBrainzClient()


def test_the_listenbrainz_client_reads_the_configured_base(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    monkeypatch.setenv(LB_BASE_URL_ENV, "https://lb.lan.example")
    httpx_mock.add_response(json=[])
    client = ListenBrainzClient(rate_limiter=type(MB_RATE_LIMITER)(min_interval=0))
    client.similar_artists("89ad4ac3-39f7-470e-963a-56509c546377")
    client.close()
    assert {request.url.host for request in httpx_mock.get_requests()} == {"lb.lan.example"}


def test_cover_art_urls_use_the_configured_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(COVER_ART_BASE_URL_ENV, "https://caa.lan.example/proxy/")
    assert cover_art_url("abc-123") == "https://caa.lan.example/proxy/release-group/abc-123/front"


def test_cover_art_rendering_survives_a_broken_endpoint_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notification the user is waiting for must not fail to render over a typo."""
    monkeypatch.setenv(COVER_ART_BASE_URL_ENV, "not a url")
    assert cover_art_url("abc-123").startswith(PUBLIC_COVER_ART_BASE_URL)


# ---------------------------------------------------------------------------
# the server
# ---------------------------------------------------------------------------


def _probe_stub(probe: EndpointProbe) -> object:
    def _fake(endpoints: Endpoints, fetch: object = None) -> EndpointProbe:
        return probe

    return _fake


def test_an_invalid_metadata_endpoint_makes_readyz_unready_and_nothing_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setattr(
        "encore.app.probe_metadata_endpoint",
        _probe_stub(EndpointProbe(PROBE_INVALID, "metadata endpoint invalid: served HTML")),
    )
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["metadata_endpoint"] == PROBE_INVALID
    assert "metadata endpoint invalid" in checks["metadata_endpoint_detail"]
    # Nothing polls: the three MB/LB-dependent schedulers never started.
    assert checks["match_scheduler"] == "idle"
    assert checks["watch_scheduler"] == "idle"
    assert checks["rec_scheduler"] == "idle"
    # Delivery of what is ALREADY recorded is deliberately not gated.
    assert checks["notify_scheduler"] == "ok"


def test_an_unreachable_metadata_endpoint_is_also_unready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    monkeypatch.setattr(
        "encore.app.probe_metadata_endpoint",
        _probe_stub(EndpointProbe(PROBE_UNREACHABLE, "mb.lan.example did not answer")),
    )
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/readyz").status_code == 503


def test_a_validated_mirror_is_ready_and_polls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    for name in ("ENCORE_MATCH_INTERVAL_HOURS", "ENCORE_WATCH_INTERVAL_HOURS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "encore.app.probe_metadata_endpoint",
        _probe_stub(EndpointProbe(PROBE_OK, "answered a MusicBrainz artist lookup")),
    )
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["metadata_endpoint"] == PROBE_OK


def test_a_malformed_variable_does_not_stop_the_boot_but_does_stop_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server comes up so the operator can read WHY, and still polls nothing."""
    monkeypatch.setenv(MB_BASE_URL_ENV, "not a url")
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/readyz")
        assert client.get("/livez").status_code == 200
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["metadata_endpoint"] == PROBE_INVALID
    assert MB_BASE_URL_ENV in checks["metadata_endpoint_detail"]
    assert checks["match_scheduler"] == "idle"


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _named(results: list[CheckResult], name: str) -> CheckResult:
    return next(result for result in results if result.name == name)


def test_doctor_names_the_endpoint_this_install_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)
    check = _named(run_checks(tmp_path), "metadata_endpoint")
    assert check.status == "pass"
    assert MIRROR in check.detail


def test_doctor_warns_when_a_configured_rate_is_not_in_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set-but-discarded is a warn. A pass here is how someone loses an afternoon."""
    monkeypatch.setenv(MB_RATE_LIMIT_ENV, "40")
    check = _named(run_checks(tmp_path), "metadata_endpoint")
    assert check.status == "warn"
    assert "IGNORED" in (check.next_step or "")


def test_doctor_fails_on_a_broken_endpoint_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(MB_BASE_URL_ENV, "not a url")
    check = _named(run_checks(tmp_path), "metadata_endpoint")
    assert check.status == "fail"
    assert MB_BASE_URL_ENV in check.detail


def test_doctor_probes_the_configured_mirror_and_not_metabrainz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latent bug this fixes: `--check-upstream` reported on hosts encore does not use."""
    monkeypatch.setenv(MB_BASE_URL_ENV, "https://mb.lan.example:5000/ws/2")
    attempted: list[tuple[str, int]] = []

    def _record(address: tuple[str, int], timeout: float) -> object:
        attempted.append(address)
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", _record)
    results = run_checks(tmp_path, check_upstream=True)
    assert ("mb.lan.example", 5000) in attempted
    assert not any(host == "musicbrainz.org" for host, _ in attempted)
    assert _named(results, "upstream:musicbrainz").status == "warn"


def test_doctor_still_opens_no_socket_without_check_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a network-shaped concept must not have made the offline checklist online."""
    monkeypatch.setenv(MB_BASE_URL_ENV, MIRROR)

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("doctor opened a socket without --check-upstream")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    assert _named(run_checks(tmp_path), "metadata_endpoint").status == "pass"


def test_a_missing_probe_result_reads_unknown_and_blocks_readiness() -> None:
    """A missing result and a healthy endpoint must not render the same.

    Reached through the helper rather than through a booted app, because the
    lifespan always records a probe — which is exactly why this branch would
    otherwise sit unexercised until the day something changed that.
    """

    class _State:
        pass

    class _App:
        state = _State()

    checks, unready = _metadata_endpoint_statuses(cast("FastAPI", _App()))
    assert checks == {"metadata_endpoint": "unknown"}
    assert unready is True
