# 10. Branch protection: accepted as advisory-only until the repo leaves the free/private tier

## Status

Accepted

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
