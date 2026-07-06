#!/usr/bin/env bash
# Apply branch protection to main. Idempotent.
#
# Requires: gh CLI authenticated; admin on the repo; and either a PUBLIC repo
# (protection is free) or a paid plan (Pro/Team) for a PRIVATE repo. On a private
# Free-plan repo the API returns 403 — make the repo public, then re-run.
#
#   ./scripts/setup-branch-protection.sh
#
# No committed GitHub "Rulesets" artifact exists yet anywhere in the portfolio
# (CICD-12/18 name it as an aspiration); this is the legacy branch-protection
# API, the same pattern civic-rag-starter-kit uses. Revisit once a portfolio-wide
# rulesets convention exists.
set -euo pipefail

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
# Required CI checks (job names from .github/workflows/ci.yml + codeql.yml).
# P1-2 adds zizmor and the CodeQL `actions` language pack as required, blocking
# checks the day protection is applied — a workflow-SAST/workflow-CodeQL finding
# should never be advisory-only once there's a real merge gate to attach it to.
CHECKS='[
  {"context":"format · lint · type · test (py3.12)"},
  {"context":"Stage 5 — security (pip-audit · gitleaks)"},
  {"context":"Stage 9 — build (container)"},
  {"context":"workflow SAST (zizmor)"},
  {"context":"analyze (python)"},
  {"context":"analyze (actions)"}
]'

protect() {
  local branch="$1"
  echo "Protecting ${REPO}@${branch} …"
  gh api -X PUT "repos/${REPO}/branches/${branch}/protection" \
    --input - <<JSON
{
  "required_status_checks": { "strict": true, "checks": ${CHECKS} },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "dismiss_stale_reviews": true,
    "require_last_push_approval": false,
    "require_code_owner_reviews": true
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
echo "Done. (Set required_approving_review_count to 1 once there's a second maintainer.)"
