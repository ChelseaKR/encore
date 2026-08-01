"""The FastAPI app factory.

Scope so far (F0 + F1 + F3 + F4): health endpoints, the storage layer, and the
three background schedulers (Plex sync, MusicBrainz release watch,
notification delivery). There is still **no HTTP read surface for library
content** — the in-app event feed is deliberately CLI-only until F6 brings the
admin password with it, because an unauthenticated `/events` route on a
published container port would hand a household observer the exact taste feed
the no-outing lens exists to protect (docs/adr/0012). `/livez` and
`/readyz` are kept distinct per OBS-18/19/20 — `readyz` performs a real
database check plus a scheduler check (a started scheduler that has died
makes the instance unready; a deliberately disabled or credential-gated one
does not). `livez` never depends on anything but the process being up, so it
can't false-negative during a slow dependency check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from encore import __version__
from encore.scheduler import build_notify_scheduler, build_sync_scheduler, build_watch_scheduler
from encore.storage import Storage, StorageError

# readyz check name → app.state attribute holding the (optional) scheduler.
_SCHEDULER_CHECKS = (
    ("sync_scheduler", "scheduler"),
    ("watch_scheduler", "watch_scheduler"),
    ("notify_scheduler", "notify_scheduler"),
)


def _scheduler_statuses(app: FastAPI) -> tuple[dict[str, str], bool]:
    """Scheduler readyz statuses (OBS-20) and whether any dead one blocks readiness.

    A scheduler that was started and has since died means silently stale
    data — unready. One that never started (disabled via env, or sync
    without credentials) is a documented idle state, not a failure.
    """
    checks: dict[str, str] = {}
    unready = False
    for name, attr in _SCHEDULER_CHECKS:
        running = getattr(app.state, attr, None)
        if running is None:
            checks[name] = "idle"
        elif running.running:
            checks[name] = "ok"
        else:
            checks[name] = "stopped"
            unready = True
    return checks, unready


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
        app.state.watch_scheduler = build_watch_scheduler(app.state.storage)
        app.state.notify_scheduler = build_notify_scheduler(app.state.storage)
        try:
            yield
        finally:
            for attr in ("scheduler", "watch_scheduler", "notify_scheduler"):
                running = getattr(app.state, attr, None)
                if running is not None:
                    running.shutdown(wait=False)
                    setattr(app.state, attr, None)
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
        # Scheduler check (M2-F3, OBS-20) — see _scheduler_statuses.
        scheduler_checks, unready = _scheduler_statuses(app)
        checks: dict[str, str] = {"db": "ok", **scheduler_checks}
        if unready:
            return JSONResponse(status_code=503, content={"status": "unready", "checks": checks})
        return JSONResponse(status_code=200, content={"status": "ok", "checks": checks})

    return app


app = create_app()
