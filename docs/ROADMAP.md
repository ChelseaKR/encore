# encore — Implementation Roadmap

> Generic enforcement lives in the portfolio's private `STANDARDS/` (fetched at CI
> time, never committed here). This document carries the decisions and
> project-specific values.
> **Last verified: 2026-07-05 · Recheck cadence: at each milestone exit.**

## 1. Snapshot

Repo status: **Scaffolded** (M0). No feature code yet — the app is a health-check
FastAPI skeleton plus the full standards/CI/docs scaffold. See `CHANGELOG.md`
`[Unreleased]` and `encore-plans/CONTEXT.md` for the planning history that preceded
this repo.

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

| Metric | Gate | Target | Stage | Status at M0 |
|---|---|---|---|---|
| Branch coverage | AUTO (CQ-08) | ≥85% | 4 | Met (100% of the health-check skeleton) |
| mypy --strict errors | AUTO (CQ-06) | 0 | 3 | Met |
| ruff (format+lint) | AUTO (CQ-04) | 0 findings | 1–2 | Met |
| Semgrep HIGH/CRIT | AUTO (SEC-07) | 0 | 5 | Not yet wired — Semgrep join CI when there's non-trivial application logic to scan (tracked, not silently dropped) |
| Fixable HIGH/CRIT vulns (pip-audit) | AUTO (SEC-11) | 0 | 5 | Met (wired) |
| CodeQL | AUTO (SEC-08) | 0 alerts | 5 | Met (wired, nightly + PR) |
| Secret scan (gitleaks) | AUTO (SEC-17/18) | clean | 5 | Met (pre-commit + CI) |
| Scorecard aggregate | AUTO (SEC-37) | ≥8 | 5 | Not yet run — requires a public repo; deferred to the public/private flip |
| Lighthouse a11y | AUTO (A11Y-02) | ≥0.95 | 6 | **N/A at M0** — no UI surface exists yet; applies from M2 (first real UI) |
| axe critical/serious/moderate | AUTO (A11Y-01) | 0 | 6 | **N/A at M0**, same reason |
| Perf stage | N/A — no measurable hot path at M0; revisit if the UI grows one | — | 7 | N/A-with-reason (CICD-29) |
| Sentinel/no-outing guard | AUTO (RTF-02, project) | pass | 8 | **N/A at M0** — no Plex/matching code exists yet; applies from M1 |
| Read-only-Plex guard | AUTO (project) | pass | 8 | **N/A at M0**, same reason; applies from M1 |
| Trivy CRITICAL,HIGH | AUTO (SEC-28) | 0 | 9 | Dockerfile builds in CI (validation only); full scan joins at first tagged release |
| MB rate-limit violations (soak counter) | AUTO (project) | 0 | 4 | N/A — no polling code exists yet; applies from M2 |
| Auto-match precision (fixture library) | AUTO (project) | ≥95% fixtures; ≥90% field | 4 | N/A — applies from M1, after the validation spike |

No row is a bare `N/A` — every one carries its reason and the milestone it activates
at (DOC-12/13).

## 8. Implementation plan for Claude Code

Milestones M0–M4 with exit criteria are specified in full in
`../encore-plans/07-metrics-and-sequencing.md`. Summary:

- **M0 — Spec & scaffold** (this state). *Exit:* CI green on the empty package;
  plans-folder content graduated into repo docs in repo voice (done — this file,
  `README.md`, `docs/RESPONSIBLE-TECH-AUDITS.md`, `docs/I18N.md`, and `docs/adr/`
  are that graduation).
- **M1 — Sync & match** (F1, F2). *Exit:* validation spike committed; ≥90%
  auto-match on the reference library; the read-only-Plex and no-outing guards land
  as tests and go merge-blocking.
- **M2 — Watch & alert** (F3–F6, the MVP line). *Exit:* fresh install → test
  notification in <10 min; 24h soak with zero duplicate alerts and zero rate-limit
  violations; a11y gates go merge-blocking with the first real UI.
- **M3 — Discover** (F7–F10). *Exit:* rec page <2s from cache; noise budget honored;
  dismissals persist.
- **M4 — Consumer polish & first release** (repo status → `Beta`). *Exit:*
  v0.1.0 published to GHCR; a second human installs it from docs alone; public/private
  flip decision put to Chelsea with the trademark-sweep result.

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

**Observability tier: A** (this is a running, self-hosted service, not a CLI/library
— `../encore-plans/04-architecture.md` §deployment & operations). `/livez` and
`/readyz` exist today (`src/encore/app.py`); `readyz` gains real DB/scheduler
heartbeat checks at M1. Structured JSON logs with secret/PII redaction, RED metrics
per route, and `slos/encore.yaml` (poll-freshness SLO) are specified now and
instantiated as the routes and pollers they measure land (M1–M2) — see
`slos/encore.yaml` for the declared target.

## 12. Responsible-tech summary

Full audits A–F in `docs/RESPONSIBLE-TECH-AUDITS.md`. One-line summary: Encore
treats music taste as sensitive-inference data, not just preference data, and is
designed so that holding a Plex-connected instance never becomes a way to out
someone sharing that Plex server.
