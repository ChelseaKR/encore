"""Grade a full-history TruffleHog scan against exact, reviewed fingerprints.

`.github/workflows/trufflehog.yml` runs TruffleHog over every commit on every
branch, at every result tier, and hands its JSON output to this gate.

TruffleHog can narrow a scan only by detector, by path, or by a starting commit.
None of those can say "this one placeholder, in this one commit, is known". A
detector exclusion stops looking for that kind of credential everywhere. A path
exclusion stops scanning the file in every future commit. A scan base drops
every older commit. And a commit MESSAGE has no path at all, so only a scan
base reaches it. PR #76 put a placeholder credential URL in two files and in its
own squash-commit message, which is how the scan went red on 2026-09-13.

So the scan runs unfiltered and a finding passes only when every part of its
fingerprint matches an entry in `.github/trufflehog-allowlist.toml`:

* the detector,
* the full commit SHA -- history is immutable, so an entry cannot drift onto
  new content,
* the path in that commit, or "" for the commit message,
* the SHA-256 of the matched value, so a different credential in the same file
  of the same commit is not covered.

Line numbers are deliberately left out. TruffleHog's decoders attribute one
match to different lines: the HTML decoder placed a line-178 match at 167.

Everything else fails. So does a scan that cannot be trusted to have looked: an
exit status other than 0 or 183, no "finished scanning" record or one that read
zero chunks, output that is not JSON, or an exit status that disagrees with the
number of findings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: TruffleHog's exit status under `--fail` when it reports anything.
FOUND_EXIT = 183

_FINISHED = re.compile(r'finished scanning.*?"chunks":\s*(\d+)')
_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_REQUIRED = ("detector", "commit", "path", "raw_sha256", "reason", "added")

#: (detector, commit, path, SHA-256 of the matched value)
Fingerprint = tuple[str, str, str, str]


class GateError(Exception):
    """The scan cannot be graded: its output is missing, malformed or incomplete."""


@dataclass(frozen=True)
class Finding:
    """One TruffleHog result, reduced to what the gate matches and reports."""

    detector: str
    commit: str
    path: str
    line: int | None
    redacted: str
    raw_sha256: str

    @property
    def fingerprint(self) -> Fingerprint:
        """Return the tuple an allowlist entry must match exactly."""
        return (self.detector, self.commit, self.path, self.raw_sha256)

    @property
    def where(self) -> str:
        """Describe the location for a human."""
        place = self.path or "the commit message"
        suffix = f" line {self.line}" if self.line is not None else ""
        return f"{place}{suffix} in {self.commit[:12]}"


def parse_findings(text: str) -> list[Finding]:
    """Parse TruffleHog's `--json` output, one result per line."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            git = record["SourceMetadata"]["Data"]["Git"]
            raw = record["Raw"]
            findings.append(
                Finding(
                    detector=str(record["DetectorName"]),
                    commit=str(git["commit"]),
                    path=str(git.get("file", "")),
                    line=git.get("line"),
                    redacted=str(record.get("Redacted", "")),
                    raw_sha256=hashlib.sha256(str(raw).encode()).hexdigest(),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise GateError(f"findings line {number} is not a git result: {exc!r}") from exc
    return findings


def scanned_chunks(log: str) -> int:
    """Return the chunk count from the scan's last `finished scanning` record."""
    counts = _FINISHED.findall(log)
    if not counts:
        raise GateError("the scan log has no 'finished scanning' record; the scan did not complete")
    return int(counts[-1])


def load_allowlist(text: str) -> dict[Fingerprint, str]:
    """Parse the allowlist into {fingerprint: reason}, refusing anything vague."""
    try:
        entries = tomllib.loads(text).get("finding", [])
    except tomllib.TOMLDecodeError as exc:
        raise GateError(f"allowlist is not valid TOML: {exc}") from exc
    allowed: dict[Fingerprint, str] = {}
    for index, entry in enumerate(entries, start=1):
        missing = [key for key in _REQUIRED if key not in entry]
        if missing:
            raise GateError(f"allowlist entry {index} is missing {', '.join(missing)}")
        if not _HEX40.fullmatch(entry["commit"]) or not _HEX64.fullmatch(entry["raw_sha256"]):
            raise GateError(f"allowlist entry {index} needs a full commit SHA and a SHA-256")
        if not str(entry["reason"]).strip():
            raise GateError(f"allowlist entry {index} gives no reason")
        key = (entry["detector"], entry["commit"], entry["path"], entry["raw_sha256"])
        if key in allowed:
            raise GateError(f"allowlist entry {index} duplicates an earlier entry")
        allowed[key] = str(entry["reason"]).strip()
    return allowed


def check_scan(exit_code: int, log: str, findings: list[Finding]) -> None:
    """Refuse to grade a scan that errored, read nothing, or lost its output."""
    if exit_code not in (0, FOUND_EXIT):
        raise GateError(f"TruffleHog exited {exit_code}: the scan errored, not a finding")
    if scanned_chunks(log) <= 0:
        raise GateError("TruffleHog read zero chunks: nothing was scanned")
    if (exit_code == FOUND_EXIT) != bool(findings):
        raise GateError(
            f"TruffleHog exited {exit_code} but {len(findings)} findings were parsed: "
            "the output and the verdict disagree"
        )


def grade(findings: list[Finding], allowed: dict[Fingerprint, str]) -> int:
    """Print the verdict; return 1 if any finding is not on the allowlist."""
    unknown = [f for f in findings if f.fingerprint not in allowed]
    for finding in findings:
        if finding.fingerprint in allowed:
            print(f"allowlisted: {finding.detector} {finding.redacted} at {finding.where}")
    for finding in unknown:
        print(
            f"::error title=TruffleHog {finding.detector}::{finding.redacted} at {finding.where}. "
            "If this is a reviewed placeholder, add to .github/trufflehog-allowlist.toml: "
            f'detector = "{finding.detector}", commit = "{finding.commit}", '
            f'path = "{finding.path}", raw_sha256 = "{finding.raw_sha256}"'
        )
    for key in sorted(allowed.keys() - {f.fingerprint for f in findings}):
        print(f"::warning::allowlist entry matched nothing and can be removed: {key}")
    print(
        f"{len(findings)} findings, {len(findings) - len(unknown)} allowlisted, {len(unknown)} not"
    )
    return 1 if unknown else 0


def main(argv: list[str] | None = None) -> int:
    """Grade one scan. Exit 0 clean, 1 on an unreviewed finding, 2 if ungradeable."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--findings", type=Path, required=True, help="TruffleHog --json stdout")
    parser.add_argument("--log", type=Path, required=True, help="TruffleHog stderr")
    parser.add_argument("--exit-code", type=int, required=True, help="TruffleHog's exit status")
    parser.add_argument("--allowlist", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        findings = parse_findings(args.findings.read_text(encoding="utf-8"))
        check_scan(args.exit_code, args.log.read_text(encoding="utf-8"), findings)
        allowed = load_allowlist(args.allowlist.read_text(encoding="utf-8"))
    except (GateError, OSError) as exc:
        print(f"::error title=TruffleHog scan not gradeable::{exc}")
        return 2
    return grade(findings, allowed)


if __name__ == "__main__":
    sys.exit(main())
