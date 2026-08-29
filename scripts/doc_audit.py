#!/usr/bin/env python3
"""Documentation inventory and link audit, generated from the tree (never typed).

``docs/DOCUMENTATION-AUDIT.md`` was a hand-written table of ``pass`` verdicts backed by
counted evidence. The verdicts stayed; the counts stopped describing this repository.
Measured on ``origin/main`` the day this tool was added:

* "3 test files (2 test modules + ``tests/__init__.py``)" against **33** files under
  ``tests/``, 29 of them ``test_*.py``;
* "architecture and interfaces | 12" against **15** files under ``docs/adr/``, with
  ADRs 0011, 0012 and 0013 absent from the inventory list below the table;
* "safety, privacy, accessibility, and audits | 3" against **5**, missing
  ``docs/audits/residual-risk.md`` and ``docs/audits/security-threat-model.md``.

Nothing generated or checked the file, so a document whose entire purpose was to show
that this project's process claims are real was itself a validation surface reporting
success about records it no longer inspected. That is the defect class this repository
polices everywhere else: a committed artifact standing in for a computation, with
nothing that re-runs the computation and compares.

This tool removes the possibility. It regenerates the machine-derived block of that
document between its ``BEGIN GENERATED`` / ``END GENERATED`` markers, so every count is
read off the tree at the commit that ships it::

    make docs-audit          # rewrite the generated block
    make docs-audit-check    # fail if the committed block has drifted (in `make verify`)

Ported from ``nearmiss/tools/doc_audit.py``, whose own docstring documents the seam and
asks each port to review the taxonomy rather than share a package. What changed for
Encore: the category rules and entry/process tuple are this repository's; there is no
Node workspace, so the npm-scripts collector is gone; the gate-script inventory is new,
because ``scripts/`` here holds merge-gate logic that ``lint`` and ``mypy`` cover to the
same standard as ``src`` and a reader should be able to see how much of it there is.

Two deliberate choices about honesty, both inherited:

* **``pass`` is reserved for a real predicate.** Presence checks (does ``README.md``
  exist?) and the link check (does every relative link resolve?) can pass or fail. An
  inventory count cannot: "33 test files" is not a verdict. The old table's standing
  ``pass`` on "Validation surface | 3 test files" is exactly the kind of borrowed
  authority that reads as a conformance result, and it was wrong by a factor of eleven
  while still displaying ``pass``.
* **No generated timestamp.** A date in the output would drift every day and make the
  drift check meaningless, and git already dates the file. The dated narrative of the
  original 2026-07-08 sweep is kept *outside* the generated block, as history, where it
  cannot masquerade as a current verdict.

``--check`` writes nothing. A gate that regenerates the artifact it is judging heals
drift on the contributor's disk while the committed bytes stay stale, which is how this
class of staleness hides in the first place.

Pure standard library, no network, deterministic: identical tree, identical bytes.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "docs" / "DOCUMENTATION-AUDIT.md"

BEGIN = "<!-- BEGIN GENERATED: doc-audit (scripts/doc_audit.py) -->"
END = "<!-- END GENERATED: doc-audit -->"

# Directory names with no hand-authored documentation to audit, excluded wherever they
# appear. Matching on the name rather than a root-relative prefix matters: the audit has
# to produce the same numbers whether or not this checkout happens to have a `.venv/`,
# a `dist/` from `make wheel`, or an `htmlcov/` from a coverage run in it.
#
# Cross-checked against .gitignore rather than from memory. `test_doc_audit.py` asserts
# that no gitignored Markdown reaches the inventory, which is the durable half of this.
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".claude",
        ".eggs",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".standards",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "venv",
    }
)

# Root-relative prefixes holding a contributor's local runtime data rather than
# repository content. `.gitignore` excludes `/instance/` and `/data/` (a self-hosted
# instance's database, backups, and Fernet key must never be committed), so any Markdown
# beneath them belongs to one checkout and not to this repository. These need a
# root-relative prefix where EXCLUDED_DIR_NAMES needs a bare name: "data" is an ordinary
# word a genuine docs directory could use, and excluding it wherever it appears would
# silently drop authored documentation.
EXCLUDED_PATH_PREFIXES = (
    "data/",
    "instance/",
    "secrets/",
)

ROOT_PROCESS_DOCS = ("CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")
ROOT_LEGAL_DOCS = ("LICENSE", "NOTICE", "CITATION.cff", "CODE_OF_CONDUCT.md")
ROOT_TEMPLATES = (".github/PULL_REQUEST_TEMPLATE.md", ".github/CODEOWNERS")

# Category rules, in order: the first prefix/name that matches wins. Written as data so
# the categorization is reviewable rather than buried in branches.
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "safety, privacy, accessibility, and audits",
        (
            "docs/audits/",
            "docs/DOCUMENTATION-AUDIT.md",
            "docs/RESPONSIBLE-TECH-AUDITS.md",
        ),
    ),
    ("architecture and interfaces", ("docs/adr/",)),
    ("planning and research", ("docs/ROADMAP.md",)),
)
ENTRY_AND_PROCESS = (
    ".github/CODEOWNERS",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "CHANGELOG.md",
    "CITATION.cff",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "SECURITY.md",
)
OTHER = "other docs"

# Representative files shown per category. The complete list is printed below the
# tables, so the table stays readable without hiding anything.
_SAMPLE = 5

# A relative Markdown link: [text](target). Absolute URLs, mailto: and pure anchors are
# out of scope — this check is about links that must resolve inside the tree.
_LINK = re.compile(r"\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
_SKIP_LINK = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//|#)")
_CODE_FENCE = re.compile(r"(?ms)^```.*?^```\s*?$")


def _relative(path: Path) -> str:
    """Return ``path`` as a POSIX path relative to the repository root."""
    return path.relative_to(ROOT).as_posix()


def _excluded(rel: str) -> bool:
    """Report whether this root-relative path is outside the authored-doc surface."""
    if rel.startswith(EXCLUDED_PATH_PREFIXES):
        return True
    return any(part in EXCLUDED_DIR_NAMES for part in rel.split("/")[:-1])


def authored_docs() -> list[str]:
    """Every hand-authored Markdown file, plus the non-Markdown root process files."""
    found: set[str] = set()
    for path in ROOT.rglob("*.md"):
        rel = _relative(path)
        if _excluded(rel):
            continue
        found.add(rel)
    for rel in (*ROOT_LEGAL_DOCS, *ROOT_TEMPLATES):
        if (ROOT / rel).is_file():
            found.add(rel)
    return sorted(found)


def _category(rel: str) -> str:
    """Return the inventory category this document belongs to."""
    for name, prefixes in CATEGORY_RULES:
        if any(rel == prefix or rel.startswith(prefix) for prefix in prefixes):
            return name
    if rel in ENTRY_AND_PROCESS:
        return "entry points and repo process"
    return OTHER


def _test_files() -> list[str]:
    """Every ``tests/test_*.py`` module, the countable unit for the validation surface."""
    tests = ROOT / "tests"
    if not tests.is_dir():
        return []
    return sorted(_relative(p) for p in tests.glob("test_*.py"))


def _test_support_files() -> list[str]:
    """Tracked ``tests/`` files that are not ``test_*.py`` (fixtures, ``__init__``)."""
    tests = ROOT / "tests"
    if not tests.is_dir():
        return []
    return sorted(_relative(p) for p in tests.glob("*.py") if not p.name.startswith("test_"))


def _workflows() -> list[str]:
    """Every GitHub Actions workflow file."""
    workflows = ROOT / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    return sorted(_relative(p) for p in (*workflows.glob("*.yml"), *workflows.glob("*.yaml")))


def _gate_scripts() -> list[str]:
    """Every file under ``scripts/`` — this repository keeps its gate logic there."""
    scripts = ROOT / "scripts"
    if not scripts.is_dir():
        return []
    return sorted(_relative(p) for p in scripts.iterdir() if p.is_file())


def _requires_python() -> str:
    """Return the ``requires-python`` constraint the package declares."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires: str = cfg["project"].get("requires-python", "unspecified")
    return requires


