"""The FastAPI app factory.

M1 scope (F0 + F1): health endpoints, the storage layer, and the background
sync scheduler. `/livez` and `/readyz` are kept distinct per OBS-18/19/20 —
`readyz` performs a real database check (storage opens at startup; the probe
runs a trivial query per request); the scheduler-heartbeat check joins it at
M2 with the MusicBrainz poller (F3). `livez` never depends on anything but
the process being up, so it can't false-negative during a slow dependency
check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from encore import __version__
from encore.scheduler import build_sync_scheduler
from encore.storage import Storage, StorageError


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    """Build the FastAPI application.

    ``data_dir`` follows `encore.storage.resolve_data_dir` precedence
    (explicit argument > ``$ENCORE_DATA_DIR`` > ``./data``). The storage
    layer opens at startup (lifespan) — building the app object itself has
    no filesystem side effects, and a broken data directory fails the boot
    loudly instead of leaving a half-ready server up. The sync scheduler
    (F1) starts alongside it when Plex credentials are configured; no
    network traffic happens at boot either way (the first run is one
    interval out — see `encore.scheduler`).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.storage = Storage(data_dir)
        app.state.scheduler = build_sync_scheduler(app.state.storage)
        try:
            yield
        finally:
            if app.state.scheduler is not None:
                app.state.scheduler.shutdown(wait=False)
                app.state.scheduler = None
            app.state.storage.close()
            app.state.storage = None

    app = FastAPI(title="encore", version=__version__, lifespan=lifespan)

    @app.get("/livez")
    def livez() -> dict[str, str]:
        # Deliberately dependency-free: process-is-up only (OBS-19).
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        # Real DB check (M1-F0): the storage layer must be open and answering.
        storage: Storage | None = getattr(app.state, "storage", None)
        if storage is None:
            return JSONResponse(
                status_code=503,
                content={"status": "unready", "checks": {"db": "storage not initialized"}},
            )
        try:
            storage.check_ready()
        except StorageError:
            return JSONResponse(
                status_code=503,
                content={"status": "unready", "checks": {"db": "unavailable"}},
            )
        return JSONResponse(status_code=200, content={"status": "ok", "checks": {"db": "ok"}})

    return app


app = create_app()
