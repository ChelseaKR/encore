"""The FastAPI app factory.

M1-F0 scope: health endpoints plus the storage layer. `/livez` and `/readyz`
are kept distinct per OBS-18/19/20 — `readyz` now performs a real database
check (storage opens at startup; the probe runs a trivial query per request);
the scheduler-heartbeat check joins it at M2 with the first scheduled poller.
`livez` never depends on anything but the process being up, so it can't
false-negative during a slow dependency check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from encore import __version__
from encore.storage import Storage, StorageError


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    """Build the FastAPI application.

    ``data_dir`` follows `encore.storage.resolve_data_dir` precedence
    (explicit argument > ``$ENCORE_DATA_DIR`` > ``./data``). The storage
    layer opens at startup (lifespan) — building the app object itself has
    no filesystem side effects, and a broken data directory fails the boot
    loudly instead of leaving a half-ready server up.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.storage = Storage(data_dir)
        try:
            yield
        finally:
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
