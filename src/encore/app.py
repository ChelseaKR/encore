"""The FastAPI app factory.

M0 scope: health endpoints only. `/livez` and `/readyz` are kept distinct per
OBS-18/19/20 — `readyz` is where the DB and scheduler heartbeat checks land
once they exist (M1+); `livez` never depends on anything but the process
being up, so it can't false-negative during a slow dependency check.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="encore", version="0.1.0")

    @app.get("/livez")
    def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        # No DB or scheduler yet (M0) — ready the moment the process is live.
        # Gains real checks (DB open, scheduler heartbeat) at M1/M2.
        return {"status": "ok"}

    return app


app = create_app()
