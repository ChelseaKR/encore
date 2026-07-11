"""CLI tests: `encore serve` argument parsing and the --data-dir wiring (F0)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from encore import cli
from encore.storage import DATA_DIR_ENV


def _capture_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake_run(app: str, **kwargs: Any) -> None:
        calls.append((app, kwargs))

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return calls


def test_serve_invokes_uvicorn_with_parsed_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    calls = _capture_uvicorn(monkeypatch)

    exit_code = cli.main(["serve", "--host", "127.0.0.1", "--port", "9999"])

    assert exit_code == 0
    assert calls == [("encore.app:app", {"host": "127.0.0.1", "port": 9999})]


def test_serve_data_dir_flag_reaches_the_app_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The flag's whole job (F0): uvicorn imports "encore.app:app" by string, so
    # --data-dir must travel via $ENCORE_DATA_DIR for the app factory to see it.
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    calls = _capture_uvicorn(monkeypatch)
    data_dir = tmp_path / "custom-data"

    exit_code = cli.main(["serve", "--data-dir", str(data_dir)])

    assert exit_code == 0
    assert os.environ[DATA_DIR_ENV] == str(data_dir)
    assert len(calls) == 1


def test_serve_without_flag_keeps_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No --data-dir: a pre-set $ENCORE_DATA_DIR must win over the ./data default.
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv(DATA_DIR_ENV, str(env_dir))
    _capture_uvicorn(monkeypatch)

    exit_code = cli.main(["serve"])

    assert exit_code == 0
    assert os.environ[DATA_DIR_ENV] == str(env_dir)


def test_missing_command_errors() -> None:
    with pytest.raises(SystemExit):
        cli.main([])