def _link_targets(text: str) -> Iterable[str]:
    """Yield every in-tree relative link target in a Markdown document."""
    for raw in _LINK.findall(_CODE_FENCE.sub("", text)):
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        target = target.split(" ")[0].split("#")[0].strip()
        if not target or _SKIP_LINK.match(target):
            continue
        yield target


def _exists_case_sensitively(target: Path) -> bool:
    """Report whether this path exists with *exactly* this spelling.

    ``Path.exists()`` is case-insensitive on macOS (APFS) and case-sensitive on the Linux
    hosts CI runs on and on github.com, so a link whose case is wrong passes on a laptop
    and 404s for every reader. That is not hypothetical in this portfolio: it is how a
    ``roadmap.md`` link survived a link check that reported "0 unresolved", recorded in
    the Link Check section of the audit itself. Each component is matched against the
    real directory listing so this gate agrees with github.com rather than with whichever
    filesystem it happened to run on.
    """
    try:
        relative = target.relative_to(ROOT)
    except ValueError:
        # A link that escapes the repository: out of scope for a repo-local check.
        return target.exists()
    cursor = ROOT
    for part in relative.parts:
        try:
            entries = {entry.name for entry in cursor.iterdir()}
        except OSError:
            return False
        if part not in entries:
            return False
        cursor = cursor / part
    return True


