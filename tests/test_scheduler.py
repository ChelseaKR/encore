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
    assert "the stored Plex connection was removed" in caplog.text

    storage.set_plex_credentials("http://plex.local:32400", "fixture-token")
    monkeypatch.setattr(scheduler, "PlexMusicClient", lambda *_args: object())

    def fail_sync(*_args: Any, **_kwargs: Any) -> None:
        raise SyncError("fixture sync failure")

    monkeypatch.setattr(scheduler, "sync_artists", fail_sync)
    with caplog.at_level(logging.ERROR):
        scheduler._run_scheduled_sync(storage)
    assert "fixture sync failure" in caplog.text
    storage.close()


def test_watch_scheduler_stays_off_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setenv(scheduler.WATCH_INTERVAL_ENV, "0")
    with caplog.at_level(logging.INFO):
        assert scheduler.build_watch_scheduler(storage) is None
    assert "watch scheduler disabled" in caplog.text
    storage.close()


def test_watch_scheduler_uses_one_coalescing_interval_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No credential gate (MusicBrainz is keyless): the watch scheduler starts
    # on a fresh storage and picks up newly matched artists next cycle.
    storage = Storage(tmp_path)
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda: fake)
    monkeypatch.setenv(scheduler.WATCH_INTERVAL_ENV, "12")

    result = scheduler.build_watch_scheduler(storage)

    assert result is fake
    assert fake.started
    assert len(fake.jobs) == 1
    args, kwargs = fake.jobs[0]
    assert args[:2] == (scheduler._run_scheduled_watch, "interval")
    assert kwargs == {
        "args": [storage],
        "hours": 12.0,
        "id": scheduler.WATCH_JOB_ID,
        "coalesce": True,  # skip-don't-queue after downtime (risk R8)
        "max_instances": 1,
    }
    storage.close()


def test_scheduled_watch_builds_and_closes_its_own_mb_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)

    class FakeMBClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = FakeMBClient()
    cycles: list[object] = []
    monkeypatch.setattr(scheduler, "MusicBrainzClient", lambda: fake_client)
    monkeypatch.setattr(
        scheduler, "watch_all_artists", lambda _storage, client: cycles.append(client)
    )

    scheduler._run_scheduled_watch(storage)

    assert cycles == [fake_client]
    assert fake_client.closed  # closed even though the cycle ran fine
    storage.close()


def test_match_scheduler_stays_off_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setenv(scheduler.MATCH_INTERVAL_ENV, "0")
    with caplog.at_level(logging.INFO):
        assert scheduler.build_match_scheduler(storage) is None
    assert "match scheduler disabled" in caplog.text
    storage.close()


def test_match_scheduler_starts_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No credential gate (MusicBrainz is keyless) and no backlog on a fresh
    # install: the pass over zero unmatched artists costs no requests.
    storage = Storage(tmp_path)
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda: fake)
    monkeypatch.delenv(scheduler.MATCH_INTERVAL_ENV, raising=False)

    result = scheduler.build_match_scheduler(storage)

    assert result is fake
    assert fake.started
    assert len(fake.jobs) == 1
    args, kwargs = fake.jobs[0]
    assert args[:2] == (scheduler._run_scheduled_match, "interval")
    assert kwargs == {
        "args": [storage],
        "hours": 24.0,
        "id": scheduler.MATCH_JOB_ID,
        "coalesce": True,  # skip-don't-queue after downtime (risk R8)
        "max_instances": 1,
    }
    storage.close()


def test_scheduled_match_builds_and_closes_its_own_mb_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)

    class FakeMBClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = FakeMBClient()
    passes: list[object] = []
    monkeypatch.setattr(scheduler, "MusicBrainzClient", lambda: fake_client)
    monkeypatch.setattr(
        scheduler, "run_matching_pass", lambda _storage, client: passes.append(client)
    )

    scheduler._run_scheduled_match(storage)

    assert passes == [fake_client]
    assert fake_client.closed  # closed even though the pass ran fine
    storage.close()


def test_notify_scheduler_stays_off_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setenv(scheduler.NOTIFY_INTERVAL_ENV, "0")
    with caplog.at_level(logging.INFO):
        assert scheduler.build_notify_scheduler(storage) is None
    assert "notify scheduler disabled" in caplog.text
    storage.close()


def test_notify_scheduler_runs_in_minutes_not_hours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The delivery queue is local, so the cadence is minutes: once an event
    # exists, the user is waiting for it (F4, docs/adr/0012).
    storage = Storage(tmp_path)
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda: fake)
    monkeypatch.setenv(scheduler.NOTIFY_INTERVAL_ENV, "5")

    result = scheduler.build_notify_scheduler(storage)

    assert result is fake
    args, kwargs = fake.jobs[0]
    assert args[:2] == (scheduler._run_scheduled_delivery, "interval")
    assert kwargs == {
        "args": [storage],
        "minutes": 5.0,
        "id": scheduler.NOTIFY_JOB_ID,
        "coalesce": True,
        "max_instances": 1,
    }
    storage.close()


def test_scheduled_delivery_runs_one_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Storage(tmp_path)
    cycles: list[object] = []
    monkeypatch.setattr(scheduler, "run_delivery_cycle", lambda s: cycles.append(s))

    scheduler._run_scheduled_delivery(storage)

    assert cycles == [storage]
    storage.close()


def test_an_unparseable_interval_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(scheduler.NOTIFY_INTERVAL_ENV, "every-so-often")
    with caplog.at_level(logging.WARNING):
        value = scheduler._configured_interval(
            scheduler.NOTIFY_INTERVAL_ENV, scheduler.DEFAULT_NOTIFY_INTERVAL_MINUTES
        )
    assert value == scheduler.DEFAULT_NOTIFY_INTERVAL_MINUTES
    assert "falling back" in caplog.text


def test_rec_scheduler_stays_off_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = Storage(tmp_path)
    monkeypatch.setenv(scheduler.REC_INTERVAL_ENV, "0")
    with caplog.at_level(logging.INFO):
        assert scheduler.build_rec_scheduler(storage) is None
    assert "recommend scheduler disabled" in caplog.text
    storage.close()


def test_rec_scheduler_defaults_to_a_weekly_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", lambda: fake)
    monkeypatch.delenv(scheduler.REC_INTERVAL_ENV, raising=False)

    result = scheduler.build_rec_scheduler(storage)

    assert result is fake
    args, kwargs = fake.jobs[0]
    assert args[:2] == (scheduler._run_scheduled_recommend, "interval")
    assert kwargs == {
        "args": [storage],
        "hours": 168.0,
        "id": scheduler.REC_JOB_ID,
        "coalesce": True,
        "max_instances": 1,
    }
    storage.close()


def test_scheduled_recommend_builds_and_closes_its_own_lb_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = Storage(tmp_path)

    class FakeLBClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    fake_client = FakeLBClient()
    refreshes: list[object] = []
    monkeypatch.setattr(scheduler, "ListenBrainzClient", lambda: fake_client)
    monkeypatch.setattr(scheduler, "refresh_recommendations", lambda _s, c: refreshes.append(c))

    scheduler._run_scheduled_recommend(storage)

    assert refreshes == [fake_client]
    assert fake_client.closed
    storage.close()
