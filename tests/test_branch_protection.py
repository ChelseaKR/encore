"""`scripts/setup-branch-protection.sh` must not be a lockout waiting to be run.

That script is not a description of a policy. It is the policy, executable, and ADR 0010
tells the maintainer to run it the day the repository leaves the free tier. It has left:
`encore` is public. Nothing read the script, so nothing in this repository could disagree
with it, and it had been wrong unopposed since it was written.

Four ways, each of which the GitHub API answers with a 200:

**`enforce_admins: true`.** The classic branch-protection equivalent of a ruleset with an
empty `bypass_actors`. It removes the only way back in that does not go through GitHub
support. An agent applied a no-bypass ruleset elsewhere in this portfolio and restoring
access took a sweep across eighteen repositories. On a single-maintainer repository, whose
required checks can stop reporting for reasons nobody chose, the admin path is the
break-glass path.

**`require_code_owner_reviews: true` with `required_approving_review_count: 0`.** CODEOWNERS
routes `*` to `@ChelseaKR`, the sole maintainer, and GitHub does not count a self-approval.
Requiring a code-owner review demands an approval nobody in this repository can give, so
every merge deadlocks, and `enforce_admins: true` above it removed the way around.

**A required context no job produces.** `Stage 5 — security (pip-audit · gitleaks)` was
required. The job had been renamed to `Stage 5 — security (Semgrep · pip-audit ·
osv-scanner · gitleaks)` and this list did not follow. A required context that matches
nothing looks exactly like a rule that passes, right up until a merge waits for it forever.

**A required context that never reports on a pull request.** `analyze (python)` and
`analyze (actions)` come from `codeql.yml`, which triggers on `push` to `main`, `schedule`
and `workflow_dispatch`, and not on `pull_request`. The jobs exist. They simply never run
where the gate is evaluated.

So the script would have deadlocked `main` three separate ways, with no admin bypass to
undo it, on a repository whose ADR says to run it.

Correcting it once is not the fix, because a job rename regresses it silently. This module
is the fix. Everything here is derived: the required contexts come out of the script, and
the check-run names come out of `.github/workflows/`, so the two are compared rather than
both being trusted. `lockout_risk` is a pure function of a parsed settings document and is
run against the documents it must reject as well as the committed one, with a positive
control so it cannot pass by refusing everything. A missing or unparseable script is a
failure rather than an empty default: a guard that passes when its subject is absent is
the defect it exists to catch, and parsing rather than grepping is what catches a
truncated file that still contains the words it is searched for.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup-branch-protection.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
FROM_JSON = re.compile(r"fromJSON\('(\[[^']*\])'\)")

CHECKS_ASSIGNMENT = re.compile(r"^CHECKS='(\[.*?\])'\s*$", re.DOTALL | re.MULTILINE)
PUT_BODY = re.compile(r"--input - <<JSON\n(.*?)\nJSON\n", re.DOTALL)


def _script_text() -> str:
    if not SCRIPT.is_file():
        pytest.fail(f"{SCRIPT} is missing; the apply script is what this module checks")
    return SCRIPT.read_text(encoding="utf-8")


def required_contexts() -> list[str]:
    """Read the contexts the script would require out of the script itself."""
    text = _script_text()
    found = CHECKS_ASSIGNMENT.search(text)
    if found is None:
        pytest.fail(
            f"{SCRIPT} no longer assigns CHECKS as a single-quoted JSON array, so this "
            "module cannot tell what it would require. Do not delete this check; update it."
        )
    # Bound before the try so a parse failure cannot leave the name unset; `pytest.fail`
    # raises, and "unreachable in practice" is the reasoning this module exists to
    # distrust. `None` is not a non-empty list either, so the next check still refuses.
    checks: Any = None
    try:
        checks = json.loads(found.group(1))
    except json.JSONDecodeError as exc:
        pytest.fail(f"CHECKS in {SCRIPT} is not parseable JSON: {exc}")
    if not isinstance(checks, list) or not checks:
        pytest.fail(f"CHECKS in {SCRIPT} is not a non-empty JSON array: {checks!r}")
    return [check["context"] for check in checks]


def protection_settings() -> dict[str, Any]:
    """Parse the settings body the script would PUT, rather than searching it.

    The `${CHECKS}` interpolation is substituted first, so the whole body is real JSON. A
    grep over this file would be satisfied by a truncated one; the parse is not.
    """
    text = _script_text()
    body = PUT_BODY.search(text)
    if body is None:
        pytest.fail(
            f"{SCRIPT} no longer PUTs a heredoc JSON body, so this module cannot tell "
            "what settings it would apply. Do not delete this check; update it."
        )
    checks = CHECKS_ASSIGNMENT.search(text)
    if checks is None:
        pytest.fail(f"{SCRIPT} no longer assigns CHECKS")
    # Same binding-before-the-try as above, for the same reason.
    loaded: Any = None
    try:
        loaded = json.loads(body.group(1).replace("${CHECKS}", checks.group(1)))
    except json.JSONDecodeError as exc:
        pytest.fail(f"the settings body in {SCRIPT} is not parseable JSON: {exc}")
    if not isinstance(loaded, dict):
        pytest.fail(f"the settings body in {SCRIPT} is not a JSON object: {loaded!r}")
    return loaded


def lockout_risk(settings: dict[str, Any]) -> str | None:
    """Say why applying these settings would wedge `main`, or ``None`` if they would not.

    A pure function of a parsed document, so it can be run against the documents it must
    reject rather than only against the one in the tree.
    """
    if settings.get("enforce_admins") is not False:
        return (
            "enforce_admins is not false, which leaves no break-glass path: the sole "
            "maintainer cannot merge past a check that has stopped reporting, cannot "
            "push, and cannot lift the protection that is blocking them"
        )
    reviews = settings.get("required_pull_request_reviews")
    if reviews is None:
        return None
    if not isinstance(reviews, dict):
        return f"required_pull_request_reviews is {type(reviews).__name__}, not an object"
    approvals = reviews.get("required_approving_review_count", 0)
    if reviews.get("require_code_owner_reviews") and approvals == 0:
        return (
            "require_code_owner_reviews is true while required_approving_review_count is "
            "0, and CODEOWNERS routes every path to the sole maintainer; GitHub does not "
            "count a self-approval, so this demands an approval nobody here can give"
        )
    if approvals and approvals > 0:
        return (
            f"required_approving_review_count is {approvals} on a single-maintainer "
            "repository, and GitHub does not count a self-approval, so no pull request "
            "can reach it"
        )
    return None


def _triggers(document: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML resolves the bare key `on` to the boolean True under YAML 1.1, which is why
    # this is not a plain document["on"], and why the key type here is not `str`.
    on = document.get(True, document.get("on"))
    if isinstance(on, dict):
        return on
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {key: {} for key in on}
    return {}


def _guaranteed_values(dimension: object, where: str) -> list[Any]:
    """Derive the values a matrix dimension takes on *every* run, not merely on some.

    A plain YAML list is every value. `ci.yml`'s gate is not a plain list:

        python: ${{ (github.event_name == 'pull_request') && fromJSON('["3.12"]')
                    || fromJSON('["3.12", "3.13"]') }}

    The expression evaluates to one of the `fromJSON` literals, and which one is not
    knowable from the file. Only a value present in *all* of them is guaranteed to be
    there, so the intersection is the honest answer, and it is the answer this module
    needs: a context can be required only if it reports on every pull request.

    An expression with no literals to read is refused rather than guessed at. Returning
    an empty list there would make the job's contexts look like phantoms; returning the
    base name would make them look guaranteed. Both are inventions, and a guard that
    invents is the kind of gate this repository keeps finding in itself.
    """
    if isinstance(dimension, list):
        return dimension
    if isinstance(dimension, str):
        literals = FROM_JSON.findall(dimension)
        if not literals:
            pytest.fail(
                f"{where} is the expression {dimension!r}, and this module cannot derive "
                "which values it takes. Extend it rather than deleting the check."
            )
        parsed = [json.loads(literal) for literal in literals]
        guaranteed = [value for value in parsed[0] if all(value in other for other in parsed[1:])]
        if not guaranteed:
            pytest.fail(
                f"{where}: no value appears in every branch of {dimension!r}, so no "
                "context from this job reports on every pull request"
            )
        return guaranteed
    pytest.fail(f"{where} is {type(dimension).__name__}, which is not a matrix dimension")


def _check_names(document: dict[str, Any], source: str) -> set[str]:
    """Derive every check-run name the jobs in ``document`` report on every run.

    A job reports under its ``name:`` if it has one, otherwise its id, and a matrix job
    reports once per combination. `${{ matrix.<key> }}` inside a name is substituted, which
    is how `analyze (python)` and `format · lint · type · test (py3.12)` are derived rather
    than guessed.
    """
    names: set[str] = set()
    for job_id, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        base = str(job.get("name", job_id))
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        if not isinstance(matrix, dict):
            names.add(base)
            continue
        keys = [key for key in matrix if key not in ("include", "exclude")]
        dimensions = [
            _guaranteed_values(matrix[key], f"{source}: job {job_id!r} matrix.{key}")
            for key in keys
        ]
        combinations: list[dict[str, Any]] = [
            dict(zip(keys, values, strict=True)) for values in itertools.product(*dimensions)
        ]
        combinations.extend(
            entry for entry in (matrix.get("include") or []) if isinstance(entry, dict)
        )
        if not combinations:
            names.add(base)
            continue
        for combination in combinations:
            if "${{" in base:
                values = combination

                def substitute(found: re.Match[str], values: dict[str, Any] = values) -> str:
                    return str(values.get(found.group(1), found.group(0)))

                names.add(re.sub(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", substitute, base))
            else:
                rendered = ", ".join(str(combination[key]) for key in keys)
                names.add(f"{base} ({rendered})" if rendered else base)
    return names


def checks_reported_on_every_pull_request() -> set[str]:
    """Collect the check-run names produced by a workflow that runs on every pull request.

    A workflow with no `pull_request` trigger never reports where the gate is evaluated,
    and one whose trigger carries `paths` or `paths-ignore` does not report on a pull
    request that touches nothing it filters on. Neither kind can be required.
    """
    names: set[str] = set()
    for workflow in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        triggers = _triggers(document)
        if "pull_request" not in triggers:
            continue
        settings = triggers.get("pull_request") or {}
        if isinstance(settings, dict) and {"paths", "paths-ignore"} & set(settings):
            continue
        names |= _check_names(document, workflow.name)
    return names


def test_applying_the_script_would_not_lock_the_owner_out() -> None:
    """The assertion `enforce_admins: true` has to fail."""
    risk = lockout_risk(protection_settings())
    assert risk is None, (
        f"running scripts/setup-branch-protection.sh as committed would wedge main: {risk}"
    )


def test_every_required_context_is_produced_on_every_pull_request() -> None:
    """A required check that never reports blocks the merge forever."""
    produced = checks_reported_on_every_pull_request()
    phantom = sorted(set(required_contexts()) - produced)
    assert not phantom, (
        f"scripts/setup-branch-protection.sh would require {phantom}, which no workflow "
        f"job reports on every pull request. Produced on every pull request: "
        f"{sorted(produced)}. A required context that matches nothing looks exactly like "
        "a rule that passes."
    )


def test_the_codeowners_catch_all_is_the_sole_maintainer() -> None:
    """The premise `require_code_owner_reviews: false` rests on.

    If a second owner ever appears, that setting should be reconsidered, and this is where
    the reconsideration gets forced.
    """
    owners = {
        token
        for line in CODEOWNERS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for token in line.split()[1:]
    }
    assert owners == {"@ChelseaKR"}, (
        f"CODEOWNERS now names {sorted(owners)}. `require_code_owner_reviews: false` in "
        "scripts/setup-branch-protection.sh is justified only while the sole code owner "
        "is also the sole approver; revisit it and update this test together."
    )


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({"enforce_admins": True}, "enforce_admins is not false"),
        ({}, "enforce_admins is not false"),
        ({"enforce_admins": "false"}, "enforce_admins is not false"),
        (
            {
                "enforce_admins": False,
                "required_pull_request_reviews": {
                    "required_approving_review_count": 0,
                    "require_code_owner_reviews": True,
                },
            },
            "require_code_owner_reviews is true",
        ),
        (
            {
                "enforce_admins": False,
                "required_pull_request_reviews": {"required_approving_review_count": 1},
            },
            "required_approving_review_count is 1",
        ),
    ],
    ids=["admins-enforced", "absent", "string-false", "code-owner-deadlock", "self-approval"],
)
def test_the_lockout_check_rejects_the_documents_it_must_reject(
    settings: dict[str, Any], expected: str
) -> None:
    """Five ways to wedge `main`, each of which GitHub answers with a 200.

    `enforce_admins: true` and the code-owner deadlock are the two that were committed.
    The absent key and the string `"false"` are the shapes an edit to fix them could
    plausibly land in, and `"false"` is the nastier one: it is truthy in JSON.
    """
    risk = lockout_risk(settings)
    assert risk is not None, f"{settings} should be refused"
    assert expected in risk


def test_the_lockout_check_accepts_the_shape_it_should() -> None:
    """A positive control, so the check above is not passing by refusing everything."""
    assert (
        lockout_risk(
            {
                "enforce_admins": False,
                "required_pull_request_reviews": {
                    "required_approving_review_count": 0,
                    "require_code_owner_reviews": False,
                },
            }
        )
        is None
    )


def test_the_context_check_rejects_a_context_no_workflow_produces() -> None:
    """A positive control for the other direction: the phantom check must be able to fire.

    The rename that orphaned `Stage 5 — security (pip-audit · gitleaks)` is replayed here
    against the real workflows, so this test fails if the derivation above ever stops
    seeing the difference between a name that exists and one that does not.
    """
    produced = checks_reported_on_every_pull_request()
    assert "Stage 5 — security (pip-audit · gitleaks)" not in produced
    assert "Stage 5 — security (Semgrep · pip-audit · osv-scanner · gitleaks)" in produced


def test_codeql_contexts_are_absent_for_the_reason_recorded() -> None:
    """`analyze (…)` is not required, and the script says why. Keep those two in step.

    If `codeql.yml` gains a `pull_request` trigger, this test fails and the required list
    should gain the contexts back, which is the intended prompt rather than a nuisance.
    """
    codeql = yaml.safe_load((WORKFLOWS / "codeql.yml").read_text(encoding="utf-8"))
    on_pull_request = "pull_request" in _triggers(codeql)
    required = set(required_contexts())
    codeql_required = {name for name in required if name.startswith("analyze (")}
    if on_pull_request:
        assert codeql_required, (
            "codeql.yml now runs on pull_request, so its contexts report on every pull "
            "request and can be required. Add them back to CHECKS in "
            "scripts/setup-branch-protection.sh; P1-2 asked for them."
        )
    else:
        assert not codeql_required, (
            f"{sorted(codeql_required)} are required but codeql.yml has no pull_request "
            "trigger, so they never report where the gate is evaluated"
        )
