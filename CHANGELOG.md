# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Gate top-ups (2026-07-09).** `make verify` grew four gates: `osv-scanner`
  against `uv.lock` as the second dependency-scan engine beside pip-audit
  (SEC-11/13, roadmap B5); `make slo-check` schema-validating `slos/*.yaml`
  against the Observability Standard §4 shape (OBS-14, B6 part 1 —
  `scripts/validate_slos.py`); `make citation-check` (pinned `cffconvert
  --validate`, DOC-08, B10); and `make wheel` (`uv build`, CQ-10, B7 — the
  container is no longer the only build artifact). `release.yml`'s
  `verify-at-tag` now also refuses to release a tag whose `CHANGELOG.md` lacks
  a matching `## [X.Y.Z]` section (REL-10, B11). ruff's pydocstyle (`D`, pep257
  convention) joined the lint gate (CQ-31, B9). `PyYAML`/`types-PyYAML` added
  to the dev dependency group for the SLO validator.

- **Project scaffolding (M0).** Apache-2.0 license, standards conformance from day
  zero (CI-fetch of the portfolio's private `portfolio-standards`, pinned to
  `v1.0.1`), ADRs 0000–0008, `docs/ROADMAP.md` metrics ledger,
  `docs/RESPONSIBLE-TECH-AUDITS.md`, `docs/I18N.md`, a CI gate covering format,
  lint, strict typing, tests with coverage, dependency + secret scanning, and
  CodeQL, plus a health-check-only FastAPI app and Dockerfile as the empty
  src-layout package the gate runs against.
- **Conformance remediation (2026-07-05).** `docs/audits/dpia.md` (a real M0 DPIA,
  not a placeholder); ADR-0009 (AI-Evaluation N/A) and ADR-0010 (branch-protection
  posture) plus `docs/adr/template.md`; `.github/PULL_REQUEST_TEMPLATE.md`;
  Harden-Runner (audit mode) on every workflow; zizmor workflow-SAST job; CodeQL's
  `actions` language pack; a weekly full-history TruffleHog scan
  (`.github/workflows/trufflehog.yml`); a real Trivy container-CVE scan on every
  build (`ci.yml` Stage 9) and at release (`release.yml`); a container bring-up +
  `/livez` check in CI; `scripts/todo-gate.sh` (`make todo-gate`) enforcing an
  issue or milestone reference on every `TODO`/`FIXME`/`HACK`; ruff `C90`
  (max-complexity 10) and `TD`/`PGH` rules; `pytest --import-mode=importlib`;
  `OTEL_SERVICE_NAME` in the Dockerfile.
- **Standards conformance re-check** against the 2026-07-05 portfolio audit
  landed as `audit-2026-07-05/encore-REMEDIATION.md`'s dated status markers and
  execution log — see that file for the full per-item accounting.

### Changed

- **Standards audit remediation (2026-07-14).** Raised the declared mypy floor to
  `>=1.18`, restored automatic CodeQL scans on every `main` update plus a weekly
  schedule, and aligned the README/i18n declarations with the canonical standards
  names and reason-bearing N/A syntax. `CITATION.cff` remains deliberately undated
  until the first real tag; CFF 1.2.0 defines `date-released` as optional.
- `SECURITY.md` now leads with the email reporting channel: GitHub private
  vulnerability reporting is non-functional on a private free-plan repo
  (DOC-09, B13) — reorder back when the repo flips public. Its phantom
  `tests/fixtures/` reference now points at `tests/` until real fixture trees
  land with F1.
- `codeql.yml`'s header comment and `docs/ROADMAP.md`'s §7 ledger now state the
  workflow's real automatic trigger state; `docs/ROADMAP.md` §11 gained an explicit
  `### Observability` subheading (OBS-21, B12).
- CI (`ci.yml`) and the release gate (`release.yml`) now install via
  `uv sync --frozen` and run the actual `make` targets (`make install`,
  `make lint`, `make type`, `make cov`, `make security`, and — for
  `release.yml`'s `verify-at-tag` — the literal `make verify`), instead of a
  hand-copied `pip install -e ".[dev]"` that floated free of `uv.lock` and
  quietly drifted from what `make verify` actually runs (CQ-09, CICD-27).
  `release.yml`'s `verify-at-tag` job now runs the security stage
  (pip-audit + gitleaks) it previously omitted entirely (REL-14, SEC-11).
- Dev dependencies moved from `[project.optional-dependencies].dev` to PEP 735
  `[dependency-groups].dev` (CQ-27); install accordingly with
  `uv sync --frozen --all-extras --group dev` (`make install`).
- `__version__` (and the value `FastAPI(version=...)` reports) is now derived via
  `importlib.metadata.version("encore")` instead of being hand-copied in
  `src/encore/__init__.py` and `src/encore/app.py` separately (REL-02).
- README's standards-conformance table no longer states present-tense claims for
  things that don't exist yet (structured JSON logs, day-one i18n catalog
  infra, an unqualified accessibility "✅") — each now names its actual M0 state
  and activation milestone (DOC-14).

### Fixed

- README/`docs/ROADMAP.md` pointed at `docs/audits/dpia.md`, which did not exist;
  the file is now a real, if narrow, M0 DPIA rather than a corrected-away claim.
- `docs/RESPONSIBLE-TECH-AUDITS.md` §F claimed "CI itself runs with
  deny-by-default egress" with no egress control configured anywhere; Harden-Runner
  (audit mode) is now wired into all four workflows, and the claim is worded to
  match reality (audit, not yet enforcing).
- `.github/workflows/standards.yml` referenced a `.standards-version` file that
  didn't exist; it's now committed and holds the same tag the workflow fetches.
- `release.yml`'s `# TODO ... Trivy` comment had fooled the portfolio's Tier-1
  conformance checker into crediting a container scan that didn't exist
  (`container_cve_scan: pass`, a false green). A real, SHA-pinned
  `aquasecurity/trivy-action` step now runs on every container build.
- `src/encore/cli.py`'s `--data-dir` flag was parsed but never used, while the
  Dockerfile's `CMD` passed it anyway; the flag is dropped until M1's storage
  layer gives it something to do, and the Dockerfile no longer passes it.

[Unreleased]: https://github.com/ChelseaKR/encore/commits/main
