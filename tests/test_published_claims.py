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


# ---------------------------------------------------------------------------
# docs/ROADMAP.md §7 (issue #75)
# ---------------------------------------------------------------------------
#
# §7 publishes figures about this repository and nothing derived any of them.
# The `Branch coverage` row's TARGET column is the half that is mechanical and
# safe to gate: it restates a floor that already lives, twice, in the build.
#
# The same row's "Current status" cell also carries a measured percentage and a
# test count. Those are NOT gated here, deliberately — re-deriving them changes
# a number this project publishes about itself, and #75 leaves that call to the
# maintainer. Adding a gate that goes red on today's committed text would put
# `main` red to make a point.
#
# Written as a TWO-STEP check, which is the shape that survives the merge
# collapse this repository measured in production (#73): resolve what the
# sources say *live*, require the sources to agree with each other, and only
# then require the document to contain it. A one-step "the doc says 85" check
# is a literal with extra steps.

PYPROJECT = REPO / "pyproject.toml"

#: The `Metric` cell whose `Target` column this gate derives.
_COVERAGE_METRIC = "Branch coverage"


def _declared_coverage_floor() -> int:
    """Return the branch-coverage floor, resolved from every place the build states it.

    Two sources, and they must agree: `pyproject.toml`'s `fail_under` (what a
    bare `coverage report` enforces) and `make cov`'s `--cov-fail-under` (what
    the gate CI runs enforces). A repository whose two floors disagree is one
    where the published figure is right about one of them and wrong about the
    other, and no document can be correct about both.
    """
    pyproject = re.search(
        r"^fail_under\s*=\s*(\d+)", PYPROJECT.read_text(encoding="utf-8"), flags=re.MULTILINE
    )
    makefile = re.search(r"--cov-fail-under=(\d+)", MAKEFILE.read_text(encoding="utf-8"))
    assert pyproject is not None, "pyproject.toml no longer declares `fail_under`"
    assert makefile is not None, "the Makefile no longer passes `--cov-fail-under`"
    assert pyproject.group(1) == makefile.group(1), (
        f"pyproject.toml declares a {pyproject.group(1)}% coverage floor and `make cov` "
        f"enforces {makefile.group(1)}%. Published documentation cannot be correct about both."
    )
    return int(pyproject.group(1))


def _roadmap_row(metric: str) -> list[str]:
    """Return the §7 table row for one metric, split into its cells."""
    for line in ROADMAP.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"| {metric} |"):
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError(
        f"docs/ROADMAP.md §7 no longer has a {metric!r} row. It was not deleted by this "
        f"gate; if the row moved, re-point the gate rather than removing it."
    )


def test_the_roadmap_coverage_target_is_the_floor_the_build_actually_enforces() -> None:
    """§7's target column, derived from the build rather than retyped.

    Step one resolves the floor from `pyproject.toml` and the Makefile and makes
    them agree. Step two requires the published row to state that number. Change
    the floor in either place without touching the roadmap and this goes red.
    """
    floor = _declared_coverage_floor()
    cells = _roadmap_row(_COVERAGE_METRIC)
    target = cells[2]
    assert target == f"≥{floor}%", (
        f"docs/ROADMAP.md §7 publishes a branch-coverage target of {target!r} while the "
        f"build enforces ≥{floor}%. The document restates a number the build owns."
    )


# --- The release workflow's claim about itself (issue #50) -------------------
#
# The container half of `release.yml` built `ghcr.io/chelseakr/encore:${TAG}`,
# CVE-scanned it, and ended. There was no `docker login`, no `docker push` and
# no `packages: write` anywhere in the file, so the image was built on the
# runner and discarded — and M4's "v0.1.0 published to GHCR" exit criterion was
# not reachable by running the workflow named for it. The run would go green
# and publish nothing, which is the worst way for this to fail: a green release
# reads as a met criterion.
#
# Nothing could have caught that, because no gate read the workflow. These do.

RELEASE_WORKFLOW = WORKFLOWS / "release.yml"
_PUBLISHING_JOB = "build-sign-publish"


def _jobs(workflow: Path) -> dict[str, dict[str, object]]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    assert isinstance(jobs, dict) and jobs, f"{workflow.name} declares no jobs"
    return jobs


def _job_run_steps(workflow: Path, job_name: str) -> list[str]:
    """Every `run:` block of one job, in the order the job runs them."""
    job = _jobs(workflow).get(job_name)
    assert job is not None, (
        f"{workflow.name} has no {job_name!r} job. It was not deleted by this gate; "
        f"if the job was renamed, re-point the gate rather than removing it."
    )
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return [step["run"] for step in steps if isinstance(step.get("run"), str)]


