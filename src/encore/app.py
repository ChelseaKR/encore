"""The FastAPI app factory.

Scope so far (F0 + F1 + F3 + F4 + F5): health endpoints, the storage layer,
the three background schedulers (Plex sync, MusicBrainz release watch,
notification delivery), and the token-gated standing feeds (RSS + iCal). The
feed routes are the app's first read surface for library content, and they
ship *with* their access control: the unguessable token in the path is the
capability, checked in constant time against the encrypted-at-rest stored
token, and every response an unauthorized request can elicit from a feed path
— no storage, no token minted, wrong token, a key that no longer decrypts, a
method other than GET/HEAD, a trailing slash — is the byte-identical bare 404
of a route that does not exist. The app publishes no OpenAPI schema, no
`/docs` and no `/redoc`, because those three would hand back the gated URL
template for free; response *shape* therefore tells a prober nothing about
the feed routes (traffic volume and timing are outside what a status code can
hide). That is the no-outing lens, docs/adr/0012 §4 applied to F5. The
unauthenticated in-app *event* feed remains CLI-only until F6 brings the
admin password. `/livez` and `/readyz` are kept distinct per OBS-18/19/20 —
`readyz` performs a real database check plus a scheduler check (a started
scheduler that has died makes the instance unready; a deliberately disabled
or credential-gated one does not). `livez` never depends on anything but the
process being up, so it can't false-negative during a slow dependency check.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from encore import __version__
from encore.feeds import ICAL_EVENT_LIMIT, RSS_EVENT_LIMIT, render_ical, render_rss
from encore.scheduler import build_notify_scheduler, build_sync_scheduler, build_watch_scheduler
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage, StorageError

# readyz check name → app.state attribute holding the (optional) scheduler.
_SCHEDULER_CHECKS = (
    ("sync_scheduler", "scheduler"),
    ("watch_scheduler", "watch_scheduler"),
    ("notify_scheduler", "notify_scheduler"),
)

# Everything under this prefix is capability-gated and must never confirm its
# own existence to a request that did not present the token.
FEED_PATH_PREFIX = "/feeds/"

# A feed body is one household's taste data behind a bearer URL. `private`
# keeps it out of any shared cache the operator puts in front; `no-store` keeps
# it off disk in the reader, where the next person at that machine would find
# the feed without ever holding the token.
FEED_CACHE_CONTROL = "private, no-store"


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


def _register_opaque_errors(app: FastAPI) -> None:
    """Answer a feed-path method mismatch as the bare 404, not 405 + `Allow`.

    Starlette replies to a POST against a GET-only route with `405 Method Not
    Allowed` and an `Allow` header, which confirms the path is real even when
    the token is wrong — the one thing the feed routes must never confirm.
    Every other error keeps FastAPI's default handling, and the substituted
    404 is produced by that same default handler, so it is byte-identical to
    the 404 of a genuinely unknown route.
    """

    @app.exception_handler(StarletteHTTPException)
    async def opaque_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code == 405 and request.url.path.startswith(FEED_PATH_PREFIX):
            exc = StarletteHTTPException(status_code=404)
        return await http_exception_handler(request, exc)


def _register_feed_routes(app: FastAPI) -> None:
    """Attach the F5 standing-feed routes (RSS + iCal) to the app."""

    def _feed_storage(candidate_token: str) -> Storage:
        """Return the storage layer iff ``candidate_token`` is *the* feed token.

        Every failure shape — storage not initialized, no token ever minted,
        wrong token, a key file that can no longer decrypt the stored one —
        raises the same bare 404: a capability URL either works or does not
        exist, and distinguishing "not yet" from "wrong" would hand an
        unauthorized prober information. The comparison is constant-time (a
        timing oracle on a secret compare is the classic way capability
        tokens fall).
        """
        storage: Storage | None = getattr(app.state, "storage", None)
        if storage is not None:
            try:
                stored_token = storage.get_feed_token()
            except (StorageError, SecretDecryptionError):
                # A database restored without its matching key (docs/adr/0008),
                # or one that has become unreadable, can prove no token at all.
                # That is a feed which does not exist — not a 500 whose
                # traceback confirms to a prober that it does. `/readyz` is
                # where the operator learns the database is unhappy.
                stored_token = None
            if stored_token is not None and secrets.compare_digest(
                candidate_token.encode(), stored_token.encode()
            ):
                return storage
        raise HTTPException(status_code=404)

    # GET *and* HEAD: feed readers and calendar clients probe with HEAD before
    # fetching, and FastAPI — unlike bare Starlette — does not add HEAD to a
    # GET route for free, so a HEAD-first reader used to be told 405.
    @app.api_route("/feeds/{token}/releases.xml", methods=["GET", "HEAD"])
    def rss_feed(token: str) -> Response:
        # The F5 RSS feed: newest release events, rendered once, shared with F4.
        storage = _feed_storage(token)
        views = storage.list_event_views(limit=RSS_EVENT_LIMIT)
        machine_identifier = storage.get_plex_machine_identifier()
        return Response(
            content=render_rss(views, machine_identifier),
            media_type="application/rss+xml; charset=utf-8",
            headers={"Cache-Control": FEED_CACHE_CONTROL},
        )

    @app.api_route("/feeds/{token}/upcoming.ics", methods=["GET", "HEAD"])
    def ical_feed(token: str) -> Response:
        # The F5 iCal feed: announced-but-not-out releases as all-day entries.
        # The cap is a ceiling, not a window: the query is already bounded by
        # reality (day-precision, future-dated announcements for artists still
        # in the library), and entries arrive soonest-first, so truncation can
        # only ever drop the most distant — never the release next month. See
        # ICAL_EVENT_LIMIT for why a calendar's ceiling is not RSS's 100.
        storage = _feed_storage(token)
        releases = storage.list_upcoming_releases()[:ICAL_EVENT_LIMIT]
        return Response(
            content=render_ical(releases),
            media_type="text/calendar; charset=utf-8",
            headers={"Cache-Control": FEED_CACHE_CONTROL},
        )


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

    app = FastAPI(
        title="encore",
        version=__version__,
        lifespan=lifespan,
        # No schema, no `/docs`, no `/redoc`. All three are unauthenticated by
        # nature, and all three would publish the exact gated URL template
        # (`/feeds/{token}/releases.xml`) alongside the product name and
        # version — everything the feed routes' bare 404 exists to withhold.
        # encore has no third-party API consumer to serve a schema to, and
        # the one person entitled to the feed URLs gets them from
        # `encore feeds show`, which is the only surface meant to hand them
        # out. Route documentation lives in docs/adr/0013.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        # A trailing-slash redirect fires only for a path that *does* exist,
        # so leaving it on answers "is /feeds/<guess>/releases.xml real?" with
        # a 307 where an unknown path gets a 404 — the same disclosure by
        # another door.
        redirect_slashes=False,
    )

    _register_opaque_errors(app)

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

    _register_feed_routes(app)
    return app


app = create_app()
