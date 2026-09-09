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
# The same row's "Current status" cell carried a measured percentage and a test
# count as well. An earlier pass left those ungated on the grounds that a gate
# going red on the committed text would put `main` red to make a point. That was
# right about the gate and wrong about the text: measured 2026-09-09 on
# `89e676d`, the cell read "95.85% over 172 tests" while `make cov` reported
# 92.78% branch coverage over 694 collected tests. The number was not merely
# undrived, it was false, and stale *upward* — the direction that least looks
# like it needs attention.
#
# #75 puts two options to the maintainer: keep live values (which needs a writer
# that runs the suite, in the shape of `mrf-honest/tools/publish_metrics.py`), or
# state what the gate is and stop restating a measurement. This repository has
# already measured what the first option costs: a figure that moves on every
# commit, living on one line of tracked prose, is exactly the collision that put
# `main` red in #73 — and coverage churns far more often than a test-module
# count. So the cell now names the enforcement and publishes no measurement, and
# these gates hold it to that: no percentage, no test count, and the two
# mechanisms it names must be the two the build actually runs.
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


#: A percentage written in prose: `93%`, `92.78 %`. The target column is a
#: separate cell and is derived, so this pattern is only ever applied to the
#: status cell.
_PERCENTAGE = re.compile(r"\d+(?:\.\d+)?\s*%")

#: A test count: "172 tests", "over 694 tests", "694 collected tests".
_TEST_COUNT = re.compile(r"\d+\s+(?:\w+\s+)?tests?\b", re.IGNORECASE)

#: The two places the build states the floor. `_declared_coverage_floor` has
#: already proved both exist and agree; the status cell has to name both, so a
#: reader is sent to the mechanism rather than to a number.
_ENFORCEMENT_TOKENS = ("fail_under", "--cov-fail-under")


