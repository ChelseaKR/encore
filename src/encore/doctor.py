"""`encore doctor` — one offline answer to "what is wrong with this install".

A self-hoster whose alerts went quiet has, today, `encore channels list`,
`/readyz`, and logs that deliberately carry no artist names. Those are three
partial views of three different things and none of them answers the question.
This module is the single checklist: it reads the data directory, the key
file's permissions, the database's own integrity, and the state each feature
already persists, and prints one line per check with a verdict and a next step.

Three properties are load-bearing, and each is held by a test rather than by
this docstring:

**Offline by default.** Nothing here opens a socket unless `--check-upstream`
is passed. That is what makes it usable as a container healthcheck and safe on
a host whose network is the thing that broke. `tests/test_doctor.py` proves it
by running the whole checklist with the socket module blocked.

**It never prints a secret.** The Plex token, an Apprise URL, and the feed
token are checked for *presence and decryptability*; their values are never
rendered. That is not just a matter of not printing them on purpose: a
channel's stored `last_error` is `str(exc)` from Apprise, and an Apprise URL
*is* a credential (`ntfy://user:pass@…`), so an error text can carry one
without anyone deciding to log it. `_redact` scrubs that before it is shown.

**A check that could not run says so.** It is never folded into a pass. The
MusicBrainz rate-limit counter lives on an in-process singleton in the running
server, so a separate CLI process cannot see it — it is reported as `skipped`
with that reason rather than as a comfortable `0`, and `skipped` contributes
nothing to the exit code. This repository has already published "no data" as a
real measurement once; not here.
"""

from __future__ import annotations

import os
import re
import socket
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from encore.models import SCHEDULER_JOB_IDS
from encore.secretstore import SecretDecryptionError, SecretKeyError
from encore.storage import (
    DB_FILENAME,
    KEY_FILENAME,
    MIGRATIONS,
    Storage,
    StorageError,
    resolve_data_dir,
)

__all__ = [
    "CHECK_UPSTREAM_HOSTS",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_WARN",
    "STATUSES",
    "CheckResult",
    "exit_code",
    "render_json",
    "render_text",
    "run_checks",
]

# `skipped` is deliberately a status rather than an omission: a check that
# could not run has to be visible, and it must not be counted as a pass.
STATUSES = ("pass", "warn", "fail", "skipped")

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2

_EXIT_FOR = {"pass": EXIT_OK, "warn": EXIT_WARN, "fail": EXIT_FAIL, "skipped": EXIT_OK}

# The channel is unhealthy at this many consecutive failures — the same
# threshold `encore channels list` already treats as "this one is broken".
CHANNEL_FAILURE_WARN = 3

# Hosts probed only under `--check-upstream`, one TCP connect each. Names, not
# URLs: this opens a socket, it does not make a request, so nothing here can
# carry a query or a token.
CHECK_UPSTREAM_HOSTS = (
    ("musicbrainz", "musicbrainz.org", 443),
    ("listenbrainz_labs", "labs.api.listenbrainz.org", 443),
    ("cover_art_archive", "coverartarchive.org", 443),
)

_UPSTREAM_TIMEOUT_SECONDS = 5.0

# Anything shaped like a URL with an embedded credential, plus any scheme we
# know is a credential in its own right. Applied to text that came from an
# exception, which nobody vetted.
_CREDENTIAL_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"]+", re.IGNORECASE)


@dataclass(frozen=True)
class CheckResult:
    """One line of the checklist.

    `next_step` is what the operator should do about it, and is `None` only
    when the verdict is `pass` — a finding nobody can act on is a finding that
    wastes the reader's time.
    """

    name: str
    status: str
    detail: str
    next_step: str | None = None

    def __post_init__(self) -> None:
        """Reject a status the renderers and exit code do not understand."""
        if self.status not in STATUSES:
            raise ValueError(f"unknown check status {self.status!r}")

    def as_dict(self) -> dict[str, Any]:
        """Render this check as the object `--json` emits for it."""
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "next_step": self.next_step,
        }


def _redact(text: str | None) -> str:
    """Scrub anything URL-shaped out of text that came from an exception.

    A channel's `last_error` is `str(exc)` raised by Apprise, and an Apprise
    URL is a credential. Nobody chose to write it there, which is exactly why
    it cannot be trusted to be safe to print.
    """
    if not text:
        return ""
    return _CREDENTIAL_URL.sub("<redacted-url>", text).strip()


