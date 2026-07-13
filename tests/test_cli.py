"""CLI tests: `encore serve` argument parsing and the --data-dir wiring (F0)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from encore import cli
from encore.plex import PlexMusicClient
from encore.storage import DATA_DIR_ENV, Storage
from tests.plex_fixtures import FakeArtist, FakeLibrary, make_client_session


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


def test_plex_configure_stores_encrypted_credentials_and_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_token", lambda: "token-needle-configure")

    exit_code = cli.main(
        [
            "plex",
            "configure",
            "--data-dir",
            str(tmp_path),
            "--base-url",
            "http://plex.local:32400",
            "--library",
            "1",
            "--library",
            "3",
        ]
    )

    assert exit_code == 0
    storage = Storage(tmp_path)
    assert storage.get_plex_credentials() == (
        "http://plex.local:32400",
        "token-needle-configure",
    )
    assert storage.get_plex_libraries() == ["1", "3"]
    storage.close()
    captured = capsys.readouterr()
    assert "token-needle-configure" not in captured.out + captured.err


def test_plex_configure_rejects_an_empty_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_token", lambda: "")

    assert (
        cli.main(
            [
                "plex",
                "configure",
                "--data-dir",
                str(tmp_path),
                "--base-url",
                "http://plex.local:32400",
            ]
        )
        == 2
    )
    assert "empty Plex token" in capsys.readouterr().err


def test_sync_requires_configured_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["sync", "--data-dir", str(tmp_path)]) == 2
    assert "no Plex credentials configured" in capsys.readouterr().err


def test_sync_runs_the_real_inventory_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    storage.set_plex_credentials("http://plex.local:32400", "token-needle-sync")
    storage.close()
    session, base_url = make_client_session(
        [FakeLibrary(key="1", title="Music", artists=[FakeArtist("101", "Stereolab")])]
    )

    def fixture_client(url: str, token: str) -> PlexMusicClient:
        assert url == "http://plex.local:32400"
        return PlexMusicClient(base_url, token, session=session)

    monkeypatch.setattr(cli, "PlexMusicClient", fixture_client)

    assert cli.main(["sync", "--data-dir", str(tmp_path), "--library", "1"]) == 0
    captured = capsys.readouterr()
    assert "seen=1 added=1" in captured.out
    assert "token-needle-sync" not in captured.out + captured.err


def test_commands_report_an_unusable_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_directory = tmp_path / "regular-file"
    not_a_directory.write_text("occupied", encoding="utf-8")
    monkeypatch.setattr(cli, "_read_token", lambda: "token")

    configure_exit = cli.main(
        [
            "plex",
            "configure",
            "--data-dir",
            str(not_a_directory),
            "--base-url",
            "http://plex.local:32400",
        ]
    )
    sync_exit = cli.main(["sync", "--data-dir", str(not_a_directory)])

    assert (configure_exit, sync_exit) == (1, 1)
    assert capsys.readouterr().err.count("cannot create data directory") == 2
