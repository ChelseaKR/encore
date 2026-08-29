"""Gates for the claims README.md makes about this repository's own machinery.

Nothing in this project reads the README, so every figure in it was a
hand-maintained literal with no way to go red: the CI/CD row claimed
`make verify` was "the literal command CI and `release.yml` run" long after
`ci.yml` had stopped running it, and the status line kept a feature-by-feature
enumeration that was four features stale. Correcting such a literal only
restarts the same clock. These tests derive each figure from the artifact it
describes — the workflows, the Makefile, `src/encore/`, `docs/ROADMAP.md` — and
fail when the README and the repository disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
PR_TEMPLATE = REPO / ".github" / "PULL_REQUEST_TEMPLATE.md"
ROADMAP = REPO / "docs" / "ROADMAP.md"
MAKEFILE = REPO / "Makefile"
WORKFLOWS = REPO / ".github" / "workflows"

# `make install` is a prerequisite of running any gate, not one of the gates
# `verify` composes, so its absence from `verify` is not drift.
_NOT_A_GATE = frozenset({"install"})


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _run_steps(workflow: Path) -> list[str]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    steps: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if isinstance(run, str):
                steps.append(run)
    return steps


def _make_targets(workflow: Path) -> set[str]:
    pattern = re.compile(r"(?:^|&&|\n)\s*make\s+([A-Za-z0-9_.-]+)")
    return {target for run in _run_steps(workflow) for target in pattern.findall(run)}


def _verify_prerequisites() -> list[str]:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^verify:([^#\n]*)", text, re.MULTILINE)
    assert match is not None, "Makefile has no `verify:` target"
    return match.group(1).split()


def _readme_ci_cd_row() -> str:
    for line in _readme().splitlines():
        if line.startswith("| CI/CD |"):
            return line
    pytest.fail("README.md has no CI/CD row in the standards table")


def test_verify_target_still_composes_gates() -> None:
    # A `verify:` that parsed to nothing would make every gate below vacuous.
    prerequisites = _verify_prerequisites()
    assert len(prerequisites) >= 5, prerequisites


def test_readme_names_the_only_workflow_running_the_literal_make_verify() -> None:
    running = {
        workflow.name
        for workflow in sorted(WORKFLOWS.glob("*.yml"))
        if any(run.strip() == "make verify" for run in _run_steps(workflow))
    }
    assert running, "no workflow runs the literal `make verify` any more"
    claimed = re.search(
        r"`([A-Za-z0-9_.-]+\.yml)`'s `[A-Za-z0-9_-]+` runs the literal `make verify`",
        _readme_ci_cd_row(),
    )
    assert claimed is not None, "README's CI/CD row no longer names the workflow"
    assert running == {claimed.group(1)}, (
        f"README credits {claimed.group(1)} with the literal `make verify`; "
        f"the workflows that actually run it are {sorted(running)}"
    )


def test_readme_names_the_verify_targets_ci_does_not_run() -> None:
    omitted = {
        target
        for target in _verify_prerequisites()
        if target not in _make_targets(WORKFLOWS / "ci.yml")
    }
    claimed = re.search(
        r"`ci\.yml` omits ((?:`[A-Za-z0-9_.-]+`(?:,\s+|\s+and\s+)?)+)",
        _readme_ci_cd_row(),
    )
    assert claimed is not None, "README's CI/CD row no longer states what ci.yml omits"
    named = set(re.findall(r"`([A-Za-z0-9_.-]+)`", claimed.group(1)))
    assert named == omitted, (
        f"README says ci.yml omits {sorted(named)}; the targets `make verify` "
        f"composes that ci.yml never runs are {sorted(omitted)}"
    )


def test_ci_runs_no_make_target_verify_does_not_compose() -> None:
    # The other direction of "one Makefile is the gate": a target CI runs and
    # `verify` does not is a gate a contributor cannot reproduce locally.
    composed = set(_verify_prerequisites()) | _NOT_A_GATE
    ci_only = _make_targets(WORKFLOWS / "ci.yml") - composed
    assert not ci_only, (
        f"ci.yml runs make target(s) {sorted(ci_only)} that `make verify` does not "
        "compose, so `make verify` no longer reproduces the CI gate set"
    )


def test_readme_status_line_matches_the_roadmap_snapshot() -> None:
    readme_milestone = re.search(r"\*\*Status:\*\* pre-alpha · `(M[0-9]+)` in progress", _readme())
    roadmap_milestone = re.search(
        r"Repo status: \*\*Pre-alpha, (M[0-9]+) in progress\.\*\*",
        ROADMAP.read_text(encoding="utf-8"),
    )
    assert readme_milestone is not None, "README's status line changed shape"
    assert roadmap_milestone is not None, "ROADMAP §1's snapshot line changed shape"
    assert readme_milestone.group(1) == roadmap_milestone.group(1)


def test_readme_status_block_delegates_feature_status_instead_of_listing_it() -> None:
    # The stale claim this replaces was a per-feature enumeration in the status
    # line. docs/ROADMAP.md owns feature status; the README points at it. A
    # bare feature id reappearing here means the second copy is back.
    text = _readme()
    start = text.index("**Status:**")
    block = text[start : text.index("## Quickstart", start)]
    assert "docs/ROADMAP.md" in block, "README's status block stopped pointing at the roadmap"
    assert not re.findall(r"\bF[0-9]+\b", block), (
        "README's status block enumerates feature ids again; feature status "
        "lives in docs/ROADMAP.md and CHANGELOG.md"
    )


def test_readme_architecture_tree_lists_every_top_level_module() -> None:
    package = REPO / "src" / "encore"
    actual = {path.name for path in package.glob("*.py") if path.name != "__init__.py"} | {
        path.name for path in package.iterdir() if (path / "__init__.py").is_file()
    }
    listed = {
        entry.rstrip("/") for entry in re.findall(r"^│\s+[├└]── (\S+)", _readme(), re.MULTILINE)
    }
    assert listed == actual, (
        f"README's architecture tree lists {sorted(listed)}; src/encore holds {sorted(actual)}"
    )


def _pull_request_template() -> str:
    return PR_TEMPLATE.read_text(encoding="utf-8")


def test_the_pull_request_template_does_not_claim_ci_runs_the_literal_command() -> None:
    """The claim #38 corrected in the README was standing in a second place.

    The template is a published surface: every contributor reads it, and it said
    `make verify` was "the same command CI runs" while `ci.yml` has never run the
    literal command. Same defect, same repository, one file over.

    Checked as a property rather than a string: the template may not describe
    `make verify` as what CI, or the pull request, runs. `release.yml` at the tag is
    the true statement and the template is free to make it.
    """
    template = " ".join(_pull_request_template().split())
    for overclaim in (
        "the same command CI runs",
        "the same command the CI runs",
        "the command CI runs",
    ):
        assert overclaim not in template, (
            f"the pull request template says {overclaim!r}; ci.yml does not run the "
            "literal `make verify`, only release.yml does"
        )


def test_the_pull_request_template_names_the_targets_ci_omits() -> None:
    """Whatever it says about the split has to be the real split.

    Derived from the Makefile and the workflow, so adding a target to `ci.yml`
    fails here rather than leaving the template quietly overstating the gap.
    """
    omitted = {
        target
        for target in _verify_prerequisites()
        if target not in _make_targets(WORKFLOWS / "ci.yml")
    }
    template = " ".join(_pull_request_template().split())
    named = set(re.findall(r"`([a-z0-9-]+)`", template.split("omits", 1)[-1]))
    assert omitted <= named, (
        f"the pull request template does not name {sorted(omitted - named)}, which "
        "`make verify` composes and `ci.yml` never runs"
    )
