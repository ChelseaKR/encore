# Project Scope

Last reviewed: 2026-07-11. Base branch: `main`.

This file is a plain-language map of the project as it exists on `main`. It does not replace the README, roadmap, audit docs, or source comments. It points to them so a reviewer can see the whole shape without reading every file first.

## What This Project Is

Encore is a self-hosted release watcher for Plex music libraries. It will read the artists someone already has, match them to MusicBrainz, watch for new releases, and recommend related artists — all without downloading music. Every one of those product features is currently planned, not built: what exists on `main` today is the FastAPI/CLI scaffold with health endpoints plus the F0 storage layer (SQLite with migrations, Fernet secrets-at-rest, a settings table that can hold Plex credentials — no code reads from Plex yet; see *What It Covers* and *Outside This Scope*).

Package metadata checked in this pass:

- Python package `encore` for Python `>=3.12`.

## Who It Serves

- Plex music users who want alerts for new releases from artists they already own.
- Self-hosters who want a local service with feeds and notifications.
- Maintainers who care about clear boundaries around metadata, recommendations, and piracy-adjacent tooling.

## What It Covers

- A FastAPI app and CLI scaffold.
- A storage layer (F0): SQLite in WAL mode via SQLModel, ordered forward migrations, and Fernet encryption at rest for the Plex token, with the key file beside the database.
- Planned read-only Plex sync, MusicBrainz matching, release polling, notifications, RSS, iCal, and ListenBrainz recommendations.
- ADRs covering polling, metadata source choices, UI, SQLite, matching, secrets, and support posture.
- SLO, security, roadmap, i18n, and responsible-tech docs.
- A Dockerfile and release workflow for later packaged runs.

## How It Is Put Together

- src/encore/ contains the app factory, CLI entry point, and the storage layer (`storage.py`, `models.py`, `secretstore.py`).
- docs/adr/ records the planned design decisions.
- slos/ contains service-level declarations.
- tests/ verifies the app, CLI, storage/migrations, and the encrypted-at-rest guarantee.
- The Dockerfile is the one-container install path (`--data-dir /data` on a mounted volume).

Observed source and operations surfaces:

- `Dockerfile`
- `Makefile`
- `pyproject.toml`
- `scripts/`
- `slos/`
- `src/`

GitHub workflow files checked:

- `.github/workflows/ci.yml`
- `.github/workflows/codeql.yml`
- `.github/workflows/release.yml`
- `.github/workflows/standards.yml`
- `.github/workflows/trufflehog.yml`

## Trust Boundaries

- The product is notification-first and does not fetch or download media.
- Plex access is intended to be read-only.
- MusicBrainz and ListenBrainz calls reveal artist metadata to those services, so the privacy docs treat that as a user disclosure choice.

## Outside This Scope

- The repo is pre-alpha and much of the product surface is planned rather than built.
- It is not a media player, metadata editor, cloud service, or downloader.
- External metadata rate limits and matching quality remain product constraints.

## Docs And Evidence Checked

This pass checked the hand-authored docs and metadata, the test suite, and all
workflow files on `main`. Generated caches and build artifacts are excluded.

Primary docs checked:

- `.github/PULL_REQUEST_TEMPLATE.md`
- `CHANGELOG.md`
- `CITATION.cff`
- `CONTRIBUTING.md`
- `DEFINITION_OF_DONE.md`
- `LICENSE`
- `README.md`
- `SECURITY.md`
- `docs/I18N.md`
- `docs/RESPONSIBLE-TECH-AUDITS.md`
- `docs/ROADMAP.md`
- `docs/adr/0000-record-architecture-decisions.md`
- `docs/adr/0001-release-group-level-watching.md`
- `docs/adr/0002-poll-dont-webhook.md`
- `docs/adr/0003-metabrainz-sole-metadata-supplier.md`
- `docs/adr/0004-server-rendered-htmx-ui.md`
- `docs/adr/0005-sqlite-single-container.md`
- `docs/adr/0006-mbid-matching-with-review-queue.md`
- `docs/adr/0007-read-only-plex-client.md`
- `docs/adr/0008-secrets-at-rest-scheme.md`
- `docs/adr/0009-ai-evaluation-not-applicable.md`
- `docs/adr/0010-branch-protection-deferred-private-repo.md`
- `docs/adr/template.md`
- `docs/audits/dpia.md`
- `docs/audits/residual-risk.md`
- `docs/audits/security-threat-model.md`

Representative test files checked:

- `tests/__init__.py`
- `tests/test_app.py`
- `tests/test_cli.py`
- `tests/test_secrets_at_rest.py`
- `tests/test_storage.py`

## Validation Notes

This file was generated from the clean `origin/main` worktree for the 2026-07-08 docs audit and re-reviewed 2026-07-11 alongside the F0 storage-layer PR (the code-state and test-file sections above were updated to match). Project test suites are the authority for code behavior; `make verify` on the PR branch is the gate that proves the claims in *What It Covers*.