def check_links(docs: Iterable[str]) -> tuple[int, list[str]]:
    """Return (links checked, unresolved ``doc -> target`` strings)."""
    checked = 0
    unresolved: list[str] = []
    for rel in docs:
        path = ROOT / rel
        if path.suffix != ".md":
            continue
        for target in _link_targets(path.read_text(encoding="utf-8")):
            checked += 1
            # Textual normalisation only: realpath would fold `..` *and*, on some
            # platforms, the very case difference this check exists to catch.
            resolved = Path(os.path.normpath(path.parent / target))
            if not _exists_case_sensitively(resolved):
                unresolved.append(f"{rel} -> {target}")
    return checked, unresolved


def _present(paths: Iterable[str]) -> list[str]:
    """Return the subset of ``paths`` that is missing from the tree."""
    return [p for p in paths if not (ROOT / p).exists()]


def _verdict(missing: list[str]) -> str:
    """``pass`` when nothing is missing, ``fail`` otherwise."""
    return "pass" if not missing else "fail"


def _bullets(items: Iterable[str]) -> str:
    """Render items as a Markdown bullet list of inline-code spans."""
    return "\n".join(f"- `{item}`" for item in items)


def _presence_table(docs: list[str], checked: int, unresolved: list[str]) -> list[str]:
    """Render the predicate half of the report: checks that can pass or fail."""
    readme_missing = [] if (ROOT / "README.md").is_file() else ["README.md"]
    return [
        "## Presence and link checks",
        "",
        "These are real predicates, so they can pass or fail.",
        "",
        "| Check | Result | Evidence |",
        "| --- | --- | --- |",
        f"| Entry doc | {_verdict(readme_missing)} | `README.md`"
        f"{'' if not readme_missing else ' missing'} |",
        f"| Root process docs | {_verdict(_present(ROOT_PROCESS_DOCS))} | "
        f"{', '.join(f'`{p}`' for p in ROOT_PROCESS_DOCS)} |",
        f"| Root legal, citation, and conduct docs | {_verdict(_present(ROOT_LEGAL_DOCS))} | "
        f"{', '.join(f'`{p}`' for p in ROOT_LEGAL_DOCS)} |",
        f"| Root-adjacent GitHub templates | {_verdict(_present(ROOT_TEMPLATES))} | "
        f"{', '.join(f'`{p}`' for p in ROOT_TEMPLATES)} |",
        f"| Local doc links resolve | {_verdict(unresolved)} | {checked} relative links "
        f"checked in {len([d for d in docs if d.endswith('.md')])} Markdown files; "
        f"{len(unresolved)} unresolved |",
        "",
    ]


