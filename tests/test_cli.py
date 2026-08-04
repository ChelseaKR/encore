"""CLI tests: argument parsing, --data-dir wiring, and the F4 channel/feed surface."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from encore import cli
from encore.notify import DeliveryError, run_delivery_cycle
from encore.plex import PlexMusicClient
from encore.storage import DATA_DIR_ENV, Storage
from tests.notify_fixtures import (
    ARTIST_NAME,
    CHANNEL_URL,
    MACHINE_ID,
    RELEASE_TITLE,
    RecordingSender,
    seed_event,
)
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
    assert calls == [("encore.app:app", {"host": "127.0.0.1", "port": 9999, "access_log": False})]


@pytest.mark.no_secrets_in_logs
def test_serve_disables_the_uvicorn_access_log(monkeypatch: pytest.MonkeyPatch) -> None:
    # OBS-11: the F5 capability token rides in the URL path, so uvicorn's
    # access log would write it to stdout — `docker logs` in the shipped
    # image — on every single feed poll. Pinned here as the flag it is;
    # tests/test_app_feeds.py proves it against a real running server.
    calls = _capture_uvicorn(monkeypatch)

    assert cli.main(["serve"]) == 0

    assert calls[0][1]["access_log"] is False


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


def test_watch_runs_a_cycle_and_prints_counts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Empty database: a watch cycle is a free no-op (zero requests), and the
    # report prints counts only — never artist names or MBIDs.
    class FakeMBClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = FakeMBClient()
    monkeypatch.setattr(cli, "MusicBrainzClient", lambda: fake_client)

    assert cli.main(["watch", "--data-dir", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert "watch complete: polled=0 failed=0" in captured.out
    assert fake_client.closed


def test_watch_reports_an_unusable_data_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_directory = tmp_path / "regular-file"
    not_a_directory.write_text("occupied", encoding="utf-8")

    assert cli.main(["watch", "--data-dir", str(not_a_directory)]) == 1
    assert "cannot create data directory" in capsys.readouterr().err


def test_channels_add_list_and_remove_never_print_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: CHANNEL_URL)

    assert cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"]) == 0
    assert cli.main(["channels", "list", "--data-dir", str(tmp_path)]) == 0
    assert cli.main(["channels", "disable", "--data-dir", str(tmp_path), "--name", "phone"]) == 0
    assert cli.main(["channels", "enable", "--data-dir", str(tmp_path), "--name", "phone"]) == 0
    assert cli.main(["channels", "remove", "--data-dir", str(tmp_path), "--name", "phone"]) == 0

    captured = capsys.readouterr()
    assert CHANNEL_URL not in captured.out + captured.err
    assert "APPRISE-URL-needle" not in captured.out + captured.err
    assert "phone" in captured.out


def test_channels_add_rejects_an_empty_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: "")
    assert cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"]) == 2


def test_channels_add_reports_a_duplicate_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: CHANNEL_URL)
    cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"])
    assert cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_channels_list_and_events_are_helpful_when_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["channels", "list", "--data-dir", str(tmp_path)]) == 0
    assert cli.main(["events", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "No notification channels configured" in output
    assert "No release events yet" in output


def test_channels_test_surfaces_the_failure_and_the_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: CHANNEL_URL)
    cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"])

    monkeypatch.setattr(cli, "send_test_notification", lambda *_a, **_k: None)
    assert cli.main(["channels", "test", "--data-dir", str(tmp_path), "--name", "phone"]) == 0

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise DeliveryError("the notification service rejected or dropped the message")

    monkeypatch.setattr(cli, "send_test_notification", _boom)
    assert cli.main(["channels", "test", "--data-dir", str(tmp_path), "--name", "phone"]) == 1
    assert "rejected or dropped" in capsys.readouterr().err


def test_notify_runs_a_cycle_and_events_shows_the_in_app_feed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: CHANNEL_URL)
    cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"])

    storage = Storage(tmp_path)
    storage.set_plex_machine_identifier(MACHINE_ID)
    seed_event(storage)
    storage.close()

    sender = RecordingSender()
    monkeypatch.setattr(
        cli, "run_delivery_cycle", lambda storage_arg: run_delivery_cycle(storage_arg, sender)
    )
    assert cli.main(["notify", "--data-dir", str(tmp_path)]) == 0
    assert "delivery complete:" in capsys.readouterr().out
    assert len(sender.calls) == 1

    # The in-app feed is the always-works fallback: it shows the event whether
    # or not any channel worked, and it renders the same fields.
    assert cli.main(["events", "--data-dir", str(tmp_path), "--limit", "5"]) == 0
    feed = capsys.readouterr().out
    assert ARTIST_NAME in feed
    assert RELEASE_TITLE in feed
    assert MACHINE_ID in feed  # the Plex deep link


def test_notify_flags_unhealthy_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_read_channel_url", lambda: CHANNEL_URL)
    cli.main(["channels", "add", "--data-dir", str(tmp_path), "--name", "phone"])
    storage = Storage(tmp_path)
    seed_event(storage)
    storage.close()

    sender = RecordingSender(raise_unexpected=True)
    monkeypatch.setattr(
        cli, "run_delivery_cycle", lambda storage_arg: run_delivery_cycle(storage_arg, sender)
    )
    assert cli.main(["notify", "--data-dir", str(tmp_path)]) == 0
    assert "channels are unhealthy" in capsys.readouterr().out


def test_data_dir_errors_are_reported_not_raised(tmp_path: Path) -> None:
    # A file where the data directory should be: every F4 command must say so
    # rather than surface a traceback.
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    for argv in (
        ["notify", "--data-dir", str(blocked)],
        ["events", "--data-dir", str(blocked)],
        ["channels", "list", "--data-dir", str(blocked)],
        ["channels", "remove", "--data-dir", str(blocked), "--name", "x"],
        ["channels", "test", "--data-dir", str(blocked), "--name", "x"],
    ):
        assert cli.main(argv) == 1
