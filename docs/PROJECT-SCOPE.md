# Project Scope

Last reviewed: 2026-07-08. Base branch: `main`.

This file is a plain-language map of the project as it exists on `main`. It does not replace the README, roadmap, audit docs, or source comments. It points to them so a reviewer can see the whole shape without reading every file first.

## What This Project Is

Encore is a self-hosted release watcher for Plex music libraries. It reads the artists someone already has, matches them to MusicBrainz, watches for new releases, and can recommend related artists without downloading music.

Package metadata checked in this pass:

- Python package `encore` for Python `>=3.12`.

## Who It Serves

- Plex music users who want alerts for new releases from artists they already own.
- Self-hosters who want a local service with feeds and notifications.
- Maintainers who care about clear boundaries around metadata, recommendations, and piracy-adjacent tooling.

## What It Covers

- A FastAPI app and CLI scaffold.
- Planned read-only Plex sync, MusicBrainz matching, release polling, notifications, RSS, iCal, and ListenBrainz recommendations.
- ADRs covering polling, metadata source choices, UI, SQLite, matching, secrets, and support posture.
- SLO, security, roadmap, i18n, and responsible-tech docs.
- A Dockerfile and release workflow for later packaged runs.

## How It Is Put Together

- src/encore/ currently contains the app factory and CLI entry point.
- docs/adr/ records the planned design decisions.
- slos/ contains service-level declarations.
- tests/ verifies the scaffolded app and CLI.
- The Dockerfile is the future one-container install path.

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

This pass checked 24 hand-authored doc or metadata files, 3 test files, and 5 workflow files on `main`. The count excludes vendored provider licenses, dependency folders, generated cache files, and large generated artifact history.

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

Representative test files checked:

- `tests/__init__.py`
- `tests/test_app.py`
- `tests/test_cli.py`

## Validation Notes

For this docs PR, validation means the scope file was generated from the clean `origin/main` worktree, reviewed against repo metadata and docs inventory, and checked with `git diff --check`. Project test suites are still the authority for code behavior, because this PR changes documentation only.
