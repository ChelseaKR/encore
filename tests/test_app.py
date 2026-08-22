"""App-factory tests: health endpoints and the readyz DB check (M1-F0).

`/livez` stays dependency-free; `/readyz` now exercises the real storage
layer, so these tests cover ready, not-started, and DB-unavailable paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import encore.app as app_module
from encore.app import create_app
from encore.storage import Storage, StorageError


def test_livez_ok_without_lifespan(tmp_path: Path) -> None:
    # livez must answer even before startup completes (OBS-19): no context
    # manager here, so the lifespan never runs and storage never opens.
    client = TestClient(create_app(tmp_path))
    response = client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_ok_with_open_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fresh install: DB open, sync scheduler idle (no Plex credentials yet),
    # the keyless match/watch schedulers running, delivery running.
    monkeypatch.delenv("ENCORE_SYNC_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("ENCORE_MATCH_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("ENCORE_WATCH_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("ENCORE_NOTIFY_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("ENCORE_REC_INTERVAL_HOURS", raising=False)
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {
            "db": "ok",
            "sync_scheduler": "idle",
            "match_scheduler": "ok",
            "watch_scheduler": "ok",
            "notify_scheduler": "ok",
            "rec_scheduler": "ok",
        },
    }


def test_readyz_503_when_a_started_scheduler_has_died(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OBS-20 (M2-F3): a scheduler that started and then stopped means data
    # will silently go stale — the instance must report unready.
    class DeadScheduler:
        running = False

        def shutdown(self, *, wait: bool) -> None:
            """Accept the lifespan's shutdown call."""

    monkeypatch.setattr(app_module, "build_watch_scheduler", lambda _storage: DeadScheduler())
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unready"
    assert body["checks"]["watch_scheduler"] == "stopped"
    assert body["checks"]["db"] == "ok"


def test_readyz_503_before_startup(tmp_path: Path) -> None:
    # Without the lifespan, storage is never initialized: readyz must say so
    # loudly instead of claiming readiness (the M0 literal-return bug class).
    client = TestClient(create_app(tmp_path))
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unready"


def test_readyz_503_when_db_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_check(self: Storage) -> None:
        raise StorageError("simulated: database gone")

    with TestClient(create_app(tmp_path)) as client:
        monkeypatch.setattr(Storage, "check_ready", broken_check)
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unready", "checks": {"db": "unavailable"}}


def test_lifespan_opens_and_closes_storage(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app):
        assert isinstance(app.state.storage, Storage)
    assert app.state.storage is None


def test_lifespan_stops_a_running_sync_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RunningScheduler:
        def __init__(self) -> None:
            self.shutdown_calls: list[bool] = []

        def shutdown(self, *, wait: bool) -> None:
            self.shutdown_calls.append(wait)

    running = RunningScheduler()
    monkeypatch.setattr(app_module, "build_sync_scheduler", lambda _storage: running)
    app = create_app(tmp_path)

    with TestClient(app):
        assert app.state.scheduler is running

    assert running.shutdown_calls == [False]
    assert app.state.scheduler is None