def _inventory_table(docs: list[str]) -> list[str]:
    """Render the count half of the report: inventory, explicitly not verdicts."""
    tests = _test_files()
    support = _test_support_files()
    return [
        "## Inventory",
        "",
        "Counts, not verdicts. A count cannot pass or fail; it can only be current, "
        "which is what generating it from the tree buys.",
        "",
        "| Surface | Count | Evidence |",
        "| --- | ---: | --- |",
        f"| Hand-authored docs | {len(docs)} | Markdown at the repository root and under "
        "`.github/` and `docs/`, plus the root legal and template files |",
        f"| Test modules | {len(tests)} | `tests/test_*.py` |",
        f"| Test support files | {len(support)} | other `tests/*.py` (fixtures, `__init__.py`) |",
        f"| Workflow files | {len(_workflows())} | `.github/workflows/*.yml` |",
        f"| Gate scripts | {len(_gate_scripts())} | `scripts/*` (lint and mypy cover "
        "these to the same standard as `src`) |",
        "",
    ]


def render() -> str:
    """Render the whole generated block, markers included."""
    docs = authored_docs()
    checked, unresolved = check_links(docs)

    categories: dict[str, list[str]] = {}
    for rel in docs:
        categories.setdefault(_category(rel), []).append(rel)

    lines: list[str] = [BEGIN, ""]
    lines += [
        "_Everything between these markers is generated by `scripts/doc_audit.py` from "
        "the tree at this commit. Do not edit it by hand: run `make docs-audit`. "
        "`make docs-audit-check` (in `make verify`) and `tests/test_doc_audit.py` fail "
        "if it has drifted._",
        "",
    ]
    lines += _presence_table(docs, checked, unresolved)
    lines += _inventory_table(docs)
    lines += [
        "### By category",
        "",
        f"Up to {_SAMPLE} representative files per category; the complete list follows below.",
        "",
        "| Category | Count | Representative files |",
        "| --- | ---: | --- |",
    ]
    for name in sorted(categories):
        members = sorted(categories[name])
        shown = ", ".join(f"`{m}`" for m in members[:_SAMPLE])
        extra = len(members) - _SAMPLE
        sample = shown + (f", plus {extra} more" if extra > 0 else "")
        lines.append(f"| {name} | {len(members)} | {sample} |")

    lines += [
        "",
        "## Workflow files checked",
        "",
        _bullets(_workflows()) or "- none found",
        "",
        "## Gate scripts checked",
        "",
        _bullets(_gate_scripts()) or "- none found",
        "",
        "## Test modules checked",
        "",
        _bullets(_test_files()) or "- none found",
        "",
        "## Package metadata",
        "",
        f"- Python package `encore` ({_requires_python()}).",
        "",
        "## Full hand-authored doc inventory",
        "",
        _bullets(docs),
        "",
    ]
    if unresolved:
        lines += ["## Unresolved links", "", _bullets(unresolved), ""]
    lines.append(END)
    return "\n".join(lines) + "\n"


def splice(document: str, generated: str) -> str:
    """Replace the marked block in ``document`` with ``generated``."""
    start = document.find(BEGIN)
    end = document.find(END)
    if start == -1 or end == -1:
        raise SystemExit(
            f"docs/DOCUMENTATION-AUDIT.md is missing the generated-block markers ({BEGIN} … {END})"
        )
    return document[:start] + generated + document[end + len(END) + 1 :]


def main(argv: list[str] | None = None) -> int:
    """Regenerate the block, or with ``--check`` compare and return ``1`` on drift."""
    parser = argparse.ArgumentParser(description="Generate or check the doc audit.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed generated block differs from the tree; writes nothing",
    )
    args = parser.parse_args(argv)

    document = AUDIT.read_text(encoding="utf-8")
    updated = splice(document, render())

    if args.check:
        if updated != document:
            print(
                "doc audit FAILED: docs/DOCUMENTATION-AUDIT.md no longer describes this "
                "tree.\n  Run `make docs-audit` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("doc audit OK: the committed inventory, counts, and link check match the tree.")
        return 0

    AUDIT.write_text(updated, encoding="utf-8")
    print(f"doc audit: regenerated the generated block in {_relative(AUDIT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
