"""Background Plex-sync scheduler behavior and privacy regressions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from encore import scheduler
from encore.plex import PlexMusicClient
from encore.secretstore import SecretDecryptionError
from encore.storage import Storage
from encore.sync import SyncError
from tests.plex_fixtures import FakeArtist, FakeLibrary, make_client_session


class FakeScheduler:
    """Record APScheduler configuration without starting a worker thread."""

    def __init__(self) -> None:
        self.jobs: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.started = False

    def add_job(self, *args: Any, **kwargs: Any) -> None:
        """Record one scheduled job."""
        self.jobs.append((args, kwargs))

    def start(self) -> None:
        """Record scheduler startup."""
        self.started = True


def test_scheduler_stays_off_when_disabled_or_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setenv(scheduler.SYNC_INTERVAL_ENV, "0")
    assert scheduler.build_sync_scheduler(storage) is None

    monkeypatch.setenv(scheduler.SYNC_INTERVAL_ENV, "not-a-number")
    with caplog.at_level(logging.WARNING):
        assert scheduler.build_sync_scheduler(storage) is None
    assert "falling back" in caplog.text
    storage.close()


def test_scheduler_fails_closed_when_credentials_cannot_be_decrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)

    def broken_credentials() -> tuple[str, str] | None:
        raise SecretDecryptionError("fixture key mismatch")

    monkeypatch.setattr(storage, "get_plex_credentials", broken_credentials)
    with caplog.at_level(logging.ERROR):
        assert scheduler.build_sync_scheduler(storage) is None
    assert "fixture key mismatch" in caplog.text
    storage.close()


def test_configured_scheduler_uses_one_coalescing_interval_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)
    storage.set_plex_credentials("http://plex.local:32400", "fixture-token")
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda: fake)
    monkeypatch.setenv(scheduler.SYNC_INTERVAL_ENV, "6.5")

    result = scheduler.build_sync_scheduler(storage)

    assert result is fake
    assert fake.started
    assert len(fake.jobs) == 1
    args, kwargs = fake.jobs[0]
    assert args[:2] == (scheduler._run_scheduled_sync, "interval")
    assert kwargs == {
        "args": [storage],
        "hours": 6.5,
        "id": scheduler.SYNC_JOB_ID,
        "coalesce": True,
        "max_instances": 1,
    }
    storage.close()


@pytest.mark.no_outing
@pytest.mark.no_secrets_in_logs
def test_scheduled_sync_logs_counts_without_artist_or_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = Storage(tmp_path)
    storage.set_plex_credentials("http://plex.local:32400", "token-needle-scheduled")
    session, base_url = make_client_session(
        [
            FakeLibrary(
                key="1",
                title="Music",
                artists=[FakeArtist("101", "sentinel-artist-needle")],
            )
        ]
    )

    def fixture_client(url: str, token: str) -> PlexMusicClient:
        assert url == "http://plex.local:32400"
        return PlexMusicClient(base_url, token, session=session)

    monkeypatch.setattr(scheduler, "PlexMusicClient", fixture_client)
    with caplog.at_level(logging.INFO):
        scheduler._run_scheduled_sync(storage)

    assert "seen=1 added=1" in caplog.text
    assert "sentinel-artist-needle" not in caplog.text
    assert "token-needle-scheduled" not in caplog.text
    storage.close()


def test_scheduled_sync_handles_removed_credentials_and_sync_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    with caplog.at_level(logging.WARNING):
        scheduler._run_scheduled_sync(storage)
    assert "credentials were removed" in caplog.text

    storage.set_plex_credentials("http://plex.local:32400", "fixture-token")
    monkeypatch.setattr(scheduler, "PlexMusicClient", lambda *_args: object())

    def fail_sync(*_args: Any, **_kwargs: Any) -> None:
        raise SyncError("fixture sync failure")

    monkeypatch.setattr(scheduler, "sync_artists", fail_sync)
    with caplog.at_level(logging.ERROR):
        scheduler._run_scheduled_sync(storage)
    assert "fixture sync failure" in caplog.text
    storage.close()
