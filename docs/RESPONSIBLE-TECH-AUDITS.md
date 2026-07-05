# Responsible-Tech Audits — encore

Instantiates the portfolio's private `RESPONSIBLE-TECH-FRAMEWORK.md` (fetched at CI
time, never committed here). The interesting finding behind this document: a music
*taste* product is quietly a *sensitive-inference* product — what someone listens to
can reveal religion, politics, sexuality, or mental state as reliably as more
obviously sensitive data, and Encore is designed to treat it that way from the start
rather than discovering it after a leak. Source planning material:
`../encore-plans/06-privacy-responsible-tech.md`.

---

## A. Ethics & responsibility

- **Worst misuse:** using Encore to surveil music consumption on a Plex server that
  isn't the operator's own — a household member, a partner, a roommate — turning a
  release-radar tool into a taste-monitoring tool.
- **Mitigations:** single-admin auth on by default (F6); no silent multi-library
  aggregation; no "watch someone else's library" feature exists or is planned; the
  non-goals in `README.md` publish this boundary.
- **"Works as intended" harm:** even used exactly as intended by its owner, a shared
  Discord notification channel or a shared household calendar (iCal feed) can out
  someone's taste in music to people they didn't choose to share it with. This is the
  no-outing lens applied to music (see §C, §threat model).
- **Non-goals:** no acquisition features, ever (`README.md`); no analytics or
  engagement-optimization of any kind. **Auto-gated:** no-outing/no-secrets-in-logs
  test suite (from M1). **Review-gated:** any new sharing/export surface gets a
  CODEOWNER review against this section before merge.
- **Kill-switch:** stop the container. All data stays on the user's own disk; there
  is no remote component to keep running.
- **Accountable owner:** Chelsea Kelly-Reif (RTF-01).

## B. Bias & fairness

- **Segments:** none — Encore has no user segments; it operates on one person's (or
  household's) music library.
- **Risks:** the real risk is not classic demographic bias but **identity
  inference from taste** — genre, artist, or listening-pattern data being used
  (by Encore or a downstream integration) to infer something about the listener
  they did not choose to disclose. Explicitly out of bounds.
