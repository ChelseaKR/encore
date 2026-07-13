# encore — Implementation Roadmap

> Generic enforcement lives in the portfolio's private `STANDARDS/` (fetched at CI
> time, never committed here). This document carries the decisions and
> project-specific values.
> **Last verified: 2026-07-12 · Recheck cadence: at each milestone exit.**

## 1. Snapshot

Repo status: **Pre-alpha, M1 in progress.** The F0 prerequisite is real: SQLite
WAL storage, forward migrations, encrypted-at-rest settings, and a database-backed
readiness probe. Plex sync, matching, and every end-user product feature remain
unbuilt. See `CHANGELOG.md` `[Unreleased]` and `encore-plans/CONTEXT.md` for the
planning history that preceded this repo.

## 2. Problem & users

See `README.md` and the planning corpus at `../encore-plans/` (`01-market-landscape.md`,
`02-positioning.md`) for the full thesis, audience, and non-goals. One line: a
self-hosted Plex music user gets release alerts and recommendations without any tool
in the stack ever touching acquisition.

## 3. Product definition

Features F1–F14, ranked and sequenced, live in `../encore-plans/03-feature-plan.md`.
MVP is F1–F6 (M1–M2); v1 adds F7–F10 (M3); F11–F14 are parked (see that document's
"Beyond v1" and "Cut list" sections for what was deliberately left out and why).

## 4. Research & evidence

The market-landscape research (`../encore-plans/01-market-landscape.md`) verified
that no existing free tool combines Plex-native sync, release alerts, and
recommendations without being built around downloading music. Claims there carry
per-claim verification status and a `2026-07-05` currency stamp; re-verify at the
next milestone exit per that document's own recheck note (DOC-15).

## 5. Experience & design

The onboarding wizard (F6) is the accessibility- and usability-critical path: paste
Plex URL + token → pick library → live-progress initial match (~17 min at MB rate
limits) → pick a notification channel → test-fire it, in under 10 minutes without
reading docs. Server-rendered htmx UI throughout (`docs/adr/0004`); no SPA.

## 6. Architecture

Stack, data model, the two pipelines (sync/watch, recommend), and the MusicBrainz
rate budget are specified in `../encore-plans/04-architecture.md` and instantiated
as ADRs in `docs/adr/`. Summary: Python 3.12+, FastAPI + htmx + Jinja2, SQLite (WAL)
via SQLModel, APScheduler, httpx, python-plexapi, Apprise; single OCI image.

## 7. Quality attributes & metrics

| Metric | Gate | Target | Stage | Current status |
|---|---|---|---|---|
| Branch coverage | AUTO (CQ-08) | ≥85% | 4 | Met (96.57% over 20 tests, including F0 storage/secrets) |
| mypy --strict errors | AUTO (CQ-06) | 0 | 3 | Met |
| ruff (format+lint) | AUTO (CQ-04) | 0 findings | 1–2 | Met |
| Semgrep HIGH/CRIT | AUTO (SEC-07) | 0 | 5 | Met — pinned Semgrep scans `p/default`, `p/python`, and Encore's no-sensitive-values-in-logs rule in `make security`; the committed waiver ledger is empty |
| Fixable HIGH/CRIT vulns (pip-audit + osv-scanner) | AUTO (SEC-11/13) | 0 | 5 | Met (both engines wired 2026-07-09 — pip-audit on the locked env, osv-scanner on `uv.lock`) |
| CodeQL | AUTO (SEC-08) | 0 alerts | 5 | Wired for manual and weekly scans; private-repo SARIF is checked in-run with upload disabled. Actions jobs remain externally blocked until the account budget is restored (roadmap B1/U6) |
| Secret scan (gitleaks) | AUTO (SEC-17/18) | clean | 5 | Met (pre-commit + CI) |
| Scorecard aggregate | AUTO (SEC-37) | ≥8 | 5 | Not yet run — requires a public repo; deferred to the public/private flip |
| Lighthouse a11y | AUTO (A11Y-02) | ≥0.95 | 6 | **N/A today** — F0 has no UI surface; applies from M2 (first real UI) |
| axe critical/serious/moderate | AUTO (A11Y-01) | 0 | 6 | **N/A today**, same reason |
| Perf stage | N/A — no measurable hot path exists yet; revisit when a UI/poller creates one | — | 7 | N/A-with-reason (CICD-29) |
| Sentinel/no-outing guard | AUTO (RTF-02, project) | pass | 8 | **N/A today** — no Plex/matching code exists; activates with F1 |
| Read-only-Plex guard | AUTO (project) | pass | 8 | **N/A today**, same reason; activates with F1 |
| Trivy CRITICAL,HIGH | AUTO (SEC-28) | 0 | 9 | Met — scans the built image on every push (`ci.yml`) and again at tag (`release.yml`), not deferred to first release |
| Container bring-up (`/livez` probe) | AUTO (QM-08, OBS-19) | 200 OK | 9 | Met (wired 2026-07-05) |
| Workflow SAST (zizmor) | AUTO (CICD-19) | 0 findings | 5 | Met (wired 2026-07-05, `ci.yml`) |
| CodeQL `actions` pack | AUTO (CICD-20) | 0 alerts | 5 | Wired 2026-07-05 (`codeql.yml`); same account-budget caveat as the CodeQL row above |
| SLO schema (`slos/*.yaml`) | AUTO (OBS-14) | conforms | 4 | Met — `make slo-check` (`scripts/validate_slos.py`, wired 2026-07-09); the SLI query itself stays a documented placeholder until the F3 poller exists (M2) |
| CITATION.cff validity | AUTO (DOC-08) | valid | 4 | Met — `make citation-check` (pinned cffconvert via uvx, wired 2026-07-09) |
| Wheel/sdist build | AUTO (CQ-10) | builds | 9 | Met — `make wheel` (`uv build`) in `make verify` + CI (wired 2026-07-09); container is no longer the only artifact |
| CHANGELOG section at tag | AUTO (REL-10) | present | 9 | Met — grep gate in `release.yml` `verify-at-tag` (wired 2026-07-09); fires at first tag |
| Full-history secret scan (TruffleHog, verified) | AUTO (SEC-19) | 0 verified | 5 | Met — weekly, `.github/workflows/trufflehog.yml` (wired 2026-07-05) |
| CI egress policy (Harden-Runner) | AUTO (SEC-04) | audit today | 1–9 | Met at `audit` — every workflow; flips to `block` once the steady-state endpoint allowlist is known from a few runs' telemetry |
| MB rate-limit violations (soak counter) | AUTO (project) | 0 | 4 | N/A — no polling code exists yet; applies from M2 |
| Auto-match precision (fixture library) | AUTO (project) | ≥95% fixtures; ≥90% field | 4 | N/A — applies from M1, after the validation spike |

