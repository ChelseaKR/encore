#!/usr/bin/env bash
# Apply branch protection to main. Idempotent.
#
# Requires: gh CLI authenticated; admin on the repo. `encore` is public as of the
# M4 flip, so classic branch protection is free here; the 403 this header used to
# warn about applied to a private repo on the Free plan and no longer does.
#
#   ./scripts/setup-branch-protection.sh
#
# Running this changes a live repository setting. It is the maintainer's call, not
# a pull request's, and nothing in CI runs it.
#
# ---------------------------------------------------------------------------
# Read this before running it. Every value below is load-bearing, and four of
# them used to be wrong in ways that only show up after the PUT succeeds.
#
# `enforce_admins` is `false`. It used to be `true`, which is the classic
# branch-protection equivalent of a ruleset with an empty `bypass_actors`: it
# removes the only way back in that does not go through GitHub support. That is
# not a stricter gate, it is a lockout. An agent applied a no-bypass ruleset
# elsewhere in this portfolio and restoring access took a sweep across eighteen
# repositories. On a single-maintainer repository, with required checks that can
# stop reporting for reasons nobody chose (a renamed job, a workflow that stops
# running on pull requests), the admin path is the break-glass path.
#
# `require_code_owner_reviews` is `false`. It used to be `true` alongside
# `required_approving_review_count: 0`, and those two together deadlock every
# merge here. CODEOWNERS routes `*` to @ChelseaKR, the sole maintainer, and
# GitHub does not count a self-approval; requiring a code-owner review therefore
# demands an approval that nobody in this repository can give. It would not have
# added a second reader. It would have made `main` unmergeable, and with
# `enforce_admins: true` above it, unmergeable with no way around.
#
# The required contexts are the check-run names GitHub actually reports, read off
# the workflow files rather than remembered. `tests/test_branch_protection.py`
# re-derives them from `.github/workflows/` and fails the build when this list
# and the workflows disagree, because a required context that matches nothing is
# a merge that never happens, and a renamed job is how that arrives quietly.
# Two contexts were wrong when that test was written:
#
#   - `Stage 5 — security (pip-audit · gitleaks)` no longer existed. The job had
#     been renamed to `Stage 5 — security (Semgrep · pip-audit · osv-scanner ·
#     gitleaks)` and this list was not updated with it.
#   - `analyze (python)` and `analyze (actions)` are produced by `codeql.yml`,
#     which triggers on `push` to `main`, `schedule` and `workflow_dispatch` and
#     NOT on `pull_request`. They never report on a pull request, so requiring
#     them blocks every merge forever.
#
# The CodeQL contexts are therefore not required here, which is a real reduction
# against P1-2's intent that a workflow-CodeQL finding should never be
# advisory-only. Requiring them needs `codeql.yml` to gain a `pull_request:`
# trigger first; then add them back to CHECKS in the same change, and the test
# will confirm they report. Recorded rather than quietly dropped. `workflow SAST
# (zizmor)` is required, so the zizmor half of P1-2 does block.
# ---------------------------------------------------------------------------
set -euo pipefail

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
# Required CI checks. These are check-run names, not job ids: a job reports under
# its `name:` when it has one, and a matrix job reports once per combination.
# `ci.yml`'s gate matrix is `["3.12"]` on a pull request, which is why py3.12 is
# the only one listed. Keep in step with `.github/workflows/`; the test named
# above fails if you do not.
CHECKS='[
  {"context":"format · lint · type · test (py3.12)"},
  {"context":"Stage 5 — security (Semgrep · pip-audit · osv-scanner · gitleaks)"},
  {"context":"Stage 9 — build (container)"},
  {"context":"workflow SAST (zizmor)"}
]'

protect() {
  local branch="$1"
  echo "Protecting ${REPO}@${branch} …"
  gh api -X PUT "repos/${REPO}/branches/${branch}/protection" \
    --input - <<JSON
{
  "required_status_checks": { "strict": true, "checks": ${CHECKS} },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true,
    "require_last_push_approval": false,
    "require_code_owner_reviews": false
  },
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": true,
  "restrictions": null
}
JSON
}

protect main
echo "Done. (Set required_approving_review_count to 1, and require_code_owner_reviews"
echo "to true, once there is a second maintainer who can actually give the approval.)"
echo
echo "Then confirm the admin path survived, because a PUT that lands every rule and"
echo "sets enforce_admins returns 200 like any other:"
echo "  gh api repos/${REPO}/branches/main/protection --jq .enforce_admins.enabled"
echo "must read false."
