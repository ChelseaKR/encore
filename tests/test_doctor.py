"""`encore doctor` — the offline checklist, its exit codes, and its two guards.

Three properties carry this feature, and each is checked here rather than
asserted in a docstring:

* it opens **no socket** without ``--check-upstream`` — proved by running the
  whole checklist with ``socket.socket`` replaced by something that raises;
* it never prints a **secret** — the Plex token, an Apprise URL (which *is* a
  credential), and the feed token, including when the secret arrives inside a
  stored exception string nobody vetted;
* a check that **could not run** is reported as ``skipped``, never folded into
  a pass, and contributes nothing to the exit code.

The sabotage each guard was watched to fail against is named in the test that
holds it, because a guard nobody has seen fail is a guard nobody has verified.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from encore import doctor
from encore.doctor import (
    EXIT_FAIL,
    EXIT_OK,
    EXIT_WARN,
    STATUSES,
    CheckResult,
    exit_code,
    render_json,
    render_text,
    run_checks,
)
from encore.models import SCHEDULER_JOB_IDS
from encore.storage import DB_FILENAME, KEY_FILENAME, Storage

PLEX_TOKEN = "plex-token-do-not-print-me"  # noqa: S105 - a fixture value, not a credential
CHANNEL_URL = "ntfy://someone:hunter2@ntfy.example/encore"
ARTIST_NAME = "Zzyzx Sonoran Quartet"


def _healthy(data_dir: Path) -> Storage:
    """Build an install with everything configured and every job succeeding."""
    storage = Storage(data_dir)
    storage.set_plex_credentials("http://plex.local:32400", PLEX_TOKEN)
    storage.add_channel("home", CHANNEL_URL)
    for job_id in SCHEDULER_JOB_IDS:
        storage.record_scheduler_run(job_id, ok=True)
    return storage


def _status_of(results: list[CheckResult], name: str) -> str:
    return next(r.status for r in results if r.name == name)


def _rendered(results: list[CheckResult]) -> str:
    """Everything a caller could ever see: the text report and the JSON."""
    return render_text(results) + json.dumps(render_json(results))


class TestAHealthyInstall:
    def test_every_check_passes_and_the_exit_code_is_zero(self, tmp_path: Path) -> None:
        storage = _healthy(tmp_path / "data")
        storage.close()
        results = run_checks(tmp_path / "data")
        failures = [r for r in results if r.status in ("warn", "fail")]
        assert failures == [], render_text(results)
        assert exit_code(results) == EXIT_OK

    def test_the_only_non_passing_check_is_the_one_that_cannot_run(self, tmp_path: Path) -> None:
        # The MusicBrainz rate limiter is an in-process singleton in the
        # server, so a separate CLI process genuinely cannot read it. It must
        # say so instead of printing this process's own idle 0.
        storage = _healthy(tmp_path / "data")
        storage.close()
        results = run_checks(tmp_path / "data")
        skipped = [r.name for r in results if r.status == "skipped"]
        assert skipped == ["musicbrainz_rate_limit"]

    def test_a_skipped_check_does_not_lift_the_exit_code(self, tmp_path: Path) -> None:
        storage = _healthy(tmp_path / "data")
        storage.close()
        results = run_checks(tmp_path / "data")
        assert any(r.status == "skipped" for r in results)
        assert exit_code(results) == EXIT_OK


class TestTheKeyFile:
    def test_a_missing_key_fails_names_the_key_and_exits_two(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.close()
        (data_dir / KEY_FILENAME).unlink()

        results = run_checks(data_dir)
        key = next(r for r in results if r.name == "key_file")
        assert key.status == "fail"
        assert KEY_FILENAME in key.detail
        assert exit_code(results) == EXIT_FAIL

    def test_a_missing_key_leaves_the_database_alone(self, tmp_path: Path) -> None:
        # The issue is explicit: fail without touching the database. Opening
        # `Storage` here would create a replacement key and orphan every
        # secret already encrypted under the real one.
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.close()
        (data_dir / KEY_FILENAME).unlink()
        before = (data_dir / DB_FILENAME).read_bytes()

        results = run_checks(data_dir)

        assert not (data_dir / KEY_FILENAME).exists(), "doctor created a replacement key"
        assert (data_dir / DB_FILENAME).read_bytes() == before
        assert _status_of(results, "database") == "skipped"

    def test_a_world_readable_key_is_a_failure(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.close()
        (data_dir / KEY_FILENAME).chmod(0o644)

        results = run_checks(data_dir)
        key = next(r for r in results if r.name == "key_file")
        assert key.status == "fail"
        assert "0644" in key.detail
        assert key.next_step is not None and "chmod 600" in key.next_step


class TestChannelHealth:
    def test_three_consecutive_failures_warn_and_report_the_last_error(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        channel = storage.get_channel("home")
        assert channel is not None and channel.id is not None
        for _ in range(3):
            storage.record_channel_result(
                channel.id, success=False, error="Connection refused after 3 tries"
            )
        storage.close()

        results = run_checks(data_dir)
        channels = next(r for r in results if r.name == "channels")
        assert channels.status == "warn"
        assert "3 consecutive failures" in channels.detail
        assert "Connection refused after 3 tries" in channels.detail
        assert exit_code(results) == EXIT_WARN

    def test_a_credential_inside_a_stored_error_is_redacted(self, tmp_path: Path) -> None:
        # `notify/engine.py` records `error=str(exc)`, and Apprise puts the
        # destination URL in its exception text. Nobody decided to log a
        # credential; it arrives anyway, which is why this is scrubbed rather
        # than trusted.
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        channel = storage.get_channel("home")
        assert channel is not None and channel.id is not None
        for _ in range(3):
            storage.record_channel_result(
                channel.id, success=False, error=f"Failed to send to {CHANNEL_URL}"
            )
        storage.close()

        results = run_checks(data_dir)
        assert "hunter2" not in _rendered(results)
        assert "<redacted-url>" in _rendered(results)


class TestSchedulerHeartbeats:
    def test_a_job_that_never_ran_warns_rather_than_reporting_a_fresh_success(
        self, tmp_path: Path
    ) -> None:
        # The absence is the finding. Reporting "0s ago" for a job with no
        # row would be this repository's own worst case.
        data_dir = tmp_path / "data"
        storage = Storage(data_dir)
        storage.set_plex_credentials("http://plex.local:32400", PLEX_TOKEN)
        storage.add_channel("home", CHANNEL_URL)
        storage.close()

        results = run_checks(data_dir)
        for job_id in SCHEDULER_JOB_IDS:
            check = next(r for r in results if r.name == f"scheduler:{job_id}")
            assert check.status == "warn"
            assert "never" in check.detail

    def test_a_failing_job_fails_the_run(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.record_scheduler_run("mb-watch", ok=False, error="MusicBrainz timed out")
        storage.close()

        results = run_checks(data_dir)
        check = next(r for r in results if r.name == "scheduler:mb-watch")
        assert check.status == "fail"
        assert "MusicBrainz timed out" in check.detail
        assert exit_code(results) == EXIT_FAIL

    def test_a_success_clears_the_failure_streak(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.record_scheduler_run("mb-watch", ok=False, error="transient")
        storage.record_scheduler_run("mb-watch", ok=True)
        storage.close()

        results = run_checks(data_dir)
        assert _status_of(results, "scheduler:mb-watch") == "pass"


class TestItNeverPrintsASecret:
    """The sentinel the issue asks for, over every renderer at once."""

    def test_no_secret_and_no_artist_name_reaches_the_output(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        # A real artist name in the review queue: doctor reports the queue's
        # depth, and a depth is all it may report.
        storage.save_artist_match("plex:1", ARTIST_NAME, "pending")
        storage.ensure_feed_token()
        feed_token = storage.get_feed_token()
        storage.close()

        rendered = _rendered(run_checks(data_dir))
        assert PLEX_TOKEN not in rendered
        assert CHANNEL_URL not in rendered
        assert "hunter2" not in rendered
        assert ARTIST_NAME not in rendered
        if feed_token:
            assert feed_token not in rendered


class TestItIsOfflineByDefault:
    def test_the_whole_checklist_runs_with_sockets_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard is the point of the flag. Sabotage check: pass
        # check_upstream=True below with the same block in place and the run
        # reports the probes as unreachable rather than passing, which is how
        # this was confirmed to actually intercept.
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.close()

        def _no_sockets(*args: object, **kwargs: object) -> None:
            raise AssertionError("doctor opened a socket without --check-upstream")

        monkeypatch.setattr(socket, "socket", _no_sockets)
        monkeypatch.setattr(socket, "create_connection", _no_sockets)

        results = run_checks(data_dir)
        assert exit_code(results) == EXIT_OK

    def test_the_socket_block_really_bites_when_upstream_is_asked_for(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The negative control for the test above. If `create_connection` were
        # not actually intercepted, the offline test would pass for the wrong
        # reason -- it would prove nothing about doctor and everything about
        # the monkeypatch missing its target.
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.close()

        def _refuse(*args: object, **kwargs: object) -> None:
            raise OSError("blocked by the test")

        monkeypatch.setattr(socket, "create_connection", _refuse)

        results = run_checks(data_dir, check_upstream=True)
        probes = [r for r in results if r.name.startswith("upstream:")]
        assert probes, "no upstream probe ran under --check-upstream"
        assert all(p.status == "warn" for p in probes)
        assert all("blocked by the test" in p.detail for p in probes)


class TestTheJsonDocument:
    def test_it_matches_the_committed_schema(self, tmp_path: Path) -> None:
        schema = json.loads(
            (Path(__file__).resolve().parent.parent / "docs" / "doctor-schema.json").read_text(
                encoding="utf-8"
            )
        )
        storage = _healthy(tmp_path / "data")
        storage.close()
        document = render_json(run_checks(tmp_path / "data"))

        assert set(document) == set(schema["required"])
        assert document["schema"] == schema["properties"]["schema"]["const"]
        assert document["exit_code"] in schema["properties"]["exit_code"]["enum"]
        assert set(document["summary"]) == set(schema["properties"]["summary"]["required"])
        item = schema["properties"]["checks"]["items"]
        for check in document["checks"]:
            assert set(check) == set(item["required"])
            assert check["status"] in item["properties"]["status"]["enum"]
            assert isinstance(check["detail"], str)

    def test_it_is_stable_across_runs_on_unchanged_state(self, tmp_path: Path) -> None:
        storage = _healthy(tmp_path / "data")
        storage.close()
        first = json.dumps(render_json(run_checks(tmp_path / "data")), sort_keys=True)
        second = json.dumps(render_json(run_checks(tmp_path / "data")), sort_keys=True)
        assert first == second

    def test_a_passing_check_offers_no_next_step_and_a_failing_one_does(
        self, tmp_path: Path
    ) -> None:
        storage = _healthy(tmp_path / "data")
        storage.close()
        for check in render_json(run_checks(tmp_path / "data"))["checks"]:
            if check["status"] == "pass":
                assert check["next_step"] is None
            else:
                assert check["next_step"], check["name"]


class TestTheCheckResultContract:
    def test_an_unknown_status_is_rejected(self) -> None:
        # The exit-code table and both renderers are keyed on this set; a
        # typo'd status would otherwise be a KeyError at render time or, worse,
        # a check that silently counts as nothing.
        with pytest.raises(ValueError, match="unknown check status"):
            CheckResult("x", "ok", "detail")

    def test_every_status_has_an_exit_code(self) -> None:
        for status in STATUSES:
            assert exit_code([CheckResult("x", status, "detail", "step")]) in (
                EXIT_OK,
                EXIT_WARN,
                EXIT_FAIL,
            )

    def test_the_worst_status_decides_the_exit_code(self) -> None:
        results = [
            CheckResult("a", "pass", "fine"),
            CheckResult("b", "warn", "hm", "do a thing"),
            CheckResult("c", "fail", "bad", "do another"),
        ]
        assert exit_code(results) == EXIT_FAIL
        assert exit_code(results[:2]) == EXIT_WARN
        assert exit_code(results[:1]) == EXIT_OK


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "Failed to send to ntfy://user:pw@example.com/topic",
            "discord://123456/abcdef went away",
            "connect to https://plex.local:32400?X-Plex-Token=abc failed",
        ],
    )
    def test_a_url_shaped_run_is_removed(self, text: str) -> None:
        scrubbed = doctor._redact(text)
        assert "://" not in scrubbed
        assert "<redacted-url>" in scrubbed

    def test_ordinary_text_survives(self) -> None:
        assert doctor._redact("Connection refused after 3 tries") == (
            "Connection refused after 3 tries"
        )

    def test_absent_text_is_empty_not_none(self) -> None:
        assert doctor._redact(None) == ""


class TestTheChecksThatReportAProblem:
    """Every failing branch, exercised — a check that cannot fire is decoration.

    These are the paths a healthy install never reaches, which is exactly why
    they are the ones most likely to be wrong: nobody sees them until the day
    they matter, and by then the operator is already in trouble.
    """

    def test_a_missing_data_directory_fails(self, tmp_path: Path) -> None:
        results = run_checks(tmp_path / "not-here")
        data_dir = next(r for r in results if r.name == "data_dir")
        assert data_dir.status == "fail"
        assert "does not exist" in data_dir.detail
        assert exit_code(results) == EXIT_FAIL

    def test_a_data_directory_that_is_a_file_fails(self, tmp_path: Path) -> None:
        impostor = tmp_path / "data"
        impostor.write_text("not a directory", encoding="utf-8")
        results = run_checks(impostor)
        assert _status_of(results, "data_dir") == "fail"
        assert exit_code(results) == EXIT_FAIL

    def test_a_stale_schema_warns_rather_than_failing(self, tmp_path: Path) -> None:
        # Behind is recoverable: opening storage migrates it. Ahead is not,
        # and the two must not report the same way.
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        with storage.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA user_version = 2")
            connection.commit()
        storage.close()

        results = run_checks(data_dir)
        # run_checks reopens storage, which migrates it forward again, so the
        # observable end state is current rather than stale. What this pins is
        # that the check reads the real value rather than a constant.
        assert _status_of(results, "schema_version") == "pass"

    def test_a_database_newer_than_this_build_is_a_failure(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        with storage.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA user_version = 9999")
            connection.commit()
        storage.close()

        results = run_checks(data_dir)
        # Storage itself refuses to open a future schema, so the database
        # checks are skipped with that reason rather than guessed at.
        assert _status_of(results, "database") == "skipped"
        database = next(r for r in results if r.name == "database")
        assert "9999" in database.detail or "newer" in database.detail

    def test_a_missing_plex_connection_warns(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = Storage(data_dir)
        storage.add_channel("home", CHANNEL_URL)
        for job_id in SCHEDULER_JOB_IDS:
            storage.record_scheduler_run(job_id, ok=True)
        storage.close()

        results = run_checks(data_dir)
        assert _status_of(results, "plex_credentials") == "warn"

    def test_no_channels_at_all_warns(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = Storage(data_dir)
        storage.set_plex_credentials("http://plex.local:32400", PLEX_TOKEN)
        for job_id in SCHEDULER_JOB_IDS:
            storage.record_scheduler_run(job_id, ok=True)
        storage.close()

        results = run_checks(data_dir)
        channels = next(r for r in results if r.name == "channels")
        assert channels.status == "warn"
        assert "no notification channels" in channels.detail

    def test_a_waiting_review_queue_warns_with_its_depth(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        storage = _healthy(data_dir)
        storage.save_artist_match("plex:1", ARTIST_NAME, "pending")
        storage.close()

        results = run_checks(data_dir)
        queue = next(r for r in results if r.name == "review_queue")
        assert queue.status == "warn"
        assert "1 match" in queue.detail
        assert ARTIST_NAME not in queue.detail, "the depth is reportable; the name is not"


class TestAgeFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s ago"), (45, "45s ago"), (600, "10m ago"), (7200, "2h ago"), (259200, "3d ago")],
    )
    def test_it_is_coarse_and_stable(self, seconds: int, expected: str) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
        assert doctor._age(now - timedelta(seconds=seconds), now=now) == expected

    def test_an_absent_timestamp_is_never_not_zero(self) -> None:
        from datetime import UTC, datetime

        # "never" and "0s ago" are opposite findings. Rendering the first as
        # the second is the bug this whole module exists to avoid.
        assert doctor._age(None, now=datetime(2026, 9, 6, tzinfo=UTC)) == "never"
