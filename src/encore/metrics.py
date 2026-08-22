"""RED metrics for the HTTP surface — the observability debt, paid (OBS-11).

The roadmap has carried this as "due, not hypothetical" since F5 landed
the first real routes: a self-hosted service needs Rate, Errors, and
Duration per route to be operable. This module is deliberately small and
dependency-free — an in-process, thread-safe registry plus Prometheus text
exposition at ``/metrics``:

    encore_http_requests_total{route="…",method="…",status="200"} N
    encore_http_request_duration_seconds_count{route="…",method="…"} N
    encore_http_request_duration_seconds_sum{route="…",method="…"} F

**Labels are route templates, never raw paths.** The F5 feed URLs embed a
capability token in the path; a metrics label built from the raw request
path would publish that token at ``/metrics`` forever and to every scrape.
The middleware reads the matched route's path template *after* routing;
anything that never matches a route is aggregated under the opaque label
``unmatched`` — 404s stay countable without confirming or leaking paths.

What the numbers are worth: counters are per-process and reset on restart
— honest about being a single-container service, not a prometheus-server
replacement. The registry is the process-global `METRICS_REGISTRY`, the
same pattern as the MetaBrainz rate limiter (`MB_RATE_LIMITER`): one
process *is* one service here, and tests that need isolation call
`MetricsRegistry.reset`. ``/metrics`` itself is unauthenticated on
purpose: it carries no taste data (templates, methods, statuses), and it
binds where the rest of encore binds (localhost by default). Operators
fronting encore with a reverse proxy can gate it there; hiding it would
just make the service unoperable from inside.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Awaitable, Callable

__all__ = [
    "METRICS_REGISTRY",
    "UNMATCHED_ROUTE",
    "MetricsRegistry",
    "RedMetricsMiddleware",
    "render_prometheus",
]

UNMATCHED_ROUTE = "unmatched"

ASGIApp = Callable[
    [
        dict[str, object],
        Callable[[], Awaitable[dict[str, object]]],
        Callable[[dict[str, object]], Awaitable[None]],
    ],
    Awaitable[None],
]


class MetricsRegistry:
    """Thread-safe RED aggregates per (route template, method)."""

    def __init__(self) -> None:
        """Start with empty aggregates and a lock guarding every read-modify-write."""
        self._lock = threading.Lock()
        # (route, method) -> {status: count}
        self._counts: dict[tuple[str, str], dict[int, int]] = {}
        # (route, method) -> [count, total_seconds, max_seconds]
        self._durations: dict[tuple[str, str], list[float]] = {}

    def observe(self, route: str, method: str, status: int, duration: float) -> None:
        """Record one finished request."""
        key = (route, method.upper())
        with self._lock:
            counts = self._counts.setdefault(key, {})
            counts[status] = counts.get(status, 0) + 1
            stats = self._durations.setdefault(key, [0, 0.0, 0.0])
            stats[0] += 1
            stats[1] += duration
            stats[2] = max(stats[2], duration)

    def reset(self) -> None:
        """Clear every aggregate (test isolation between app instances)."""
        with self._lock:
            self._counts.clear()
            self._durations.clear()

    def snapshot_counts(
        self,
    ) -> list[tuple[str, str, int, int]]:
        """Deterministic copy of the request counters for exposition."""
        rows: list[tuple[str, str, int, int]] = []
        with self._lock:
            for (route, method), statuses in sorted(self._counts.items()):
                for status in sorted(statuses):
                    rows.append((route, method, status, statuses[status]))
        return rows

    def snapshot_durations(
        self,
    ) -> list[tuple[str, str, int, float, float]]:
        """Deterministic copy of ``(route, method, count, sum, max)``."""
        rows: list[tuple[str, str, int, float, float]] = []
        with self._lock:
            for (route, method), stats in sorted(self._durations.items()):
                count, total, longest = stats
                rows.append((route, method, int(count), total, longest))
        return rows


def render_prometheus(registry: MetricsRegistry) -> str:
    """Render the registry in the Prometheus text format (version 0.0.4)."""
    lines = [
        "# HELP encore_http_requests_total HTTP requests processed.",
        "# TYPE encore_http_requests_total counter",
    ]
    for route, method, status, count in registry.snapshot_counts():
        lines.append(
            f'encore_http_requests_total{{route="{route}",method="{method}",'
            f'status="{status}"}} {count}'
        )
    lines.append(
        "# HELP encore_http_request_duration_seconds Request duration in seconds (count/sum/max)."
    )
    lines.append("# TYPE encore_http_request_duration_seconds summary")
    for route, method, count, total, _max in registry.snapshot_durations():
        lines.append(
            f'encore_http_request_duration_seconds_count{{route="{route}",'
            f'method="{method}"}} {count}'
        )
        lines.append(
            f'encore_http_request_duration_seconds_sum{{route="{route}",'
            f'method="{method}"}} {total:.6f}'
        )
    for route, method, _count, _total, longest in registry.snapshot_durations():
        lines.append(
            f'encore_http_request_duration_seconds_max{{route="{route}",'
            f'method="{method}"}} {longest:.6f}'
        )
    return "\n".join(lines) + "\n"


def _route_label(route_obj: object | None) -> str:
    """Return the label for a matched route: its template, never a raw path."""
    if route_obj is None:
        return UNMATCHED_ROUTE
    return str(getattr(route_obj, "path", UNMATCHED_ROUTE))


# One process is one service (single-container deployment, ADR-0005) — the
# same reasoning as `MB_RATE_LIMITER`. Tests reset it between instances.
METRICS_REGISTRY = MetricsRegistry()


class RedMetricsMiddleware:
    """Raw-ASGI RED observation around the FastAPI app.

    Installed via ``app.add_middleware(RedMetricsMiddleware)``; it records
    into the process-global `METRICS_REGISTRY` unless one is injected.

    The route template is read from ``scope["route"]`` *after* the inner
    app runs — FastAPI writes the matched `APIRoute` there during routing
    (verified against the installed fastapi.routing), and only then does a
    template exist. Reading before would force labeling by raw path, which
    for this app means leaking feed capability tokens into metric labels;
    unmatched requests are recorded under the single ``unmatched`` label
    instead of their raw path for the same reason. A request that dies
    without sending a response is counted as a 500.
    """

    def __init__(self, app: ASGIApp, registry: MetricsRegistry = METRICS_REGISTRY) -> None:
        """Hold the inner ASGI app and the registry to record into."""
        self.app = app
        self.registry = registry

    async def __call__(
        self,
        scope: dict[str, object],
        receive: Callable[[], Awaitable[dict[str, object]]],
        send: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        """Observe one HTTP request into the registry, then pass it through."""
        if scope.get("type") != "http":  # pragma: no cover - lifespan passes through
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        start = time.monotonic()
        status_seen: list[int] = []

        async def send_wrapped(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.start" and not status_seen:
                status_seen.append(int(message["status"]))  # type: ignore[call-overload]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapped)
        except Exception:
            self.registry.observe(
                _route_label(scope.get("route")), method, 500, time.monotonic() - start
            )
            raise
        self.registry.observe(
            _route_label(scope.get("route")),
            method,
            status_seen[-1] if status_seen else 500,
            time.monotonic() - start,
        )
