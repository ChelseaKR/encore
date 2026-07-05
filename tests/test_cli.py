"""M0 smoke test for the `encore serve` CLI entry point."""

from __future__ import annotations

from typing import Any

import pytest

from encore import cli


def test_serve_invokes_uvicorn_with_parsed_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def fake_run(app: str, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)

    exit_code = cli.main(["serve", "--host", "127.0.0.1", "--port", "9999"])

    assert exit_code == 0
    assert calls == [("encore.app:app", {"host": "127.0.0.1", "port": 9999})]


def test_missing_command_errors() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
