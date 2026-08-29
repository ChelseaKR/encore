# 10. Branch protection: accepted as advisory-only until the repo leaves the free/private tier

## Status

Accepted 2026-07-05. **Its premise expired on the M4 flip, and its central factual
claim is now wrong.** See "Correction, 2026-08-29" at the end. Not superseded, because
the decision it records is still the decision in force: nothing mechanical blocks a
merge here. The reason has changed, and the script it points at was a lockout.

## Context

`main` has no branch protection. This was verified directly against the GitHub
API (`gh api repos/ChelseaKR/encore/branches/main/protection` → 404 "Branch not
protected") during the 2026-07-05 conformance audit, and is structural, not an
oversight: `scripts/setup-branch-protection.sh`'s own header explains that the
classic branch-protection API returns 403 for a private repository on GitHub's
free plan. Every CI check, CODEOWNERS routing, and the DoD/PR-template
checklist is therefore **advisory** right now — nothing on GitHub actually
stops a direct push or a self-merge to `main`, even though `README.md` and
`CONTRIBUTING.md`'s language ("A change merges when the full gate is green")
reads as if a gate enforces that.

Three ways to close this gap were on the table (audit's P0-3):

- **(a) Flip the repo public early.** `docs/ROADMAP.md` §4/§8 defers the
  public/private flip to M4, pending a trademark sweep on the name "encore"
  (`../encore-plans/08-risks-and-counters.md` R5). Flipping now would unblock
  branch protection, Scorecard, and GitHub private-vulnerability-reporting in
  one move, but it is a one-way, live, and irreversible-in-spirit action (once
  public, the trademark and any-prior-art exposure the sweep exists to catch
  can't be un-seen by anyone who noticed in the meantime) and it is not this
  ADR's decision to make — it's the maintainer's, tied to the trademark
  sweep's result.
- **(b) Upgrade to a paid GitHub plan (Pro/Team).** Unblocks protection while
  staying private. A recurring-cost decision, also not this ADR's to make.
- **(c) Accept the gap, document it, and rely on self-imposed discipline until
  (a) or (b) happens.** No live GitHub setting changes. Fully reversible:
  superseded the moment either alternative is chosen.

## Decision

**(c).** Encore accepts advisory-only merge gates until the repo leaves the
free/private tier, and documents that state here instead of letting
`README.md`/`CONTRIBUTING.md` imply an enforcement that isn't real. This is the
safest default precisely because it requires no live, hard-to-reverse action —
flipping repo visibility or changing billing are decisions the maintainer
makes deliberately, not a side effect of a conformance remediation pass.

Self-imposed discipline in the meantime (since nothing mechanical enforces
these yet):

- Treat `make verify` green as a hard personal rule before merging to `main`,
  not a suggestion — the CODEOWNERS routing and PR template exist to make the
  high-stakes paths (secrets, Plex read-only guarantee, matching thresholds)
  visually loud in review even though GitHub can't yet force that review.
- No direct pushes to `main` outside of the M0 scaffold commit that predates
  this ADR; open a PR against `main` even as a sole contributor, so the habit
  and the tooling (PR template, CI checks-as-signal) are already correct the
  day protection turns on.
- `required_approving_review_count: 0` in `scripts/setup-branch-protection.sh`
  is a deliberate single-maintainer waiver, not an oversight — bump it to `1`
  in that script the day a second maintainer exists, alongside a note in that
  PR referencing this ADR.

## Consequences

- CQ-37/38/40/41/43 and CICD-11/13/14/15/16/18 remain **FAIL** on the
  mechanical audit until (a) or (b) resolves this ADR — that is expected and
  correct; a PASS here would itself be a misrepresented-conformance defect of
  exactly the kind this audit round was fixing elsewhere (P0-1).
- Scorecard (SEC-36/37/38, P1-6) and GitHub private-vulnerability-reporting
  (DOC-09) stay blocked on the same visibility decision — both are gated on
  option (a), not on this ADR directly.
- The day the maintainer picks (a) or (b): run
  `./scripts/setup-branch-protection.sh`, flip this ADR's Status to
  `Superseded by <new ADR number>`, and write the new ADR recording which
  option was chosen and why.

**⛔ Decision needed from the maintainer, not from this remediation pass:**
whether/when to pursue (a) the trademark sweep + public flip, or (b) a paid
plan. Recorded as open in `docs/ROADMAP.md` §8's M4 exit criteria.


## Correction, 2026-08-29

Two things this ADR asserts were true when it was written and are not true now, and one
thing it recommends would have wedged the repository.

**`encore` is public.** Option (a) happened. `gh api repos/ChelseaKR/encore --jq .visibility`
reads `public`. Every "blocked on the visibility decision" consequence below is unblocked,
including Scorecard and private-vulnerability-reporting, and the 403-on-a-private-free-repo
reasoning in the Context section no longer applies to anything.

**`main` is not unprotected.** This ADR says "`main` has no branch protection", verified
against `gh api repos/ChelseaKR/encore/branches/main/protection` returning 404. That call
still returns 404, and it is now the wrong question. Measured 2026-08-29:

| Question | Answer |
|---|---|
| `gh api repos/ChelseaKR/encore/rulesets` | one ruleset, id `20564798`-style entry: `18823573`, `protect-main`, `active` |
| its `created_at` | `2026-07-11T20:44:58.625-07:00`, six days after this ADR |
| its `updated_at` | `2026-08-28T16:35:13.097-07:00` |
| its rules | `deletion`, `non_fast_forward` |
| its `bypass_actors` | `[{ "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }]` |
| `current_user_can_bypass` | `"always"` |
| `gh api repos/ChelseaKR/encore/branches/main/protection` | 404, "Branch not protected" |

So `main` cannot be deleted or force-pushed, and the owner keeps a standing bypass. What
the ruleset does **not** carry is a `pull_request` rule or any required status check, so
the substantive claim in the Context section still holds for a narrower reason than the
one given: nothing on GitHub stops a direct push or a self-merge to `main`. The gates are
still advisory. The sentence "nothing on GitHub actually stops a direct push or a
self-merge" survives; "`main` has no branch protection" does not.

**`scripts/setup-branch-protection.sh` would have deadlocked `main`, with no way out.**
This ADR's Consequences section says to run it the day (a) or (b) resolves. (a) has
resolved. Running the script as it stood would have applied, in one PUT:

- `"enforce_admins": true`. The classic branch-protection equivalent of a ruleset with an
  empty `bypass_actors`. It is not a stricter gate, it is a lockout: an agent applied a
  no-bypass ruleset elsewhere in this portfolio and restoring access took a sweep across
  eighteen repositories. Now `false`. The ruleset above already keeps the owner's standing
  bypass, and this is the same posture in the older API.
- `"require_code_owner_reviews": true` alongside `"required_approving_review_count": 0`.
  `.github/CODEOWNERS` routes `*` to `@ChelseaKR`, and GitHub does not count a
  self-approval, so this demanded an approval nobody in this repository can give. Every
  merge would have deadlocked, and `enforce_admins: true` removed the way around. Now
  `false`, with the third bullet under "Self-imposed discipline" below extended to say
  when to turn it back on.
- Three required contexts that never report on a pull request.
  `Stage 5 — security (pip-audit · gitleaks)` no longer existed: the job had been renamed
  to `Stage 5 — security (Semgrep · pip-audit · osv-scanner · gitleaks)` and the script
  was not updated with it. `analyze (python)` and `analyze (actions)` come from
  `codeql.yml`, which triggers on `push` to `main`, `schedule` and `workflow_dispatch`,
  and not on `pull_request`. A required context that matches nothing looks exactly like a
  rule that passes, until a merge waits for it forever.

The rename is fixed. The two CodeQL contexts are removed from the required list, which is
a real reduction against P1-2's intent that a workflow-CodeQL finding should never be
advisory-only, and it is recorded here rather than quietly taken: requiring them needs
`codeql.yml` to gain a `pull_request:` trigger first. `workflow SAST (zizmor)` is still
required, so the zizmor half of P1-2 does block. `tests/test_branch_protection.py` now
re-derives the check-run names from `.github/workflows/` and fails the build when the
script and the workflows disagree, so the next rename cannot orphan a context quietly.

Nothing here changed a live setting. Every reading above is a GET.

**Still the maintainer's, not this correction's:** whether to apply branch protection at
all now that it is free, and whether to give `codeql.yml` a `pull_request` trigger so its
contexts can be required again.
