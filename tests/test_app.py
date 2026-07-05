"""M0 smoke tests: the app factory builds and the health endpoints respond.

Real coverage of the sync/match/watch/notify pipelines lands with the
features that implement them (M1+); this file exists so `make verify`
has something to run against the empty scaffold.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from encore.app import create_app


def test_livez_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
