"""F5 feed routes: the capability token gates everything, failures leak nothing."""

from __future__ import annotations

import logging
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from encore.app import create_app
from encore.storage import Storage
from tests.notify_fixtures import ARTIST_NAME, RELEASE_TITLE, seed_event


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


@pytest.mark.no_secrets_in_logs
def test_feed_token_never_reaches_encore_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Scope: encore's own loggers (OBS-11). A web *server's* access log
    # inevitably sees the URL — that operator-host exposure is documented in
    # docs/adr/0013 — but no encore log line may ever carry the token.
    token = _mint(tmp_path)
    with caplog.at_level(logging.DEBUG), TestClient(create_app(tmp_path)) as client:
        assert client.get(f"/feeds/{token}/releases.xml").status_code == 200
        assert client.get(f"/feeds/{token}/upcoming.ics").status_code == 200
        assert client.get("/feeds/wrong-token/releases.xml").status_code == 404
    encore_records = [rec for rec in caplog.records if rec.name.startswith("encore")]
    assert encore_records  # the capture must not pass vacuously
    for record in encore_records:
        assert token not in record.getMessage()
