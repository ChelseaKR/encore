#!/usr/bin/env python3
"""Fail when a committed file gains a reference to a path outside this repository.

Issue #22: `docs/ROADMAP.md`, `docs/RESPONSIBLE-TECH-AUDITS.md`, the ADRs, the
Definition of Done, and several source docstrings delegate the feature plan, the
architecture, and the M0-M4 exit criteria to `../encore-plans/` -- a plain local
directory that is not a git repository, not a submodule, and has never been in
this repository's history. For anyone who clones encore, every one of those
pointers resolves to nothing, and on the day the repo goes public they go public
with it.

Resolving each one is a per-document decision the maintainer has to make
(graduate it into `docs/`, publish it separately and link by URL, or cut the
reference and say what to read instead). This gate does not make that decision.
It makes the count a **ratchet**: the ledger in `.external-refs.yml` is a
ceiling, so a reference can be removed freely but a new one cannot appear
without an explicit, reviewable ledger edit. Before the public/private flip
(ROADMAP section 8, M4) the ledger should reach zero.

Usage: uv run python scripts/external_refs_gate.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

LEDGER = ".external-refs.yml"
SELF_EXEMPT = {LEDGER, "scripts/external_refs_gate.py"}

# A markdown link or bare path token that climbs out of the repository root.
_PARENT_LINK = re.compile(r"\]\(\s*((?:\.\./)+[^)\s]*)")


def _git(*args: str) -> str:
    """Run a git subcommand and return its stdout.

    `git` is resolved from PATH on purpose (S603/S607): this is a repo-local
    developer tool that only ever runs inside a checkout, the argument list is
    a fixed literal with no interpolation, and hard-coding an absolute path
    would break on every platform whose git lives somewhere else.
    """
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def _tracked_files() -> list[str]:
    """Every path git tracks, as repo-relative strings."""
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _load_ledger(root: Path) -> tuple[dict[str, int], list[str]]:
    """Return (per-path ceiling, configured external root names)."""
    path = root / LEDGER
    if not path.is_file():
        raise SystemExit(f"external-refs: FAIL - {LEDGER} is missing; the gate has no ceiling.")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise SystemExit(f"external-refs: FAIL - {LEDGER} must be a mapping.")
    roots = doc.get("external_roots") or []
    entries = doc.get("allowed") or {}
    if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
        raise SystemExit(f"external-refs: FAIL - {LEDGER}: 'external_roots' must be a string list.")
    if not isinstance(entries, dict):
        raise SystemExit(f"external-refs: FAIL - {LEDGER}: 'allowed' must be a mapping.")
    ceiling: dict[str, int] = {}
    for key, value in entries.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SystemExit(f"external-refs: FAIL - {LEDGER}: '{key}' must be a count >= 0.")
        ceiling[str(key)] = value
    return ceiling, roots


def _count_refs(text: str, roots: list[str], repo_depth: int) -> int:
    """External references in one file's text."""
    hits = 0
    for root_name in roots:
        hits += len(re.findall(re.escape(root_name), text))
    for match in _PARENT_LINK.finditer(text):
        target = match.group(1)
        # `../` links are only external if they climb past the repository root.
        if target.count("../") > repo_depth and not any(r in target for r in roots):
            hits += 1
    return hits


def _scan(
    root: Path, ceiling: dict[str, int], roots: list[str]
) -> tuple[list[str], list[str], int]:
    """Return (violations, ledger-slack notes, total references found)."""
    violations: list[str] = []
    slack: list[str] = []
    total = 0
    for rel in _tracked_files():
        if rel in SELF_EXEMPT:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        count = _count_refs(text, roots, repo_depth=rel.count("/"))
        if count == 0:
            continue
        total += count
        allowed = ceiling.get(rel, 0)
        if count > allowed:
            violations.append(f"{rel}: {count} external reference(s), ledger allows {allowed}")
        elif count < allowed:
            slack.append(f"{rel}: {count} now, ledger still allows {allowed} - tighten it")
    return violations, slack, total


def main() -> int:
    """Compare live external-reference counts against the ledger ceiling."""
    root = Path(_git("rev-parse", "--show-toplevel").strip())
    ceiling, roots = _load_ledger(root)
    if not roots:
        raise SystemExit(f"external-refs: FAIL - {LEDGER}: 'external_roots' is empty.")

    violations, slack, total = _scan(root, ceiling, roots)
    for line in slack:
        print(f"external-refs: note - {line}")
    if violations:
        for line in violations:
            print(f"external-refs: FAIL - {line}", file=sys.stderr)
        print(
            "external-refs: a committed file must not reference a path outside this\n"
            f"  repository. Resolve it (issue #22), or raise the ceiling in {LEDGER}\n"
            "  deliberately and say why in the same change.",
            file=sys.stderr,
        )
        return 1

    print(
        f"external-refs: clean - {total} known external reference(s), none above the "
        f"{LEDGER} ceiling."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