def _age(moment: datetime | None, *, now: datetime) -> str:
    """Format a coarse, stable age; ``"never"`` for an absent timestamp."""
    if moment is None:
        return "never"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((now - moment).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _check_data_dir(data_dir: Path) -> CheckResult:
    if not data_dir.exists():
        return CheckResult(
            "data_dir",
            "fail",
            f"{data_dir} does not exist",
            "Create it, or point $ENCORE_DATA_DIR (or --data-dir) at the real one.",
        )
    if not data_dir.is_dir():
        return CheckResult(
            "data_dir", "fail", f"{data_dir} is not a directory", "Move whatever is in its way."
        )
    if not os.access(data_dir, os.R_OK | os.W_OK | os.X_OK):
        return CheckResult(
            "data_dir",
            "fail",
            f"{data_dir} is not readable and writable by this user",
            "Run encore as the user that owns the data directory, or fix its ownership.",
        )
    return CheckResult("data_dir", "pass", f"{data_dir} exists and is writable")


def _check_key_file(data_dir: Path) -> CheckResult:
    """Check the key file's existence, mode and ownership — never its contents."""
    key_path = data_dir / KEY_FILENAME
    if not key_path.exists():
        database = data_dir / DB_FILENAME
        if database.exists():
            return CheckResult(
                "key_file",
                "fail",
                f"{key_path} is missing while {database.name} exists",
                "Restore the database and its key from the same backup. Do not let encore "
                "create a replacement key: every stored secret is encrypted under the old one.",
            )
        return CheckResult(
            "key_file",
            "fail",
            f"{key_path} is missing",
            "Run any encore command that opens storage to create it, then re-run doctor.",
        )
    try:
        info = key_path.stat()
    except OSError as exc:
        return CheckResult(
            "key_file", "fail", f"cannot stat {key_path}: {exc}", "Check the mount and the path."
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        return CheckResult(
            "key_file",
            "fail",
            f"{key_path} is mode {mode:04o}; group and other must have no access",
            f"chmod 600 {key_path}",
        )
    if info.st_uid != os.getuid():
        return CheckResult(
            "key_file",
            "warn",
            f"{key_path} is owned by uid {info.st_uid}, not this user ({os.getuid()})",
            "Run encore as the owning user, or chown the key to the service user.",
        )
    return CheckResult("key_file", "pass", f"{key_path} is mode {mode:04o} and owned by this user")


def _skipped_db_checks(reason: str) -> list[CheckResult]:
    """Every database-backed check, reported as not run and why.

    Not omitted, and not passed. The caller reaches this when the key is
    missing, and the issue this implements is explicit that doctor must not
    touch the database in that case — so these are the checks it deliberately
    did not perform, said out loud.
    """
    names = (
        "database",
        "journal_mode",
        "schema_version",
        "plex_credentials",
        "review_queue",
        "channels",
        "deliveries",
        *(f"scheduler:{job}" for job in SCHEDULER_JOB_IDS),
    )
    return [
        CheckResult(name, "skipped", reason, "Fix the checks above, then re-run doctor.")
        for name in names
    ]


def _check_database(storage: Storage) -> list[CheckResult]:
    results: list[CheckResult] = []

    integrity = storage.integrity_check()
    if integrity == "ok":
        results.append(CheckResult("database", "pass", "PRAGMA integrity_check reports ok"))
    else:
        results.append(
            CheckResult(
                "database",
                "fail",
                f"PRAGMA integrity_check reports {integrity!r}",
                "Restore from a backup. A corrupt SQLite file does not repair itself.",
            )
        )

    journal = storage.journal_mode().lower()
    if journal == "wal":
        results.append(CheckResult("journal_mode", "pass", "wal"))
    else:
        results.append(
            CheckResult(
                "journal_mode",
                "warn",
                f"journal_mode is {journal!r}, expected wal (docs/adr/0005)",
                "Reopen the database with encore, which sets WAL on every open.",
            )
        )

    version = storage.schema_version()
    expected = len(MIGRATIONS)
    if version == expected:
        results.append(CheckResult("schema_version", "pass", f"v{version}, current"))
    elif version < expected:
        results.append(
            CheckResult(
                "schema_version",
                "warn",
                f"database is v{version}, this build expects v{expected}",
                "Run any encore command that opens storage; migrations are applied on open.",
            )
        )
    else:
        results.append(
            CheckResult(
                "schema_version",
                "fail",
                f"database is v{version}, newer than this build understands (v{expected})",
                "Upgrade encore rather than downgrading the database.",
            )
        )
    return results


def _check_plex_credentials(storage: Storage) -> CheckResult:
    """Presence and decryptability. The token's value is never read out."""
    try:
        credentials = storage.get_plex_credentials()
    except SecretDecryptionError as exc:
        return CheckResult(
            "plex_credentials",
            "fail",
            f"the stored Plex token cannot be decrypted: {_redact(str(exc))}",
            "The key file does not match the database. Restore both from the same backup.",
        )
    if credentials is None:
        return CheckResult(
            "plex_credentials",
            "warn",
            "no Plex connection is configured",
            "Run `encore configure` to connect a Plex server; sync stays idle without it.",
        )
    base_url, _token = credentials
    return CheckResult("plex_credentials", "pass", f"stored and decryptable for {base_url}")


def _check_review_queue(storage: Storage) -> CheckResult:
    depth = len(storage.list_review_queue())
    if depth == 0:
        return CheckResult("review_queue", "pass", "no matches are waiting for a decision")
    return CheckResult(
        "review_queue",
        "warn",
        f"{depth} match(es) waiting for a decision",
        "Run `encore matches list` — an artist stays unwatched until its match is resolved.",
    )


def _check_channels(storage: Storage) -> CheckResult:
    channels = storage.list_channels()
    if not channels:
        return CheckResult(
            "channels",
            "warn",
            "no notification channels are configured",
            "Run `encore channels add` — releases are recorded but nothing is delivered.",
        )
    broken = [c for c in channels if c.consecutive_failures >= CHANNEL_FAILURE_WARN]
    disabled = [c for c in channels if not c.enabled]
    if broken:
        detail = "; ".join(
            f"{c.name}: {c.consecutive_failures} consecutive failures, "
            f"last error {_redact(c.last_error) or 'not recorded'}"
            for c in broken
        )
        return CheckResult(
            "channels",
            "warn",
            detail,
            "Run `encore channels test <name>` after fixing the destination.",
        )
    note = f"{len(channels)} configured"
    if disabled:
        note += f", {len(disabled)} disabled"
    return CheckResult("channels", "pass", f"{note}, none failing")


def _check_deliveries(storage: Storage) -> CheckResult:
    counts = storage.delivery_counts()
    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0)
    if failed:
        return CheckResult(
            "deliveries",
            "warn",
            f"{failed} delivery(ies) exhausted their retries, {pending} pending",
            "Check the channel above, then re-run `encore notify` for the next cycle.",
        )
    return CheckResult(
        "deliveries", "pass", f"{pending} pending, {counts.get('delivered', 0)} delivered"
    )


def _check_scheduler_heartbeats(storage: Storage, *, now: datetime) -> list[CheckResult]:
    """One line per background job, from the heartbeats it persisted.

    A job with no row has never run in this install. That is reported as
    `warn` with "never", never as a fresh success — the absence is the
    finding.
    """
    recorded = {row.job_id: row for row in storage.list_scheduler_heartbeats()}
    results: list[CheckResult] = []
    for job_id in SCHEDULER_JOB_IDS:
        name = f"scheduler:{job_id}"
        row = recorded.get(job_id)
        if row is None:
            results.append(
                CheckResult(
                    name,
                    "warn",
                    "has never run in this install",
                    "Start `encore serve` (or run the matching command by hand) — a job that "
                    "has never run is not the same as one that ran and found nothing.",
                )
            )
            continue
        if row.consecutive_failures:
            results.append(
                CheckResult(
                    name,
                    "fail",
                    f"{row.consecutive_failures} consecutive failure(s), last "
                    f"{_age(row.last_failure_at, now=now)}: "
                    f"{_redact(row.last_error) or 'no error recorded'}",
                    "Read the server log for this job; the heartbeat records only the summary.",
                )
            )
            continue
        results.append(
            CheckResult(name, "pass", f"last succeeded {_age(row.last_success_at, now=now)}")
        )
    return results


def _check_upstream() -> list[CheckResult]:
    """One TCP connect per upstream. Only ever called with --check-upstream."""
    results: list[CheckResult] = []
    for name, host, port in CHECK_UPSTREAM_HOSTS:
        try:
            with socket.create_connection((host, port), timeout=_UPSTREAM_TIMEOUT_SECONDS):
                results.append(CheckResult(f"upstream:{name}", "pass", f"{host}:{port} reachable"))
        except OSError as exc:
            results.append(
                CheckResult(
                    f"upstream:{name}",
                    "warn",
                    f"{host}:{port} unreachable: {exc}",
                    "Check this host's DNS and outbound network before blaming encore.",
                )
            )
    return results


def _rate_limit_note() -> CheckResult:
    """Report the MusicBrainz rate-limit counter as unreadable, and say why.

    `MB_RATE_LIMITER` is a module-level singleton holding the timestamp of the
    last request *in the process that made it*. `encore doctor` is a different
    process, so the honest answer is "not visible from here" — printing the
    fresh singleton's `0` would be reporting this process's own idleness as
    the server's.
    """
    return CheckResult(
        "musicbrainz_rate_limit",
        "skipped",
        "the counter lives on an in-process singleton in the running server, so a separate "
        "CLI process cannot read it",
        "Read it from the server's own /metrics endpoint instead.",
    )


def run_checks(
    data_dir: str | Path | None = None,
    *,
    check_upstream: bool = False,
    now: datetime | None = None,
) -> list[CheckResult]:
    """Run the whole checklist, in the order an operator should read it.

    Opens no socket unless `check_upstream` is true, and never creates the key
    file: a missing key is a finding, and manufacturing one while diagnosing
    would destroy every secret already encrypted under the real one.
    """
    moment = now or datetime.now(UTC)
    resolved = resolve_data_dir(data_dir)
    results = [_check_data_dir(resolved)]

    key_result = _check_key_file(resolved)
    results.append(key_result)

    if key_result.status == "fail" or results[0].status == "fail":
        results.extend(
            _skipped_db_checks("not run: the data directory or key file check failed above")
        )
    else:
        try:
            storage = Storage(resolved)
        except StorageError as exc:
            results.extend(_skipped_db_checks(f"storage would not open: {_redact(str(exc))}"))
        except SecretKeyError as exc:  # pragma: no cover - Storage wraps this already
            results.extend(_skipped_db_checks(f"key unusable: {_redact(str(exc))}"))
        else:
            try:
                results.extend(_check_database(storage))
                results.append(_check_plex_credentials(storage))
                results.append(_check_review_queue(storage))
                results.append(_check_channels(storage))
                results.append(_check_deliveries(storage))
                results.extend(_check_scheduler_heartbeats(storage, now=moment))
            finally:
                storage.close()

    results.append(_rate_limit_note())
    if check_upstream:
        results.extend(_check_upstream())
    return results


def exit_code(results: list[CheckResult]) -> int:
    """0 all pass, 1 any warn, 2 any fail. `skipped` counts as neither."""
    return max((_EXIT_FOR[r.status] for r in results), default=EXIT_OK)


def render_text(results: list[CheckResult]) -> str:
    """Render the checklist as the plain-text report the CLI prints."""
    width = max((len(r.name) for r in results), default=0)
    lines = []
    for result in results:
        lines.append(f"{result.status.upper():<7} {result.name:<{width}}  {result.detail}")
        if result.next_step:
            lines.append(f"{'':<7} {'':<{width}}  → {result.next_step}")
    counts = {status: sum(1 for r in results if r.status == status) for status in STATUSES}
    lines.append("")
    lines.append(
        f"{counts['pass']} pass, {counts['warn']} warn, {counts['fail']} fail, "
        f"{counts['skipped']} skipped"
    )
    return "\n".join(lines)


def render_json(results: list[CheckResult]) -> dict[str, Any]:
    """Build the `--json` document, whose shape `docs/doctor-schema.json` pins.

    Deliberately carries no timestamp of its own: the issue asks for output
    that is stable across runs on unchanged state, and a generated-at field
    would make every run differ.
    """
    return {
        "schema": "encore.doctor/1",
        "exit_code": exit_code(results),
        "summary": {status: sum(1 for r in results if r.status == status) for status in STATUSES},
        "checks": [result.as_dict() for result in results],
    }
