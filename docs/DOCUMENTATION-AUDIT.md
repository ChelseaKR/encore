# Documentation Audit

Last reviewed: 2026-07-08. Base branch: `main`.

This audit records the documentation sweep and remediation loop for this repository. It checks the docs as a system: entry points, root-level process and legal files, project scope, setup and validation notes, safety and privacy posture, architecture and planning docs, local links, and the places where code, tests, workflows, and docs meet.

## Audit Results

| Area | Result | Evidence |
| --- | --- | --- |
| Entry docs | pass | `README.md` present |
| Security/process docs | pass | CONTRIBUTING.md, SECURITY.md, CHANGELOG.md |
| Architecture/planning docs | pass | 12 architecture/interface docs; 1 planning/research docs |
| Safety/privacy/audit docs | pass | 3 safety/privacy/accessibility/audit docs |
| Validation surface | pass | 3 test files (2 test modules + `tests/__init__.py` — same counting rule as `docs/PROJECT-SCOPE.md`); 5 workflow files |
| Local doc links | pass | 0 unresolved after review remediation — see *Link Check* below for the case-sensitivity correction |

## Root-Level Documentation Audit

This section covers hand-authored documentation at the repository root and root-adjacent GitHub templates. It is separate from the `docs/` inventory so README, process, legal, release, and project-specific root files do not get hidden inside the larger docs tree.

| Surface | Result | Evidence |
| --- | --- | --- |
| Root README | pass | Present: `README.md` |
| Root process docs | pass | Present: `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md` |
| Root legal, citation, and conduct docs | pass | Present: `LICENSE`, `NOTICE`, `CITATION.cff`, `CODE_OF_CONDUCT.md` |
| Other root project docs | info | `DEFINITION_OF_DONE.md` |
| Root-adjacent GitHub templates | pass | `.github/PULL_REQUEST_TEMPLATE.md`, `.github/CODEOWNERS` |
| Root/template doc links | pass | 25 root-level/template links checked; 0 unresolved |

Root-level files checked:

- `CHANGELOG.md`
- `CITATION.cff`
- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `DEFINITION_OF_DONE.md`
- `LICENSE`
- `NOTICE`
- `README.md`
- `SECURITY.md`

Root-adjacent template files checked:

- `.github/PULL_REQUEST_TEMPLATE.md`
- `.github/CODEOWNERS`

## Remediation In This PR

- Added missing root-level remediation docs found by the audit loop, including legal, conduct, contribution, or security files where absent.
- Added `docs/PROJECT-SCOPE.md` as the plain-language project and boundary map.
- Added this audit record so future doc changes have a dated baseline.
- Added or refreshed the docs index so scope, audit, and primary docs are easy to find.
- Fixed or added root/doc remediation files: `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `NOTICE`.

## Repo Surfaces Checked

Package and workspace metadata:

- Python package `encore` (>=3.12).

Source and operations surfaces seen at the repo root:

- `Dockerfile`
- `Makefile`
- `pyproject.toml`
- `scripts/`
- `src/`
- `tests/`
- `uv.lock`

Workflow files checked:

- `.github/workflows/ci.yml`
- `.github/workflows/codeql.yml`
- `.github/workflows/release.yml`
- `.github/workflows/standards.yml`
- `.github/workflows/trufflehog.yml`

## Documentation Inventory

| Category | Count | Representative files |
| --- | ---: | --- |
| architecture and interfaces | 12 | `docs/adr/0000-record-architecture-decisions.md`, `docs/adr/0001-release-group-level-watching.md`, `docs/adr/0002-poll-dont-webhook.md`, `docs/adr/0003-metabrainz-sole-metadata-supplier.md`, `docs/adr/0004-server-rendered-htmx-ui.md`, `docs/adr/0005-sqlite-single-container.md`, `docs/adr/0006-mbid-matching-with-review-queue.md`, `docs/adr/0007-read-only-plex-client.md`, plus 4 more |
| entry points and repo process | 10 | `.github/CODEOWNERS`, `.github/PULL_REQUEST_TEMPLATE.md`, `CHANGELOG.md`, `CITATION.cff`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `LICENSE`, `NOTICE`, plus 2 more |
| other docs | 4 | `DEFINITION_OF_DONE.md`, `docs/I18N.md`, `docs/PROJECT-SCOPE.md`, `docs/README.md` |
| planning and research | 1 | `docs/ROADMAP.md` |
| safety, privacy, accessibility, and audits | 3 | `docs/DOCUMENTATION-AUDIT.md`, `docs/RESPONSIBLE-TECH-AUDITS.md`, `docs/audits/dpia.md` |

Full hand-authored doc inventory checked by this pass:

- `.github/CODEOWNERS`
- `.github/PULL_REQUEST_TEMPLATE.md`
- `CHANGELOG.md`
- `CITATION.cff`
- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `DEFINITION_OF_DONE.md`
- `LICENSE`
- `NOTICE`
- `README.md`
- `SECURITY.md`
- `docs/DOCUMENTATION-AUDIT.md`
- `docs/I18N.md`
- `docs/PROJECT-SCOPE.md`
- `docs/README.md`
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

## Link Check

- The original 2026-07-08 pass checked 37 local links in authored Markdown and
  MDX docs and reported 0 unresolved — but it ran on a case-insensitive
  filesystem (macOS), which masked one broken link: `docs/README.md` linked
  `roadmap.md` where only `docs/ROADMAP.md` exists, a 404 on GitHub's
  case-sensitive rendering.
- Review remediation (2026-07-11) removed that duplicate link and re-ran the
  check case-sensitively against the git index (which records exact case):
  every remaining relative link in authored docs resolves; 0 unresolved.
- Root-level/template unresolved links after remediation: 0.

## Validation Notes

- The audit was generated from a clean worktree based on `origin/main` for this PR branch.
- Ran a local relative-link check over hand-authored Markdown and MDX docs.
- Ran an explicit root-level documentation presence and link check for README, process, legal, project, and template docs.
- Ran `git diff --check` across the PR worktrees after remediation.
- Product test suites remain the authority for runtime behavior; this PR changes documentation only.