def _index_of(steps: list[str], needle: str) -> int:
    for index, run in enumerate(steps):
        if needle in run:
            return index
    pytest.fail(f"no release step runs {needle!r}; steps were: {steps}")


def test_the_release_workflow_actually_pushes_the_image_to_ghcr() -> None:
    """A workflow that builds an image and never pushes it publishes nothing.

    M4's first exit criterion is "v0.1.0 published to GHCR". Building the tag
    locally and scanning it satisfies neither half of that sentence.
    """
    steps = _job_run_steps(RELEASE_WORKFLOW, _PUBLISHING_JOB)
    joined = "\n".join(steps)
    assert "docker push" in joined, (
        "release.yml builds ghcr.io/chelseakr/encore:${TAG} and never pushes it. "
        "The image is discarded when the runner ends, so the release publishes "
        "nothing to GHCR while reporting success (issue #50)."
    )
    assert "docker login ghcr.io" in joined, (
        "release.yml pushes to GHCR without authenticating to it."
    )


def test_the_pushed_image_is_the_one_that_was_scanned() -> None:
    """Ordering, not presence: the CVE gate must precede the publish.

    A push before the Trivy scan would make the scan advisory — the image
    users pull would already exist by the time the gate had an opinion. A
    second `docker build` between them would mean the scanned bytes and the
    published bytes are merely two builds of the same Dockerfile.
    """
    steps = _job_run_steps(RELEASE_WORKFLOW, _PUBLISHING_JOB)
    build = _index_of(steps, "docker build")
    scan = _index_of(steps, "trivy image")
    push = _index_of(steps, "docker push")
    assert build < scan < push, (
        f"release.yml runs build={build}, scan={scan}, push={push}. The scan has to sit "
        f"between them or it does not gate what is published."
    )
    between = steps[scan + 1 : push + 1]
    assert not any("docker build" in run for run in between), (
        "release.yml rebuilds the image between the CVE scan and the push, so the "
        "scanned image is not the published one."
    )


def test_the_publish_step_checks_at_runtime_that_it_is_pushing_the_scanned_image() -> None:
    """Static ordering is an arrangement; this is the assertion that enforces it.

    The ordering test above reads the workflow file. It cannot see a step that
    re-tags the image at run time, and it is satisfied by any file whose steps
    happen to be in the right sequence. So the scan step records the local id of
    the image it passed, and the push step refuses to publish anything else.

    The check has to *span* the two steps to mean anything. An earlier draft
    captured the id inside the push step, immediately before `docker push`, and
    compared it immediately after — but `docker push` does not change a local
    image id, so that comparison could not fail under any circumstance. It read
    as provenance and asserted nothing. This gate pins the shape that does work:
    recorded by the scan, consumed by the push.
    """
    steps = _job_run_steps(RELEASE_WORKFLOW, _PUBLISHING_JOB)
    scan = _index_of(steps, "trivy image")
    push = _index_of(steps, "docker push")
    assert "SCANNED_IMAGE_ID=" in steps[scan] and "GITHUB_ENV" in steps[scan], (
        "release.yml's CVE scan step does not export SCANNED_IMAGE_ID, so the push step "
        "has nothing to compare against and cannot tell a scanned image from any other."
    )
    before_push = steps[push].split("docker push", 1)[0]
    assert "SCANNED_IMAGE_ID" in before_push, (
        "release.yml's push step does not check SCANNED_IMAGE_ID before pushing. A check "
        "made after the push cannot stop an unscanned image from being published."
    )


def test_only_the_publishing_job_may_write_packages() -> None:
    """`packages: write` is scoped to the one job that needs it."""
    jobs = _jobs(RELEASE_WORKFLOW)
    publishing = jobs[_PUBLISHING_JOB]
    permissions = publishing.get("permissions") or {}
    assert isinstance(permissions, dict)
    assert permissions.get("packages") == "write", (
        "release.yml's publishing job cannot push to GHCR: it has no `packages: write`. "
        "The push step would fail with a 403 at the very end of a release run."
    )
    for name, job in jobs.items():
        if name == _PUBLISHING_JOB:
            continue
        other = job.get("permissions") or {}
        assert isinstance(other, dict)
        assert other.get("packages") is None, (
            f"release.yml's {name!r} job also holds a `packages` permission. Only the "
            f"job that pushes the image should be able to write packages."
        )