- **Tests:** a no-identity-inference guard (grep/AST check) asserting no code path
  derives a demographic or identity attribute from taste data. Recommendations are
  collaborative-overlap only (F7), always shown with visible provenance ("similar to
  X, Y you own") rather than an opaque score, so the *reasoning* is inspectable, not
  just the output.
- **Commitment:** recommendations never claim to know who you are, only what
  correlates with what you already have. **Auto-gated:** the identity-inference guard
  test (from M3, when F7 lands). **Review-gated:** any feature proposal that scores
  or segments users by inferred traits is rejected at design time, not fixed post-hoc.

## C. Privacy & data-protection

- **Data inventory:** see the DPIA table below (`docs/audits/dpia.md` will hold the
  full regenerated version once schema exists; the table here is the M0 source of
  truth).
- **Handling:** local-first — everything lives in Encore's own SQLite file. Outbound
  calls are purpose-bound: artist names/MBIDs to MusicBrainz/ListenBrainz (matching,
  watching, recommendations), and whatever the user configures to their own
  notification channels. No telemetry, no analytics, no accounts, no phone-home,
  ever — there is no server-side component to send data to.
- **Commitment:** the privacy notice states the MetaBrainz and notification-channel
  egress plainly, as disclosure choices, rather than implying "local-first" means
  "zero egress." **Auto-gated:** the sentinel-artist tripwire and no-exfiltration
  test (outbound-request allowlist) — from M1. **Review-gated:** any new outbound
  integration (F11 ListenBrainz account linking, F12 Jellyfin/Navidrome, F14 if it
  ever ships) requires a DPIA update before merge.

### Data inventory

| Data | Why held | Where | Retention | Sensitivity |
|---|---|---|---|---|
| Plex base URL + token | library sync (F1) | SQLite, encrypted at rest (`docs/adr/0008`) | until user removes | **High** — grants full Plex access |
| Artist inventory + play counts | matching, weighting (F2, F9) | SQLite | mirror of Plex; tombstoned on removal | Medium — taste data, inference-rich |
| MBID match table | release watching (F3) | SQLite | permanent cache | Low |
| Notification channel URLs (Apprise) | delivery (F4) | SQLite, encrypted at rest | until user removes | **High** — many Apprise URLs embed credentials |
| Feed tokens (RSS/iCal) | F5 auth | SQLite | rotatable | Medium — feed contents reveal taste |
| Optional ListenBrainz username (F11, not yet built) | account linking | SQLite | until unlinked | Medium |

Not collected, ever: telemetry, analytics, crash reports to third parties, accounts,
email addresses (beyond a user-supplied SMTP target), music files.

## D. Transparency & explainability

- **In the product:** recommendation provenance shown in-UI once F7 ships ("similar
  to X, Y, Z you own"); a data-flow explanation in `README.md`; `Last verified:`
  currency stamps on any doc citing an external fact (DOC-15); a plain-language
  privacy notice targeted at an eighth-grade reading level once the onboarding
  wizard exists (A11Y-23 spirit).
- **Commitment:** no dark patterns, no hidden data flows — everything Encore sends
  anywhere is documented in this file and in `README.md`. **Auto-gated:** none yet
  (this is a documentation commitment, not a mechanically-testable one).
  **Review-gated:** any PR touching an outbound call or a notification payload is
  checked against this section.

## E. Accessibility (WCAG 2.2 AA)

- **Surface:** the onboarding wizard, review queue, settings, and recommendation
  browse page (all htmx/Jinja2 server-rendered, `docs/adr/0004`) — no UI exists yet
  at M0.
- **Commitment:** axe zero critical/serious/moderate, Lighthouse ≥0.95, full keyboard
  path including the review queue and wizard, 4.5:1 contrast, 24×24 touch targets,
  reduced-motion support, 320px reflow. The onboarding wizard is the
  accessibility-critical path: a consumer product that fails keyboard-only setup
  fails its own thesis. **Auto-gated:** axe/pa11y-ci/Lighthouse in CI, merge-blocking
  from M2. **Review-gated:** an NVDA+VoiceOver walkthrough per release, recorded in
  an accessibility statement at `docs/a11y/STATEMENT.md` (from M4).

## F. Security

- **Threat model:** three actors, detailed in `../encore-plans/06-privacy-responsible-tech.md`
  §threat model: (1) a household/shared-server observer who could be outed by a taste
  feed landing somewhere visible to them; (2) a token thief (stolen backup, stolen
  disk) — countered by encryption at rest with the boundary stated honestly
  (`docs/adr/0008`); (3) the developer — countered structurally, since no telemetry
  endpoint exists to exfiltrate to, and CI itself runs with deny-by-default egress.
- **Controls:** ASVS **L2** (Encore stores a credential granting full Plex access
  plus sensitive taste data) — Semgrep zero HIGH/CRIT, CodeQL, pip-audit, gitleaks,
  Trivy on the Dockerfile, keyless cosign + SLSA provenance on release, Scorecard
  ≥8 once public. See `README.md` §standards conformance for what's wired today vs.
  what activates at which milestone.
- **Residual-risk register:** not yet instantiated (`docs/audits/residual-risk.md`)
  — there is no feature code yet to carry residual risk. Created at M1 alongside the
  first Plex-touching code. **Auto-gated:** the security CI stage (wired from M0).
  **Review-gated:** the residual-risk register is reviewed at each milestone exit.

---

## Committed artifacts

- `docs/audits/dpia.md` — full DPIA (data inventory above is the M0 seed; this file
  is created when the schema exists to regenerate it against, M1)
- `docs/audits/security-threat-model.md` — expanded from §F above (M1)
- `docs/audits/residual-risk.md` — created at M1
- `docs/a11y/STATEMENT.md` — accessibility conformance statement (M4)

No LLM exists in this product, so RTF-09..15 (AI risk register, EU AI Act
classification, model cards) are **N/A-with-reason** until F14 (optional "vibe"
recommendations, deliberately last — `docs/adr` will gain a new entry the day an LLM
SDK import lands, per `../encore-plans/05-standards-alignment.md` §AI-eval).
