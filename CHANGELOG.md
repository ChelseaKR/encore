# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **F1 Plex library sync (M1, 2026-07-17).** A read-only Plex adapter
  (`src/encore/plex/`, docs/adr/0007) wraps python-plexapi behind two mechanical
  guarantees: a transport-level `ReadOnlySession` that raises on any HTTP method
  other than GET/HEAD/OPTIONS before a byte leaves the process, and a facade
  whose public surface is asserted by test to contain no mutating operation.
  `encore plex configure` stores the server URL + token (token prompted or piped,
  never a CLI flag) and an optional multi-library selection; `encore sync` runs
  the on-demand inventory (`src/encore/sync.py`): upsert on the Plex rating key,
  tombstone artists that disappear (row kept, artist unwatched), resurrect them
  when they return, and skip "Various Artists" compilation pseudo-artists. A
  background scheduler (`src/encore/scheduler.py`, APScheduler) re-syncs daily by
  default (`$ENCORE_SYNC_INTERVAL_HOURS`; disabled when no Plex connection is
  configured; first run one interval out so restart loops never hammer Plex).
  Contract tests run against recorded-shape Plex XML fixtures including
  pagination, so a plexapi upgrade that changes endpoints or attributes fails in
  CI, not in an install. CI stage 8 (responsible-tech guards) is now an explicit
  blocking step: `make responsible` runs the `read_only_plex`,
  `no_secrets_in_logs`, and `no_outing` marker tests (the no-outing battery
  grows with the F4/F5 egress surfaces at M2). Scope honesty: no MusicBrainz
  matching yet (F2) — synced artists are stored unmatched; nothing is watched or
  notified yet (F3-F5).

- **Semgrep is now a blocking merge/release gate (SEC-07/SEC-02).** The pinned
  CLI scans `p/default`, `p/python`, and a repository rule that rejects passing
  token/secret/password/credential/taste fields to Python log calls. It runs
  inside `make security`, so local, CI, and tag verification share one command;
  inline `nosemgrep` suppressions are disabled, the custom rule has a regression
  fixture, and `.semgrep-waivers.yml` is committed with no waivers.

- **F0 storage & secrets layer (M1, 2026-07-11).** SQLite (WAL) via SQLModel in a
  single data directory (`src/encore/storage.py`), with ordered forward
  migrations tracked in `PRAGMA user_version`; a Fernet key file created 0600
  beside the database encrypting secret-bearing columns at rest
  (`src/encore/secretstore.py`, docs/adr/0008) — proven by a test that greps
  the raw database bytes for the plaintext token; the `settings` singleton
  table holding the Plex base URL + encrypted token (`src/encore/models.py`).
  `encore serve --data-dir` is back and wired for real this time (explicit
  flag > `$ENCORE_DATA_DIR` > `./data`), the Dockerfile `CMD` passes
  `--data-dir /data` again, and `/readyz` performs an actual database probe
  instead of returning a literal. Scope honesty: no Plex sync, matching, or
  scheduler yet — this is the prerequisite layer F1/F2 build on.

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
- `codeql.yml` scans automatically again (push to `main` + weekly + manual
  dispatch) with SARIF findings gated in-run because private-repo upload is
  unavailable without GHAS. GitHub Actions jobs remain externally blocked by the
  account budget; the configured controls are preserved rather than weakened.
  `docs/ROADMAP.md`'s §7 ledger states the real trigger state and §11 gained an
  explicit `### Observability` subheading (OBS-21, B12).
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

- Storage now fails closed when an existing database has no companion Fernet
  key instead of silently minting an unusable replacement. Existing key files
  must be regular, non-symlink paths with no group/other permissions, and a
  concurrent first-start process reuses the exclusive-create winner or reports
  a clear recovery error. CodeQL no longer requests `security-events: write`
  while SARIF upload is disabled, and the DPIA now correctly distinguishes the
  plaintext Plex base URL from the encrypted token. Backup documentation now
  requires a stopped, consistent copy of the complete `/data` fileset and states
  explicitly that a whole-volume backup contains the decryption key and must be
  protected as secret material.

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
