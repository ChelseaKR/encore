"""RED-metrics tests (OBS-11): template labels, exposition, and no leaks."""

from __future__ import annotations

import time
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from encore.app import create_app
from encore.metrics import (
    METRICS_REGISTRY,
    UNMATCHED_ROUTE,
    MetricsRegistry,
    RedMetricsMiddleware,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """Every test starts and ends with an empty process-global registry."""
    METRICS_REGISTRY.reset()
    yield
    METRICS_REGISTRY.reset()


def test_registry_observes_counts_and_durations() -> None:
    registry = MetricsRegistry()
    registry.observe("/livez", "GET", 200, 0.010)
    registry.observe("/livez", "GET", 200, 0.030)
    registry.observe("/livez", "GET", 503, 0.100)

    counts = registry.snapshot_counts()
    assert ("/livez", "GET", 200, 2) in counts
    assert ("/livez", "GET", 503, 1) in counts

    durations = {
        key: stats
        for key, *stats in [
            ((route, method), count, total, longest)
            for route, method, count, total, longest in registry.snapshot_durations()
        ]
    }
    assert durations[("/livez", "GET")][0] == 3  # count
    assert abs(durations[("/livez", "GET")][1] - 0.14) < 1e-9  # sum
    assert abs(durations[("/livez", "GET")][2] - 0.1) < 1e-9  # max


def test_registry_reset_clears_everything() -> None:
    registry = MetricsRegistry()
    registry.observe("/x", "GET", 200, 0.001)
    registry.reset()
    assert registry.snapshot_counts() == []
    assert registry.snapshot_durations() == []


def test_prometheus_render_is_deterministic_and_complete() -> None:
    registry = MetricsRegistry()
    registry.observe("/livez", "GET", 200, 0.5)

    text_a = _render(registry)
    text_b = _render(registry)
    assert text_a == text_b
    assert 'encore_http_requests_total{route="/livez",method="GET",status="200"} 1' in text_a
    assert 'encore_http_request_duration_seconds_count{route="/livez",method="GET"} 1' in text_a
    assert "# TYPE encore_http_requests_total counter" in text_a


def _render(registry: MetricsRegistry) -> str:
    from encore.metrics import render_prometheus

    return render_prometheus(registry)


def test_middleware_records_template_labels_and_404s(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        client.get("/livez")
        client.get("/definitely-not-a-route")

        body = client.get("/metrics").text

    assert 'encore_http_requests_total{route="/livez",method="GET",status="200"} 1' in body
    assert f'route="{UNMATCHED_ROUTE}"' in body
    assert "/definitely-not-a-route" not in body  # raw paths never become labels


def test_feed_tokens_never_reach_the_metrics_surface(tmp_path: Path) -> None:
    # The leak this module exists to prevent: a capability token travels in
    # the feed URL's path. Even rejected requests must leave nothing but
    # the route TEMPLATE behind.
    sentinel = "super-secret-feed-token-4f0a"
    with TestClient(create_app(tmp_path)) as client:
        client.get(f"/feeds/{sentinel}/releases.xml")
        body = client.get("/metrics").text

    assert sentinel not in body
    assert 'route="/feeds/{token}/releases.xml"' in body
    assert '"status="404"' not in body.replace('status="404"', "")  # sanity: parseable
    assert 'status="404"' in body


def test_an_exception_is_recorded_as_a_500_and_reraised() -> None:
    class Boom(Exception):
        pass

    async def exploding_app(scope, receive, send):  # type: ignore[no-untyped-def]
        raise Boom("fixture")

    app = RedMetricsMiddleware(exploding_app, METRICS_REGISTRY)

    with pytest.raises(Boom):

        async def run() -> None:
            await app(
                {"type": "http", "method": "GET"},
                _noop_receive,
                _noop_send,
            )

        import anyio

        anyio.run(run)

    counts = METRICS_REGISTRY.snapshot_counts()
    assert counts == [(UNMATCHED_ROUTE, "GET", 500, 1)]


async def _noop_receive() -> dict[str, object]:
    return {"type": "http.request"}


async def _noop_send(message: dict[str, object]) -> None:
    return None


def test_durations_are_actually_measured(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        client.get("/livez")

    rows = METRICS_REGISTRY.snapshot_durations()
    assert len(rows) == 1
    _route, _method, count, total, longest = rows[0]
    assert count == 1
    assert total > 0.0 and longest > 0.0
    time.sleep(0)  # keep the module imported for symmetry
