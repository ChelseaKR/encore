# Contributing to encore

Thank you for considering a contribution. Encore holds a credential that grants full
access to your Plex server and taste data that can be more revealing than it looks
(see [`docs/RESPONSIBLE-TECH-AUDITS.md`](docs/RESPONSIBLE-TECH-AUDITS.md) §B/§C), so
contributing here carries one obligation beyond the usual: never let real secrets or
real taste data reach the repository.

If you have not yet, read [`README.md`](README.md) for what the project is and why,
and [`SECURITY.md`](SECURITY.md) for how to report a vulnerability.

## Project independence

Encore is an independent, personal open-source project. It is not affiliated with,
sponsored by, or endorsed by any employer, client, government, or institutional
customer, and it contains no proprietary, confidential, or client material. Please
keep it that way: do not contribute anything you do not have the right to release
under Apache-2.0.

## The no-real-secrets rule (read this first)

**Never paste a real Plex token, a real Apprise/notification-channel URL, or real
listening/taste data into an issue, a pull request, a commit, a log, a screenshot, a
test, or a fixture.** Reproduce bugs with the synthetic tests under
[`tests/`](tests/) — use sentinel artists and sentinel credentials only. If a fixture you need doesn't exist yet, add a synthetic one
rather than reaching for anything real.

This is enforced socially in review and mechanically where possible: gitleaks in
pre-commit and CI, and the no-outing/no-secrets-in-logs guards described below. A
pull request that violates it will be closed and, if needed, the history scrubbed.

## Getting set up

