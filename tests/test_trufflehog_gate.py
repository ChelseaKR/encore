"""The full-history secret scan grades findings by exact fingerprint, and nothing wider.

The weekly scan (`.github/workflows/trufflehog.yml`) runs only on a schedule, so
a broken gate would not show up until a Sunday, and then only as a green check.
These tests run on every PR. They pin both halves of the gate.

* It still fails. An allowlist entry covers one detector, one commit, one path
  and one value. A neighbouring commit, file or credential fails. So does a
  scan that errored, read nothing, or lost its output.
* It is still the whole-history, all-tier scan. The workflow must not pick up a
  path exclusion, a scan base or `--only-verified`. Any of those makes the
  allowlist pointless and fails far more quietly.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
from scripts.trufflehog_gate import (
    FOUND_EXIT,
    GateError,
    check_scan,
    grade,
    load_allowlist,
    main,
    parse_findings,
    scanned_chunks,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "trufflehog.yml"
ALLOWLIST = ROOT / ".github" / "trufflehog-allowlist.toml"

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
# Raw values are opaque to the gate. These are not credential-shaped on purpose:
# this file is itself scanned.
RAW = "fixture-value-one"
OTHER_RAW = "fixture-value-two"
LOG = '{"level":"info-0","msg":"finished scanning","chunks":1725,"unverified_secrets":1}\n'


def _result(commit: str = COMMIT, path: str = "docs/x.md", raw: str = RAW) -> str:
    git: dict[str, object] = {"commit": commit, "line": 3}
    if path:
        git["file"] = path
    record = {
        "DetectorName": "URI",
        "Raw": raw,
        "Redacted": "redacted-form",
        "SourceMetadata": {"Data": {"Git": git}},
    }
    return json.dumps(record)


def _entry(commit: str = COMMIT, path: str = "docs/x.md", raw: str = RAW) -> str:
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return (
        f'[[finding]]\ndetector = "URI"\ncommit = "{commit}"\npath = "{path}"\n'
        f'raw_sha256 = "{digest}"\nreason = "a reviewed placeholder"\nadded = 2026-09-18\n'
    )


def _run(
    tmp_path: Path, findings: list[str], allowlist: str, exit_code: int, log: str = LOG
) -> int:
    (tmp_path / "f.jsonl").write_text("".join(f + "\n" for f in findings), encoding="utf-8")
    (tmp_path / "scan.log").write_text(log, encoding="utf-8")
    (tmp_path / "allow.toml").write_text(allowlist, encoding="utf-8")
    argv = ["--findings", str(tmp_path / "f.jsonl"), "--log", str(tmp_path / "scan.log")]
    argv += ["--exit-code", str(exit_code), "--allowlist", str(tmp_path / "allow.toml")]
    return main(argv)


# -- it passes only what is listed -------------------------------------------


def test_a_clean_scan_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, [], "", 0) == 0


def test_an_allowlisted_finding_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, [_result()], _entry(), FOUND_EXIT) == 0


def test_a_commit_message_finding_is_matched_by_the_empty_path(tmp_path: Path) -> None:
    assert _run(tmp_path, [_result(path="")], _entry(path=""), FOUND_EXIT) == 0


def test_an_unlisted_finding_fails_and_prints_its_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, [_result()], "", FOUND_EXIT) == 1
    out = capsys.readouterr().out
    assert "::error" in out
    assert COMMIT in out
    assert hashlib.sha256(RAW.encode()).hexdigest() in out


@pytest.mark.parametrize(
    "neighbour",
    [
        _result(commit=OTHER_COMMIT),  # the same value, committed again later
        _result(path="src/encore/other.py"),  # the same value, in another file
        _result(raw=OTHER_RAW),  # another value, in the listed file and commit
        _result(path=""),  # the listed value, but in the commit message
    ],
    ids=["other-commit", "other-path", "other-value", "commit-message"],
)
def test_an_entry_covers_nothing_next_to_it(tmp_path: Path, neighbour: str) -> None:
    assert _run(tmp_path, [_result(), neighbour], _entry(), FOUND_EXIT) == 1


def test_a_stale_entry_warns_but_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, [], _entry(), 0) == 0
    assert "::warning::" in capsys.readouterr().out


# -- it refuses to grade a scan it cannot trust ------------------------------


@pytest.mark.parametrize(
    ("exit_code", "findings", "log"),
    [
        (1, [], LOG),  # TruffleHog errored, e.g. a repeated flag
        (0, [], '{"msg":"finished scanning","chunks":0}\n'),  # read nothing
        (0, [], '{"msg":"scanning repo"}\n'),  # never finished
        (FOUND_EXIT, [], LOG),  # said it found something; the output is gone
        (0, [_result()], LOG),  # the output and the verdict disagree
        (FOUND_EXIT, ["not json"], LOG),
    ],
    ids=["errored", "zero-chunks", "unfinished", "lost-output", "disagree", "not-json"],
)
def test_an_untrustworthy_scan_is_not_graded(
    tmp_path: Path, exit_code: int, findings: list[str], log: str
) -> None:
    assert _run(tmp_path, findings, "", exit_code, log) == 2


def test_the_chunk_count_is_read_from_either_log_format() -> None:
    console = '2026-09-13T08:13:10Z\tinfo-0\ttrufflehog\tfinished scanning\t{"chunks": 1709}'
    assert scanned_chunks(console) == 1709
    assert scanned_chunks(LOG) == 1725


@pytest.mark.parametrize(
    "bad",
    [
        _entry().replace('reason = "a reviewed placeholder"', 'reason = "  "'),
        _entry().replace(COMMIT, "a" * 7),  # an abbreviated SHA could match many commits
        _entry().replace('path = "docs/x.md"\n', ""),
        _entry() + _entry(),
        "[[finding]\n",
    ],
    ids=["no-reason", "short-sha", "no-path", "duplicate", "not-toml"],
)
def test_a_vague_allowlist_is_refused(bad: str) -> None:
    with pytest.raises(GateError):
        load_allowlist(bad)


def test_check_scan_accepts_the_two_honest_verdicts() -> None:
    check_scan(0, LOG, [])
    check_scan(FOUND_EXIT, LOG, parse_findings(_result()))


def test_a_scanner_error_is_named_as_one() -> None:
    # A repeated `--fail` makes TruffleHog exit 1 before it scans anything, and
    # the log then has no summary at all. The exit status is the better reason.
    with pytest.raises(GateError, match="exited 1"):
        check_scan(1, "trufflehog: error: flag 'fail' cannot be repeated\n", [])


def test_grade_is_the_whole_verdict() -> None:
    findings = parse_findings(_result())
    assert grade(findings, load_allowlist(_entry())) == 0
    assert grade(findings, {}) == 1


# -- the committed allowlist and workflow ------------------------------------


def test_the_committed_allowlist_is_exact_and_reasoned() -> None:
    allowed = load_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    assert allowed, "the allowlist parsed to nothing"
    # Every entry is one detector, one full SHA, one path and one digest. No
    # entry names a directory or a pattern, and none excludes a whole detector.
    for detector, commit, path, digest in allowed:
        assert detector, "an entry must name its detector"
        assert len(commit) == 40
        assert len(digest) == 64
        assert not any(ch in path for ch in "*?[")


def test_the_workflow_runs_the_whole_scan_through_the_gate() -> None:
    # Commentary is dropped first: the workflow explains `--only-verified` and
    # `--fail` in prose, and only what it executes counts here.
    text = "\n".join(
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "fetch-depth: 0" in text, "without full history this is a one-commit scan"
    assert "--results=verified,unknown,unverified" in text
    assert "python3 scripts/trufflehog_gate.py" in text
    assert "--allowlist .github/trufflehog-allowlist.toml" in text
    assert re.search(r"trufflehog:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", text), "image not pinned"
    # `--fail` exactly once: a repeated flag is a CLI error, and exit 183 is how
    # the gate knows TruffleHog meant its findings.
    assert len(re.findall(r"--fail\b", text)) == 1
    # The narrowings this gate exists to avoid. Each would turn the scan green
    # over far more than the listed fingerprints.
    for widening_loss in (
        "--only-verified",
        "--exclude-paths",
        "--exclude-globs",
        "--since-commit",
    ):
        assert widening_loss not in text, widening_loss
    assert re.findall(r"--exclude-detectors=(\S+)", text) == ["Lob"]


def test_inline_ignores_are_only_the_reviewed_one() -> None:
    """TruffleHog skips any line that carries its inline ignore tag, anywhere on the line.

    The allowlist is reviewed and exact; an inline tag is neither, so every line
    carrying one is listed here and a new one has to change this test. The tag is
    spelled in two parts below so that this module never carries it itself.
    """
    tag = "trufflehog:" + "ignore"
    tracked = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local paths
        ["git", "-C", str(ROOT), "ls-files", "-z"],  # noqa: S607
        capture_output=True,
        check=True,
    ).stdout.decode()
    tagged: dict[str, int] = {}
    for name in filter(None, tracked.split("\0")):
        path = ROOT / name
        if not path.is_file():
            continue
        try:
            count = path.read_text(encoding="utf-8").count(tag)
        except UnicodeDecodeError:
            continue
        if count:
            tagged[name] = count
    assert tagged == {"tests/test_endpoints.py": 1}
