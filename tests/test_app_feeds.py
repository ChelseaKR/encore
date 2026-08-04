"""F5 feed routes: the capability token gates everything, failures leak nothing."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from encore import cli
from encore.app import FEED_CACHE_CONTROL, create_app
from encore.feeds import ICAL_EVENT_LIMIT
from encore.models import UpcomingReleaseView
from encore.storage import DATA_DIR_ENV, KEY_FILENAME, Storage
from tests.notify_fixtures import ARTIST_NAME, RELEASE_TITLE, seed_event

RSS_PATH = "/feeds/{token}/releases.xml"
ICS_PATH = "/feeds/{token}/upcoming.ics"


def _mint(tmp_path: Path, with_event: bool = True) -> str:
    """Seed one event (a sentinel artist) and mint the feed token."""
    storage = Storage(tmp_path)
    if with_event:
        seed_event(storage, kind="upcoming", first_release_date="2100-09-15")
    token = storage.ensure_feed_token()
    storage.close()
    return token


def test_rss_with_the_valid_token(tmp_path: Path) -> None:
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/feeds/{token}/releases.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/rss+xml")
    root = ET.fromstring(response.text)  # noqa: S314 - parsing our own trusted test output
    assert root.tag == "rss"
    assert RELEASE_TITLE in response.text
    assert ARTIST_NAME in response.text


def test_ical_with_the_valid_token(tmp_path: Path) -> None:
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/feeds/{token}/upcoming.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in response.text
    assert "DTSTART;VALUE=DATE:21000915" in response.text
    assert ARTIST_NAME in response.text


@pytest.mark.no_outing
def test_wrong_token_is_a_bare_404_with_no_taste_data(tmp_path: Path) -> None:
    _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        rss = client.get("/feeds/not-the-token/releases.xml")
        ics = client.get("/feeds/not-the-token/upcoming.ics")
        unknown_route = client.get("/feeds/not-the-token/other.json")
    for response in (rss, ics):
        assert response.status_code == 404
        # Indistinguishable from a route that does not exist at all.
        assert response.json() == unknown_route.json()
        assert ARTIST_NAME not in response.text
        assert RELEASE_TITLE not in response.text


@pytest.mark.no_outing
def test_unminted_token_state_is_the_same_404(tmp_path: Path) -> None:
    # No token has ever been minted: there is nothing to guess, and the
    # response must not reveal that feeds exist but are "not set up yet".
    storage = Storage(tmp_path)
    seed_event(storage)
    storage.close()
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/feeds/anything/releases.xml")
    assert response.status_code == 404
    assert ARTIST_NAME not in response.text


def test_before_startup_the_routes_404(tmp_path: Path) -> None:
    token = _mint(tmp_path)
    # No lifespan: storage never opened — same opaque 404, not a 500.
    client = TestClient(create_app(tmp_path))
    assert client.get(f"/feeds/{token}/releases.xml").status_code == 404


def test_rotation_revokes_the_old_url_immediately(tmp_path: Path) -> None:
    old_token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/feeds/{old_token}/releases.xml").status_code == 200
        app_storage = client.app.state.storage  # type: ignore[attr-defined]
        new_token = app_storage.rotate_feed_token()
        assert client.get(f"/feeds/{old_token}/releases.xml").status_code == 404
        assert client.get(f"/feeds/{new_token}/releases.xml").status_code == 200


def test_head_is_answered_like_a_reader_expects(tmp_path: Path) -> None:
    # Feed readers and calendar clients probe with HEAD before they GET; a 405
    # there is a feed that "does not subscribe cleanly" (docs/adr/0013).
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        rss = client.head(RSS_PATH.format(token=token))
        ics = client.head(ICS_PATH.format(token=token))
    assert rss.status_code == 200
    assert rss.headers["content-type"].startswith("application/rss+xml")
    assert ics.status_code == 200
    assert ics.headers["content-type"].startswith("text/calendar")


def test_both_feeds_forbid_caching(tmp_path: Path) -> None:
    # A capability URL in a shared proxy cache, or on disk in the reader, is
    # the feed available to someone who never held the token.
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        for path in (RSS_PATH, ICS_PATH):
            response = client.get(path.format(token=token))
            assert response.status_code == 200
            assert response.headers["cache-control"] == FEED_CACHE_CONTROL
    assert FEED_CACHE_CONTROL == "private, no-store"


@pytest.mark.no_outing
def test_an_undecryptable_token_is_the_same_404_not_a_traceback(tmp_path: Path) -> None:
    # The ADR-0008 case: a database restored beside a key that does not match
    # it. `get_feed_token` raises SecretDecryptionError, which used to escape
    # as a 500 whose traceback confirmed both the route and the database.
    token = _mint(tmp_path)
    (tmp_path / KEY_FILENAME).write_bytes(Fernet.generate_key())
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(RSS_PATH.format(token=token))
        unknown_route = client.get("/feeds/anything/other.json")
    assert response.status_code == 404
    assert response.json() == unknown_route.json()
    assert ARTIST_NAME not in response.text


@pytest.mark.no_outing
def test_no_schema_or_docs_surface_publishes_the_gated_url(tmp_path: Path) -> None:
    # /openapi.json, /docs and /redoc are unauthenticated by nature; all three
    # used to hand back the exact `/feeds/{token}/...` template plus the
    # product name and version (docs/adr/0013 §Decision 2).
    _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        unknown_route = client.get("/definitely-not-a-route")
        for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json() == unknown_route.json(), path
            assert "feeds" not in response.text, path


@pytest.mark.no_outing
def test_a_wrong_method_on_a_feed_path_is_the_same_404(tmp_path: Path) -> None:
    # Method dispatch is a route-shape oracle: 405 on a feed path where an
    # unknown path answers 404 confirms the path exists without the token.
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        unknown_route = client.post("/definitely-not-a-route")
        assert unknown_route.status_code == 404
        for path in (RSS_PATH, ICS_PATH):
            for candidate in (token, "not-the-token"):
                url = path.format(token=candidate)
                for response in (
                    client.post(url),
                    client.put(url),
                    client.patch(url),
                    client.delete(url),
                    client.options(url),
                    client.request("FROBNICATE", url),
                ):
                    assert response.status_code == 404, (url, response.request.method)
                    assert response.json() == unknown_route.json()
                    # The `Allow` header is the same disclosure by another name.
                    assert "allow" not in response.headers


@pytest.mark.no_outing
def test_a_trailing_slash_does_not_confirm_the_route(tmp_path: Path) -> None:
    # Starlette's slash redirect fires only for a path that exists, so a 307
    # here would answer the question the 404 refuses to.
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        unknown_route = client.get("/definitely-not-a-route/", follow_redirects=False)
        for path in (RSS_PATH, ICS_PATH):
            for candidate in (token, "not-the-token"):
                response = client.get(path.format(token=candidate) + "/", follow_redirects=False)
                assert response.status_code == 404
                assert response.json() == unknown_route.json()


def test_the_calendar_is_capped_and_keeps_the_soonest_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The RSS route has always had a stated cap; the calendar now has a ceiling
    # too. Entries arrive soonest-first, so truncation drops the far future.
    overflow = ICAL_EVENT_LIMIT + 5
    views = [
        UpcomingReleaseView(
            release_group_mbid=f"{index:08d}-0000-0000-0000-000000000000",
            title=f"Release {index}",
            primary_type="Album",
            secondary_types=(),
            first_release_date=(f"21{index // 365:02d}-01-01" if index else "2100-01-01"),
            artist_mbid="11111111-2222-3333-4444-555555555555",
            artist_name=f"Artist {index}",
        )
        for index in range(overflow)
    ]
    monkeypatch.setattr(Storage, "list_upcoming_releases", lambda self, today=None: views)
    token = _mint(tmp_path)
    with TestClient(create_app(tmp_path)) as client:
        body = client.get(ICS_PATH.format(token=token)).text
    assert body.count("BEGIN:VEVENT") == ICAL_EVENT_LIMIT
    assert "Release 0" in body
    assert f"Release {overflow - 1}" not in body


def _serve_once(app_target: str, kwargs: dict[str, Any], path: str) -> int:
    """Run a real uvicorn server for exactly one request; return its status.

    A `TestClient` never reaches uvicorn, which is precisely why a guard that
    only used one could not see the access log. This starts the server encore
    ships, with the arguments `encore serve` actually passes.
    """
    config = uvicorn.Config(app_target, **{**kwargs, "host": "127.0.0.1", "port": 0})
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn never started"
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=30).status_code
    finally:
        server.should_exit = True
        thread.join(timeout=30)


@pytest.mark.no_secrets_in_logs
def test_the_shipped_server_writes_no_feed_token_to_its_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # OBS-11, the real path: uvicorn's access log is a stream of its own, with
    # `propagate = False`, so a caplog-only guard inspected the wrong place
    # entirely while every feed poll printed the capability URL — token and
    # all — to stdout, i.e. to `docker logs`, forever.
    token = _mint(tmp_path)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    captured: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        cli.uvicorn, "run", lambda app, **kwargs: captured.append((app, kwargs)), raising=True
    )
    assert cli.main(["serve", "--data-dir", str(tmp_path)]) == 0
    app_target, serve_kwargs = captured[0]
    path = RSS_PATH.format(token=token)

    # 1. The server as `encore serve` configures it: the token is in no stream.
    assert _serve_once(app_target, serve_kwargs, path) == 200
    shipped = capfd.readouterr()
    assert shipped.out or shipped.err  # the capture must not pass vacuously
    assert token not in shipped.out
    assert token not in shipped.err

    # 2. The positive control, so (1) is a statement about `access_log=False`
    #    and not about a capture that would have seen nothing either way: the
    #    identical harness with uvicorn's default access log *does* print it.
    assert _serve_once(app_target, {**serve_kwargs, "access_log": True}, path) == 200
    leaky = capfd.readouterr()
    assert token in leaky.out + leaky.err


@pytest.mark.no_secrets_in_logs
def test_feed_token_never_reaches_encore_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The narrower half of OBS-11: encore's own loggers. The access-log path
    # is pinned above; this pins that no application log line carries the
    # token either, including on the failure branches.
    token = _mint(tmp_path)
    with caplog.at_level(logging.DEBUG), TestClient(create_app(tmp_path)) as client:
        assert client.get(RSS_PATH.format(token=token)).status_code == 200
        assert client.get(ICS_PATH.format(token=token)).status_code == 200
        assert client.get("/feeds/wrong-token/releases.xml").status_code == 404
    encore_records = [rec for rec in caplog.records if rec.name.startswith("encore")]
    assert encore_records  # the capture must not pass vacuously
    for record in encore_records:
        assert token not in record.getMessage()