Encore targets Python 3.12+ and uses [`uv`](https://docs.astral.sh/uv/) for a
reproducible, locked environment:

```sh
make install
```

Run `make help` to see every target.

The full gate also shells out to tools that are not Python dependencies, so
`make verify` needs these on your `PATH`: `gitleaks`, `osv-scanner`, `shellcheck`,
`docker`, and `trivy`. Each gate fails closed with an actionable message when its
tool is missing — none of them skip. `trivy` and `docker` back the container stage
(build, CVE scan, `/livez` bring-up), which is the stage that has most often failed
in CI, so it is worth having locally rather than discovering it on a pull request.

## The merge gate

A change merges when the full gate is green. Reproduce it locally with:

```sh
make verify
```

`make verify` runs **format-check + lint + type + test/coverage + security +
todo-gate** — the exact same targets `ci.yml` and `release.yml` invoke, on the
same pinned (`uv sync --locked`) toolchain, so green locally means green in CI:
there is no second, drifted reimplementation of the gate (CQ-09, CICD-27).

| Gate | Command | What it checks |
| --- | --- | --- |
| Format + lint | `make lint` | `ruff format --check` + `ruff check`: correctness, security (bandit rules), import hygiene, cyclomatic complexity (≤10) |
| Type | `make type` | `mypy --strict` over `src/encore` |
| Test + coverage | `make cov` | pytest; branch coverage ≥85% |
| Security | `make security` | Semgrep (`p/default`, `p/python`, the custom no-sensitive-log rule, plus `scripts/semgrep-test-gate.sh` proving that rule's own tests ran) + pip-audit + osv-scanner (both against the *locked* env) + gitleaks over **both** git history and the working tree |
| TODO gate | `make todo-gate` | every `TODO`/`FIXME`/`HACK` names a `#issue` or an `M0`–`M4` milestone |
| External refs | `make external-refs` | no committed file gains a reference to a path outside this repository (issue #22; ratchet against `.external-refs.yml`) |
| Container (stage 9) | `make container-verify` | `docker build` + Trivy CVE scan (CRITICAL/HIGH, fixed-only) + `/livez` bring-up — the same target `ci.yml` runs, needs `docker` and `trivy` on `PATH` and fails closed without them |

Cross-cutting rigor lives once in the portfolio's private `STANDARDS/`
(fetched at CI time via `.github/workflows/standards.yml`, never committed
here); this file and `DEFINITION_OF_DONE.md` state the per-repo targets those
standards require, so contributors don't need read access to the private repo
to know what "done" means here.

Two guarantees are called out separately because they protect the project's core
privacy promise, and a regression in either must be unmistakable, not buried (once
the code they cover exists — see the milestone note in each):

- **No-outing / no-secrets-in-logs** (`pytest -m no_outing` / `-m no_secrets_in_logs`,
  landing at M1). Structured logs, the JSON API, feed output, and error messages must
  never contain a Plex token, an Apprise URL, a feed token, or taste data that could
  identify or out someone sharing a Plex server. Treat a regression here like memory
  unsafety elsewhere.
- **Read-only Plex** (`pytest -m read_only_plex`, landing at M1). The Plex client
  wrapper must expose no mutating verbs. Encore never writes to your Plex server.

## Accessibility

Once the UI exists (M2), any change touching `src/encore/app.py`'s rendered routes or
`templates/` must keep the WCAG 2.2 AA gate green: landmarks, labels, contrast,
keyboard navigation, no color-only meaning. Not merge-blocking before M2 — there's no
UI to regress yet — but plan for it: the onboarding wizard is the accessibility-
critical path (a consumer product that fails keyboard-only setup fails the whole
thesis).

## Commit style: Conventional Commits

This repository uses [Conventional Commits](https://www.conventionalcommits.org/).
The type drives the changelog and the next semver bump.

```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `build`, `ci`,
`chore`. A breaking change is marked with `!` after the type/scope
(`feat(match)!: ...`) and explained in a `BREAKING CHANGE:` footer. Useful scopes
mirror the architecture: `plex`, `matching`, `watch`, `notify`, `recommend`, `app`,
`cli`, `docs`, `infra`.

Examples:

```
feat(watch): diff release-groups on MBID + first-release-date
fix(plex): stop leaking token in the onboarding error page
docs(adr): record decision to watch release-groups, not releases
```

## ADRs: record significant decisions

Any decision that is hard to reverse or that shapes the architecture, the threat
model, or a public interface gets an **Architecture Decision Record** in
`docs/adr/`. That includes the matching thresholds, the rate-limiting scheme, the
secrets-at-rest approach, and anything affecting the no-outing or read-only-Plex
guarantees.

Add an ADR as a numbered Markdown file (`docs/adr/NNNN-short-title.md`) using the
standard shape: **Title**, **Status** (Proposed / Accepted / Superseded),
**Context**, **Decision**, **Consequences**. Reference the ADR from the pull request
that implements it. Superseding an earlier decision means marking the old ADR
`Superseded by NNNN`, not deleting it.

## Pull requests

Open a PR against `main`. The short version of the checklist:

- `make verify` is green.
- No real Plex token, Apprise URL, or taste data appears anywhere in the diff —
  only synthetic sentinels.
- The accessibility gate is green if you touched a UI surface (M2+).
- An ADR is added if you made a significant decision.
- Docs are updated to match the change, including the DPIA data-inventory table in
  `docs/RESPONSIBLE-TECH-AUDITS.md` if you touched what's collected or retained.

Keep PRs focused, explain the *why* in the description, and link any related issue.

## Reporting bugs and security issues

- **Security and any secret-leak / no-outing flaw:** do **not** open a public issue.
  Use GitHub's private vulnerability reporting (the **Security** tab → "Report a
  vulnerability"), or email **ckellyreif@gmail.com** as a fallback. See
  [`SECURITY.md`](SECURITY.md). Describe the shape of the leak, reproduce with
  synthetic fixtures, paste no real credentials or taste data.
- **Ordinary bugs:** open a GitHub issue.

## Versioning and releases

Encore follows [Semantic Versioning](https://semver.org/). Before 1.0, public
interfaces may still change, but a breaking change is always flagged in the commit
and the [changelog](CHANGELOG.md). Releases are signed and tagged (`vX.Y.Z`), with
pinned dependencies and SHA-pinned GitHub Actions; every CI gate must be green for a
tag to ship, re-verified at the tagged commit.

## License

By contributing, you agree that your contributions are licensed under the project's
[Apache-2.0](LICENSE) license. You must have the right to release what you
contribute, and it must contain no proprietary or client material.