No row is a bare `N/A` — every one carries its reason and the milestone it activates
at (DOC-12/13).

## 8. Implementation plan for Claude Code

Milestones M0–M4 with exit criteria are specified in full in
`../encore-plans/07-metrics-and-sequencing.md`. Summary:

- **M0 — Spec & scaffold** (complete). *Exit:* CI green on the empty package;
  plans-folder content graduated into repo docs in repo voice (done — this file,
  `README.md`, `docs/RESPONSIBLE-TECH-AUDITS.md`, `docs/I18N.md`, and `docs/adr/`
  are that graduation).
- **M1 — Sync & match** (in progress; F0 storage prerequisite complete, F1/F2 unbuilt). *Exit:* validation spike committed; ≥90%
  auto-match on the reference library; the read-only-Plex and no-outing guards land
  as tests and go merge-blocking.
- **M2 — Watch & alert** (F3–F6, the MVP line). *Exit:* fresh install → test
  notification in <10 min; 24h soak with zero duplicate alerts and zero rate-limit
  violations; a11y gates go merge-blocking with the first real UI.
- **M3 — Discover** (F7–F10). *Exit:* rec page <2s from cache; noise budget honored;
  dismissals persist.
- **M4 — Consumer polish & first release** (repo status → `Beta`). *Exit:*
  v0.1.0 published to GHCR; a second human installs it from docs alone; public/private
  flip decision put to Chelsea with the trademark-sweep result — see
  `docs/adr/0010-branch-protection-deferred-private-repo.md` for what's blocked
  on that decision (branch protection, Scorecard, private-vulnerability-reporting)
  and the interim, self-imposed discipline in place until then.

## 9. Go-to-market & community

FOSS, no monetization plan — deliberate at this scale (`../encore-plans/02-positioning.md`
§sustainability). GitHub Sponsors on the repo once public; a MetaBrainz donation
nudge in the README, live from M0.

## 10. Legal & compliance

Apache-2.0. Non-goals published in `README.md` verbatim per the planning corpus's
graduation instruction. No regulated-data compliance regime applies (no health,
financial, or minor's data); the privacy posture is governed by the DPIA in
`docs/RESPONSIBLE-TECH-AUDITS.md` and `docs/audits/dpia.md`, not by an external
statute.

## 11. Operations & sustainability

### Observability

**Observability tier: A** (this is a running, self-hosted service, not a CLI/library
— `../encore-plans/04-architecture.md` §deployment & operations). `/livez` and
`/readyz` exist today (`src/encore/app.py`); `readyz` performs a real database
probe since F0 (M1, 2026-07-11) and gains the scheduler-heartbeat check at M2
with the first poller. Structured JSON logs with secret/PII redaction, RED metrics
per route, and `slos/encore.yaml` (poll-freshness SLO) are specified now and
instantiated as the routes and pollers they measure land (M1–M2) — see
`slos/encore.yaml` for the declared target, schema-validated on every `make verify`
(`make slo-check`, OBS-14).

## 12. Responsible-tech summary

Full audits A–F in `docs/RESPONSIBLE-TECH-AUDITS.md`. One-line summary: Encore
treats music taste as sensitive-inference data, not just preference data, and is
designed so that holding a Plex-connected instance never becomes a way to out
someone sharing that Plex server.