def test_the_coverage_row_publishes_no_measurement_nothing_recomputes() -> None:
    """§7's branch-coverage status cell states the gate, and states no figure.

    Two halves, and the second is what stops the cell being gutted to the word
    "Met":

    * **No measurement.** A percentage or a test count in this cell is a number
      a human typed and nothing re-derives. The cell carried one for months and
      it was wrong in both figures by the time anyone measured.
    * **The enforcement, named, and real.** The cell must name `fail_under` and
      `--cov-fail-under`, and `_declared_coverage_floor` has already required
      that both exist in `pyproject.toml` and the Makefile and agree — so this
      is a relation between the document and the build, not a spelling check.

    If a later change decides §7 *should* carry live values after all, the
    honest way in is a writer that runs the suite and a `--check` half that is a
    gate (#75), not a number retyped into this row: delete this test in the same
    commit that adds the writer, so the trade is visible in one diff.
    """
    floor = _declared_coverage_floor()
    status = _roadmap_row(_COVERAGE_METRIC)[-1]

    percentage = _PERCENTAGE.search(status)
    assert percentage is None, (
        f"docs/ROADMAP.md §7's branch-coverage status cell publishes {percentage.group(0)!r}. "
        f"Nothing in this repository recomputes a coverage percentage into that cell, so it "
        f"is stale from the commit after the one that measured it. The floor is in the target "
        f"column, derived from the build's own ≥{floor}%."
    )

    count = _TEST_COUNT.search(status)
    assert count is None, (
        f"docs/ROADMAP.md §7's branch-coverage status cell publishes {count.group(0)!r}. "
        f"A test count moves with every added module and nothing recomputes it here; "
        f"`docs/DOCUMENTATION-AUDIT.md` is the generated inventory that does."
    )

    missing = [token for token in _ENFORCEMENT_TOKENS if token not in status]
    assert not missing, (
        f"docs/ROADMAP.md §7's branch-coverage status cell no longer names {missing}. "
        f"Removing the measurement is only honest if the cell says what enforces the floor "
        f"instead; a bare 'Met' is a verdict with no mechanism behind it."
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

    # Everything the push step does *before* it publishes. A check made after
    # `docker push` cannot stop an unscanned image from reaching the registry.
    before_push = steps[push].split("docker push", 1)[0]

    # Deliberately not `"SCANNED_IMAGE_ID" in before_push`. That weaker form was
    # written first and a negative control walked straight through it: deleting
    # the comparison left a `test -n "${SCANNED_IMAGE_ID:-}"` presence guard
    # behind, the substring still matched, and the suite stayed green over a
    # step that would happily publish a substituted image.
    comparisons = [
        (match.group("lhs"), match.group("rhs"))
        for match in re.finditer(
            r'test\s+"\$\{(?P<lhs>\w+)\}"\s*=\s*"\$\{(?P<rhs>\w+)\}"', before_push
        )
    ]
    named = [pair for pair in comparisons if "SCANNED_IMAGE_ID" in pair]
    assert named, (
        "release.yml's push step never compares anything against SCANNED_IMAGE_ID before "
        "`docker push`. Mentioning the variable — a `test -n` guard, a comment — does not "
        "stop an unscanned image from being published; the id the tag resolves to now has "
        "to be compared against the id the CVE gate passed."
    )
    others = [name for pair in named for name in pair if name != "SCANNED_IMAGE_ID"]
    assert others, (
        "release.yml's push step compares SCANNED_IMAGE_ID against itself. That holds for "
        "every image, scanned or not."
    )
    other = others[0]
    assignment = re.search(rf'{other}="\$\((?P<command>[^)]*)\)"', before_push)
    assert assignment is not None and "docker image inspect" in assignment.group("command"), (
        f"release.yml's push step compares SCANNED_IMAGE_ID against {other!r}, which is not "
        f"read from `docker image inspect` in the same step. The comparison only means "
        f"something against the id the tag resolves to at push time."
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


# --- Where CodeQL findings go, and what still stops a merge -------------------
#
# `codeql.yml` was written while this repository was private, and carried
# `upload: never` with the comment "no GHAS on this private repo". The repo is
# public now (ADR 0010's 2026-08-29 correction records the flip) and code
# scanning is free on public repositories, so that setting was no longer a
# constraint being respected — it was every analysis being written to a runner
# and deleted with it. No alert, no history, no dismissal record.
#
# The correction has two halves and only one of them is obvious. Turning upload
# on is the obvious half. The half worth gating is that upload must not be
# mistaken for the gate: code scanning *records* a finding, it does not stop a
# merge. If the in-run failure step is ever dropped "because we have alerts
# now", CodeQL becomes advisory and nothing says so.

CODEQL_WORKFLOW = WORKFLOWS / "codeql.yml"
_CODEQL_METRIC = "CodeQL"


def _codeql_analyze_job() -> dict[str, object]:
    job = _jobs(CODEQL_WORKFLOW).get("analyze")
    assert job is not None, (
        "codeql.yml has no 'analyze' job. It was not deleted by this gate; if the job "
        "was renamed, re-point the gate rather than removing it."
    )
    return job


def _codeql_upload_setting() -> str:
    """`upload:` as the workflow actually sets it, defaulting the way the action does."""
    steps = _codeql_analyze_job().get("steps") or []
    assert isinstance(steps, list)
    for step in steps:
        assert isinstance(step, dict)
        uses = step.get("uses")
        if isinstance(uses, str) and "codeql-action/analyze" in uses:
            with_block = step.get("with") or {}
            assert isinstance(with_block, dict)
            # The action's own default is `always`; an omitted key means uploading.
            return str(with_block.get("upload", "always"))
    pytest.fail("codeql.yml's analyze job no longer runs codeql-action/analyze")


def test_a_codeql_finding_still_fails_the_run() -> None:
    """An alert in a tab is not a gate.

    Uploading SARIF gives a finding somewhere to live. It does not fail a build,
    so it cannot replace the step that does. This asserts the failing step is
    still there and still reads the analysis rather than something else.
    """
    steps = _job_run_steps(CODEQL_WORKFLOW, "analyze")
    gating = [run for run in steps if "codeql-results" in run and "exit 1" in run and "jq" in run]
    assert gating, (
        "codeql.yml no longer has a step that reads the SARIF and exits non-zero on a "
        "finding. Code scanning alerts do not fail a build, so without this step a "
        "CodeQL finding is advisory and nothing in the repository says so."
    )


def test_a_missing_analysis_cannot_read_as_a_clean_one() -> None:
    """Absence rendered as a value, in the one place it would be silent.

    `jq -s` with no file arguments reads stdin and answers 0. The gating step
    globs for `codeql-results/*.sarif`, so under `nullglob` an analysis that
    never produced SARIF would score zero findings and pass. Measured.
    """
    steps = _job_run_steps(CODEQL_WORKFLOW, "analyze")
    candidates = [run for run in steps if "codeql-results" in run and "exit 1" in run]
    assert candidates, (
        "codeql.yml has no step that reads the SARIF and can exit non-zero; see "
        "test_a_codeql_finding_still_fails_the_run for what that costs."
    )
    gating = candidates[0]
    # `nullglob` off is a different, louder failure: the unmatched glob stays
    # literal and jq exits 2. Only the nullglob path can score absence as zero.
    if "nullglob" in gating:
        assert re.search(r'\$\{#\w+\[@\]\}"?\s*-eq\s*0', gating), (
            "codeql.yml's findings gate enables `nullglob` without checking that the "
            "SARIF glob matched anything. An empty match leaves `jq -s` with no file "
            "arguments, and `jq -s` with no arguments reads stdin and answers 0 — a "
            "missing analysis scored as a clean one."
        )


def test_uploading_sarif_carries_the_permission_that_makes_it_possible() -> None:
    """`upload:` on without `security-events: write` is a 403 at the end of every run."""
    upload = _codeql_upload_setting()
    permissions = _codeql_analyze_job().get("permissions") or {}
    assert isinstance(permissions, dict)
    granted = permissions.get("security-events")
    if upload == "never":
        assert granted is None, (
            "codeql.yml grants `security-events: write` while `upload: never` means "
            "nothing is ever uploaded. A write permission nothing uses is scope for free."
        )
    else:
        assert granted == "write", (
            f"codeql.yml sets `upload: {upload}` but its analyze job has no "
            f"`security-events: write`, so every upload fails with a 403."
        )


def test_the_roadmap_says_where_codeql_findings_actually_go() -> None:
    """§7's CodeQL row, derived from the workflow rather than retyped.

    The row spent weeks saying SARIF upload was disabled because this was a
    private repository without GHAS. Both halves of that sentence had expired.
    Requiring the row to quote the workflow's own `upload:` value means the next
    change to the posture cannot land without the published claim moving with it.
    """
    upload = _codeql_upload_setting()
    status = _roadmap_row(_CODEQL_METRIC)[-1]
    # Anchored on "sets `upload: X`", not on the bare value. The first version of
    # this gate looked for "`upload: {upload}`" anywhere in the cell, and a
    # negative control walked through it: the row *narrates* the correction, so it
    # quotes the old `upload: never` alongside the new posture. Both values were
    # present, and the check passed whichever one the workflow held. A gate that
    # accepts every answer is the defect this row exists to describe.
    stated = re.search(r"sets `upload: (\w+)`", status)
    assert stated is not None, (
        "docs/ROADMAP.md §7's CodeQL row no longer says what `upload:` codeql.yml sets. "
        "The row is the published claim about where findings go, and it has to state the "
        "current posture in a form that cannot also match the posture it replaced."
    )
    assert stated.group(1) == upload, (
        f"docs/ROADMAP.md §7's CodeQL row says codeql.yml sets `upload: {stated.group(1)}`; "
        f"the workflow sets `upload: {upload}`."
    )
