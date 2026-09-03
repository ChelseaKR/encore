# encore — Implementation Roadmap

> Generic enforcement lives in the portfolio's private `STANDARDS/` (fetched at CI
> time, never committed here). This document carries the decisions and
> project-specific values.
> **Last verified: 2026-09-01 · Recheck cadence: at each milestone exit.**

> **Nothing here delegates to a document a reader cannot open.** Seven pointers
> in this file used to send the reader elsewhere for the feature plan, the
> architecture, the market research, and the M0–M4 exit criteria — to an earlier
> planning corpus that is a plain local directory on one machine: not a git
> repository, not a submodule, never in this repository's history. For anyone
> who clones encore they resolved to nothing, and the criteria that decide
> whether a milestone is done were not reviewable in the repository claiming to
> have met them (issue #22). Each is now resolved in place: the section carries
> the content, or it says plainly that the content is not published here and
> does not pretend to summarize it. Where a claim rested on that corpus alone it
> is now labelled an unverified premise rather than research (§4).
> `make external-refs` pins this file at zero such references.

## 1. Snapshot

Repo status: **Pre-alpha, M2 in progress.** M1 is *feature*-complete (F0
SQLite WAL storage with forward migrations and encrypted-at-rest settings, F1
read-only Plex sync, F2 MusicBrainz matching), with one exit criterion
deliberately outstanding: the U8 validation spike has not run, so the ≥90%
auto-match rate on a real library is unmeasured and the auto-match thresholds
in `src/encore/matching/scoring.py` remain provisional (§7, §8 — issue #21).
M1 is therefore not *closed*; its remaining criterion is carried into the M2
soak. M2 is most of the way there: F3 release watching, F4 notifications, and
F5 standing feeds have landed. **F6, the
onboarding wizard, is the remaining MVP feature** — and with it the admin
password and the first rendered UI. Nothing has run against a live install for
24 hours yet; the soak is the M2 exit gate.

M3's four features landed ahead of F6, all on 2026-08-22 (#27–#30): F10 watch
settings, F9 listening-history weighting, F7 similar-artist recommendations, F8
promoted-recommendation watching. That does **not** close M3 — two of its three
exit criteria are measured on a recommendation *page*, which does not exist until
F6 brings the first rendered UI. So M1, M2 and M3 are all open at once, each on an
exit criterion rather than on a missing feature; the sequence F1–F14 is a priority
order, not a promise that milestones close in it. §8 carries the per-milestone
detail.

`CHANGELOG.md` `[Unreleased]` is the project history a reader can open. The
drafting notes that preceded this repository are not published here, and nothing
below depends on them.

## 2. Problem & users

`README.md` carries the thesis, audience, and non-goals a reader can open — the
non-goals verbatim (§10). The longer positioning and market-landscape drafts
those were distilled from are not published in this repository; §4 marks the one
claim that rested on them alone. One line: a self-hosted Plex music user gets
release alerts and recommendations without any tool in the stack ever touching
acquisition.

## 3. Product definition

MVP is F1–F6 (M1–M2); v1 adds F7–F10 (M3); F11–F14 are parked. The ranked
feature plan those labels came from is not published in this repository (issue
#22), so this section reconstructs the list from what the repository can itself
account for. Every row cites the committed file it was read off, including the
four parked ones — a numbered feature nobody can look up is the same dead
pointer the delegation was.

| # | Feature | Where this repository accounts for it |
|---|---|---|
| F0 | SQLite (WAL) storage, forward migrations, encrypted-at-rest settings | `src/encore/storage.py`; `docs/adr/0005`, `docs/adr/0008` |
| F1 | Read-only Plex library sync | `src/encore/plex/`, `src/encore/sync.py`; `docs/adr/0007` |
| F2 | MusicBrainz matching + review queue | `src/encore/matching/`; `docs/adr/0006` |
| F3 | Release watching | `src/encore/watch/`; `docs/adr/0001`, `docs/adr/0011` |
| F4 | Notifications — Apprise fan-out, delivery queue, digest cadences | `src/encore/notify/`; `docs/adr/0012` |
| F5 | Standing feeds — RSS + iCal behind a rotatable capability URL | `src/encore/feeds/`; `docs/adr/0013` |
| F6 | Onboarding wizard — the remaining MVP feature | **Not built.** `docs/adr/0004` fixes the server-rendered htmx approach |
| F7 | Similar-artist recommendations with provenance | `src/encore/recommend/engine.py`, `src/encore/recommend/lb.py` |
| F8 | Promotion of a recommendation into the release-watch pool | `src/encore/storage.py` (`list_watched_artist_mbids`), `src/encore/cli.py` |
| F9 | Listening-history weighting from Plex play counts | `src/encore/sync.py`, `src/encore/storage.py` (`listening_weights`) |
| F10 | Per-artist and global watch settings — types, muting, priority | `src/encore/artistsettings.py` |
| F11 | Optional ListenBrainz account linking — **parked** | `docs/RESPONSIBLE-TECH-AUDITS.md` §C — the data inventory carries it as "not yet built" and gates it on a DPIA update |
| F12 | Jellyfin/Navidrome adapter behind the same read-only interface — **parked** | `docs/adr/0007`, `docs/adr/0002`; `DEFINITION_OF_DONE.md` (portability) |
| F13 | Multi-user households, per-user data isolation — **parked** | `docs/adr/0005` (the single-container relaxation rule), `docs/adr/0008` |
| F14 | Optional "vibe" recommendations, deliberately last — **parked** | `docs/adr/0009`; `README.md` (the AI-eval N/A row and its flip trigger) |

**What is missing is the ranking, not the list.** The plan's ordering rationale
and its cut list — what was considered, deliberately left out, and why — are not
in this repository and are not reconstructible from it. So this section is a
definition, not a prioritization: it says what F0–F14 are and where each is
accounted for, and it does not say why they sit in that order or what was cut to
get there. Writing that down is the remainder of issue #22 for this section, due
before the M4 public/private flip (§8).

## 4. Research & evidence

**No published evidence backs this section.** The market-landscape scan behind
the claim below was written on 2026-07-05 into a draft that is not in this
repository, so neither its per-claim verification statuses nor its currency
stamp can be checked from a clone. It is recorded here as a premise, not as
research:

> No existing free tool combines Plex-native sync, release alerts, and
> recommendations without being built around downloading music.

That premise is load-bearing — it is the reason this project exists — and it is
also two months old in a category where a competitor shipping once would falsify
it. Until the scan is redone in the open, as a document in `docs/` carrying its
own sources, this section states what was believed and by when, and claims
nothing about whether it still holds (issue #22, DOC-15). It is due before the
M4 public/private flip (§8), where it would otherwise ship as a public
assertion this repository cannot support.

## 5. Experience & design

The onboarding wizard (F6) is the accessibility- and usability-critical path: paste
Plex URL + token → pick library → live-progress initial match (~17 min at MB rate
limits) → pick a notification channel → test-fire it, in under 10 minutes without
reading docs. Server-rendered htmx UI throughout (`docs/adr/0004`); no SPA.

## 6. Architecture

Stack, data model, the two pipelines (sync/watch, recommend), and the MusicBrainz
rate budget are recorded as ADRs in `docs/adr/`, which is where they are
reviewable; the earlier architecture draft they were derived from is not
published in this repository. Summary: Python 3.12+, FastAPI + htmx + Jinja2,
SQLite (WAL) via SQLModel, APScheduler, httpx, python-plexapi, Apprise; single
OCI image.

## 7. Quality attributes & metrics

| Metric | Gate | Target | Stage | Current status |
|---|---|---|---|---|
| Branch coverage | AUTO (CQ-08) | ≥85% | 4 | Met (95.85% over 172 tests, covering F0-F4) |
| mypy --strict errors | AUTO (CQ-06) | 0 | 3 | Met |
| ruff (format+lint) | AUTO (CQ-04) | 0 findings | 1–2 | Met |
| Semgrep HIGH/CRIT | AUTO (SEC-07) | 0 | 5 | Met — pinned Semgrep scans `p/default`, `p/python`, and Encore's no-sensitive-values-in-logs rule in `make security`; the committed waiver ledger is empty |
| Fixable HIGH/CRIT vulns (pip-audit + osv-scanner) | AUTO (SEC-11/13) | 0 | 5 | Met (both engines wired 2026-07-09 — pip-audit on the locked third-party set exported from `uv.lock`, osv-scanner on `uv.lock`) |
| CodeQL | AUTO (SEC-08) | 0 alerts | 5 | Python + Actions packs trigger on every `main` update, weekly, and on dispatch; private-repo SARIF is checked in-run with upload disabled (no GHAS). Actions jobs remain externally blocked until the account budget is restored (roadmap B1/U6) |
| Secret scan (gitleaks) | AUTO (SEC-17/18) | clean | 5 | Met (pre-commit + CI) |
| Scorecard aggregate | AUTO (SEC-37) | ≥8 | 5 | Not yet run — requires a public repo; deferred to the public/private flip |
| Lighthouse a11y | AUTO (A11Y-02) | ≥0.95 | 6 | **N/A today** — still no rendered UI. F5's HTTP routes serve RSS/iCal *documents* to feed readers and calendar clients, which have no DOM to audit; the accessibility burden there is the client's. Activates with F6's onboarding wizard, the first rendered surface |
| axe critical/serious/moderate | AUTO (A11Y-01) | 0 | 6 | **N/A today**, same reason |
| Perf stage | N/A — no measurable hot path exists yet; revisit when a UI/poller creates one | — | 7 | N/A-with-reason (CICD-29) |
| Sentinel/no-outing guard | AUTO (RTF-02, project) | pass | 8 | Active for the F1-F4 surfaces (2026-08-01): sync/scheduler logging is sentinel-tested; the matching and watch layers prove artist names/MBIDs/titles/token never reach logs or outbound MB requests; and F4's marker tests extend the guarantee to the **egress** surface — a notification body and an Apprise channel URL never reach a log line at any level, a plugin exception is reduced to its type before it can echo a URL, and the plaintext channel URL never reaches the database file (raw-bytes test). Blocking via `make responsible` in `make verify` + CI stage 8. **Extended to F5 (2026-08-04)**: the standing feeds are the project's first HTTP read surface for library content, so the marker tests pin that an unauthorized request — wrong token, no token minted, storage not open — returns a 404 byte-identical to a nonexistent route's and carries no sentinel artist or title, that rotation kills the old URL on the next request, and that the feed token reaches neither an encore log line nor the database file in plaintext. The remaining growth is F6's admin password |
| Read-only-Plex guard | AUTO (project) | pass | 8 | Met (F1, 2026-07-17) — transport-level `ReadOnlySession` rejects non-GET/HEAD/OPTIONS before network I/O, plus a facade no-mutating-verbs assertion (`tests/test_plex_client.py`, `read_only_plex` marker); blocking via `make responsible` |
| Trivy CRITICAL,HIGH | AUTO (SEC-28) | 0 | 9 | Met — scans the built image on every push (`ci.yml`) and again at tag (`release.yml`), not deferred to first release |
| Container bring-up (`/livez` probe) | AUTO (QM-08, OBS-19) | 200 OK | 9 | Met (wired 2026-07-05) |
| Workflow SAST (zizmor) | AUTO (CICD-19) | 0 findings | 5 | Met (wired 2026-07-05, `ci.yml`) |
| CodeQL `actions` pack | AUTO (CICD-20) | 0 alerts | 5 | Wired 2026-07-05 (`codeql.yml`); automatic triggers restored 2026-07-14; same account-budget caveat as the CodeQL row above |
| SLO schema (`slos/*.yaml`) | AUTO (OBS-14) | conforms | 4 | Met — `make slo-check` (`scripts/validate_slos.py`, wired 2026-07-09); the F3 poller and F4 delivery cycle now exist, but both SLI queries stay documented placeholders until a metrics surface ships. F5 landed the first HTTP routes (the feeds), so the RED-metrics debt is now **due, not hypothetical** — it is owed with F6, which brings the rest of the HTTP surface it would instrument |
| I18N extraction template current | AUTO (I18N G2-lite) | matches source | 4 | **Met (new 2026-08-01)** — `make i18n-check` in `make verify` + CI; the seam went live with F4's notification strings, the project's first user-facing text. G7/G6/G5/G3 stay deferred with reasons in `docs/I18N.md` (no second catalog exists to compile or compare) |
| Notification delivery (real service) | MANUAL (project) | test-fire arrives | 9 | **Not met — needs a live service.** Every F4 path is proven offline against a `NotificationSender` seam plus the real Apprise URL parser, but "Discord accepted this message" cannot be asserted without a Discord webhook. Verify with `encore channels test` against a real ntfy/Discord/SMTP target as part of the M2 exit soak |
| CITATION.cff validity | AUTO (DOC-08) | valid | 4 | Met — `make citation-check` (pinned cffconvert via uvx, wired 2026-07-09) |
| Wheel/sdist build | AUTO (CQ-10) | builds | 9 | Met — `make wheel` (`uv build`) in `make verify` + CI (wired 2026-07-09); container is no longer the only artifact |
| CHANGELOG section at tag | AUTO (REL-10) | present | 9 | Met — grep gate in `release.yml` `verify-at-tag` (wired 2026-07-09); fires at first tag |
| Full-history secret scan (TruffleHog, verified) | AUTO (SEC-19) | 0 verified | 5 | Met — weekly, `.github/workflows/trufflehog.yml` (wired 2026-07-05) |
| CI egress policy (Harden-Runner) | AUTO (SEC-04) | audit today | 1–9 | Met at `audit` — every workflow; flips to `block` once the steady-state endpoint allowlist is known from a few runs' telemetry |
| MB rate-limit violations (soak counter) | AUTO (project) | 0 | 4 | Polling code exists (F3, 2026-07-31): all MB traffic — search and browse — shares one process-global 1 req/s limiter, tested at the unit level; the soak *counter* itself needs a real 24h deployment and lands with the M2 exit soak |
| Auto-match precision (fixture library) | AUTO (project) | ≥95% fixtures; ≥90% field | 4 | **Fixture half met (F2, 2026-07-17)** — the 23-case known-nasty battery in `tests/test_matching_engine.py` gates ≥95% correct decisions and zero wrong auto-matches on every run; the ≥90% field half needs the U8 validation spike on a real library, which also freezes the provisional thresholds |

No row is a bare `N/A` — every one carries its reason and the milestone it activates
at (DOC-12/13).

## 8. Implementation plan for Claude Code

**The exit criteria below are the ones this repository is held to.** They were
drafted in a sequencing note that is not published here, and this section used to
defer to it — which meant the criteria deciding whether a milestone was done were
not reviewable in the repository claiming to have met them (issue #22). This
section does not claim to reproduce that draft; it is the standing copy, and
where a criterion is unmet or not yet measurable it says so in place rather than
being summarized away.

- **M0 — Spec & scaffold** (complete). *Exit:* CI green on the empty package;
  plans-folder content graduated into repo docs in repo voice (done — this file,
  `README.md`, `docs/RESPONSIBLE-TECH-AUDITS.md`, `docs/I18N.md`, and `docs/adr/`
  are that graduation).
- **M1 — Sync & match** (feature-complete, exit criterion outstanding; F0
  storage complete, F1 Plex sync complete 2026-07-17, F2 matching engine +
  review queue complete 2026-07-17). *Exit:* validation spike committed —
  **not done**; ≥90% auto-match on the reference library — **not measured**
  (U8 needs a real Plex library, a human-input task, and freezing the
  provisional thresholds depends on it); the read-only-Plex and no-outing
  guards land as tests and go merge-blocking — done with F1/F2
  (`make responsible`, CI stage 8). §1 states the same status: two of three
  met, M1 open until U8 runs.
- **M2 — Watch & alert** (F3–F6, the MVP line; F3 release watching complete
  2026-07-31 — poller, diff engine, baseline seeding per `docs/adr/0011`,
  events table, second scheduler, `encore watch`. F4 notifications complete
  2026-08-01 — Apprise fan-out, materialized delivery queue with bounded
  exponential backoff, instant + digest cadences, `encore channels`/`notify`,
  the CLI in-app feed, and the i18n seam per `docs/adr/0012`. F5 standing
  feeds complete 2026-08-04 — RSS release feed + iCal of upcoming dates
  behind a rotatable capability token per `docs/adr/0013`, `encore feeds
  show|rotate`, migration v6). *Remaining:* F6 onboarding wizard — which also
  brings the admin password the HTTP event feed is waiting on. *Exit:* fresh
  install → test notification in <10 min; 24h soak with zero duplicate alerts
  and zero rate-limit violations; a11y gates go merge-blocking with the first
  real UI.
- **M3 — Discover** (F7–F10, all four features complete 2026-08-22, ahead of F6 —
  #27 per-artist and global watch settings, #28 listening-history weighting from
  Plex play counts, #29 similar-artist recommendations with provenance from the
  ListenBrainz labs dataset, #30 promotion into the release-watch pool. CLI only:
  `encore recommend`, `encore recommendations list|dismiss|promote`, `encore
  settings`). *Exit:* rec page <2s from cache — **not measurable yet**, there is no
  rec page until F6's UI; noise budget honored — **not measured**, same reason it
  needs a live install as M2's soak does; dismissals persist — met in storage and
  regression-tested (a dismissed candidate does not resurrect on the next refresh).
  M3 is therefore feature-complete and open, blocked behind F6 like M2's soak.
- **M4 — Consumer polish & first release** (repo status → `Beta`). *Exit:*
  v0.1.0 published to GHCR; a second human installs it from docs alone; public/private
  flip decision put to Chelsea with the trademark-sweep result — see
  `docs/adr/0010-branch-protection-deferred-private-repo.md` for what's blocked
  on that decision (branch protection, Scorecard, private-vulnerability-reporting)
  and the interim, self-imposed discipline in place until then.

## 9. Community

A MetaBrainz donation nudge in the README, live from M0 — every self-hosted install
is independent load on the free, donation-funded upstream infrastructure Encore
depends on.

## 10. Legal & compliance

Apache-2.0. Non-goals are published verbatim in `README.md`, where they are the
reviewable copy. No regulated-data compliance regime applies (no health,
financial, or minor's data); the privacy posture is governed by the DPIA in
`docs/RESPONSIBLE-TECH-AUDITS.md` and `docs/audits/dpia.md`, not by an external
statute.

## 11. Operations & sustainability

### Observability

**Observability tier: A** — a running, self-hosted service rather than a CLI or
library, which is the whole of the justification and needs no source outside this
repository. `/livez` and
`/readyz` exist today (`src/encore/app.py`); `readyz` performs a real database
probe since F0 (M1, 2026-07-11) and carries the scheduler-heartbeat check for
all four schedulers since the F2 match job landed (sync, match, watch,
notify — a started-then-dead scheduler is unready; a disabled or
credential-gated one is idle, not a failure). Structured JSON logs with secret/PII redaction are active; **RED metrics per route landed 2026-08-22**
(`encore.metrics`: in-process registry, `/metrics` exposition, route-template
labels — the debt recorded here as "due" is paid); `slos/encore.yaml` (poll-freshness SLO) are specified now and
instantiated as the routes and pollers they measure land (M1–M2) — see
`slos/encore.yaml` for the declared target, schema-validated on every `make verify`
(`make slo-check`, OBS-14).

## 12. Responsible-tech summary

Full audits A–F in `docs/RESPONSIBLE-TECH-AUDITS.md`. One-line summary: Encore
treats music taste as sensitive-inference data, not just preference data, and is
designed so that holding a Plex-connected instance never becomes a way to out
someone sharing that Plex server.
