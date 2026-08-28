# Definition of Done

A change is **done** when it is provably correct, gated, and reversible — not when it
"works on my machine." This file is the acceptance contract: the same checklist the
PR template enforces, with the rationale spelled out. Cross-cutting rigor (coverage
floors, the gate model, the security posture) lives once in the portfolio's private
`STANDARDS/` (fetched at CI time, never committed here) and is *referenced*; per-repo
target values live in [`docs/ROADMAP.md`](docs/ROADMAP.md).

> **The one command that proves it:** `make verify` reproduces the full CI gate set
> locally — `format+lint · type · test (≥85% branch) · security · responsible ·
> todo-gate · slo/citation/i18n schema · external-refs · wheel · container
> (build + Trivy CVE scan + `/livez` bring-up)` — plus a11y once the UI exists (M2).
> It is the same command `ci.yml` and `release.yml` invoke, not a parallel
> reimplementation. A change is not done until `make verify` is green. A
> *milestone* is not done until it is green on `main`.
>
> The container stage is listed explicitly because it used to be missing here and
> present in `ci.yml`, which made this paragraph untrue in the direction that costs
> the most: `make verify` went green on trees whose image CI then failed on a real
> HIGH CVE. Running the full gate now needs `docker` and `trivy` on `PATH`, the same
> way `make security` already needs `osv-scanner` and `gitleaks`; it fails closed
> with an actionable message rather than skipping.

## Acceptance criteria (every PR)

### Code quality and types
- [ ] `make lint` clean — `ruff format --check` and `ruff check` pass (no new
      suppression without a comment justifying it).
- [ ] `make type` clean — `mypy --strict` passes; no new `# type: ignore` without a
      reason.
- [ ] `make cov` green with **branch coverage ≥85%**. New code paths carry tests;
      the Plex client, matching, and watch/diff logic are unit-tested against
      recorded fixtures, not live network calls.

### Privacy guarantees (load-bearing, from M1)
- [ ] The **no-outing / no-secrets-in-logs** guard (`pytest -m no_outing -m
      no_secrets_in_logs`) still passes: no Plex token, Apprise URL, feed token, or
      identity-inferring taste data appears in any log line above DEBUG, API
      response, export, or error message.
- [ ] The **read-only-Plex** guard (`pytest -m read_only_plex`) still passes: the
      Plex client wrapper exposes no mutating verbs.
- [ ] If `src/encore/plex/`, `src/encore/matching/`, or anything touching secrets
      storage changed, an **ADR is linked** and a **CODEOWNER reviewed** it.

### Accessibility gate (merge-blocking from M2)
- [ ] `make a11y` (once it exists) passes on any rendered surface — WCAG 2.2 AA:
      landmarks, labels, `lang`, alt text, contrast, full keyboard path. A
      regression fails the build; it is not a follow-up ticket. Before M2, this
      section is N/A — there is no UI yet.

### Security and supply chain
- [ ] `make security` clean (pip-audit; gitleaks); no secrets committed;
      dependencies on the pinned lock. CodeQL (python + actions), zizmor, and
      Trivy (every container build, not just at release) stay green in CI.

### Rate-budget discipline (from M2, once release watching exists)
- [ ] Any change touching the MusicBrainz/ListenBrainz client respects the shared
      global 1 req/s token bucket (`docs/adr/0001-release-group-level-watching.md`
      §budget) — no per-job limiter that can sum past the shared budget, backoff on
      `Retry-After` honored.

### Documentation and traceability
- [ ] Docs and `CHANGELOG.md` `[Unreleased]` updated; user-visible impact described,
      not commit subjects.
- [ ] `docs/RESPONSIBLE-TECH-AUDITS.md`'s data-inventory table updated if the change
      adds, removes, or changes retention of anything Encore stores.

### ISO 25010 quality characteristic — **named**
- [ ] The PR names the **ISO/IEC 25010:2023** product-quality characteristic(s) it
      primarily moves, so quality is argued, not assumed:

  | Characteristic | Typical encore sub-characteristic |
  |---|---|
  | Functional suitability | correctness — a matched artist is the artist it claims to be |
  | Reliability | fault tolerance — MB/LB backoff, skip-don't-queue after downtime |
  | Security | confidentiality — token/URL encryption at rest; no exfiltration |
  | Maintainability | modularity — independent sync·match·watch·notify·recommend |
  | Performance efficiency | time behaviour — rate-budget math holds at 1,000-artist scale |
  | Compatibility | interoperability — Apprise fan-out, RSS/iCal standards compliance |
  | Usability | accessibility — WCAG 2.2 AA; learnability — 10-minute onboarding |
  | Portability | adaptability — Plex today, adapter interface designed for Jellyfin/Navidrome (F12) |

### Rollback
- [ ] A **rollback plan** is stated. Encore is SQLite-backed, so state changes:
      either a flag/setting that reverts behavior, or a clean single-commit revert
      plus a documented migration-down step if the schema changed. Never revert a
      migration by hand-editing the database.

## Phase-level / release Definition of Done

Beyond per-PR criteria, a release is done when:

- [ ] A fresh user can `docker run` the image, complete onboarding, and get a real
      test notification in under 10 minutes without reading docs (F6 acceptance,
      `encore-plans/03-feature-plan.md`).
- [ ] Every CI gate is green on `main`; the release tag is signed and points at that
      exact commit, re-verified at the tag (see `.github/workflows/release.yml`).
- [ ] `CHANGELOG.md` `[Unreleased]` is promoted to the new version with its date and
      compare link.
- [ ] For v0.1.0 specifically: the validation spike (MBID auto-match rate on a real
      library) is committed, and the public/private repo-visibility decision is put
      to Chelsea alongside the trademark-sweep result (R5,
      `encore-plans/08-risks-and-counters.md`).
