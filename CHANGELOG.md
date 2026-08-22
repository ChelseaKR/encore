# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`encore match` and `encore matches` close the F2 CLI gap.** The matching
  engine (`MatchEngine`) and the review-queue storage methods have existed
  since F2 landed, but nothing in the shipped CLI or scheduler ever called
  them — the F2 CHANGELOG entry said so plainly ("nothing calls the engine
  on a schedule until F1's sync loop integrates it"), and until now that
  integration didn't exist anywhere, CLI included. A freshly synced library
  had no path from `encore sync` to a populated `artist_matches` table, so
  `encore watch` always saw zero watched artists. `encore match` runs one
  on-demand matching pass over synced-but-unmatched artists (skip-don't-queue
  on a per-artist MusicBrainz failure, same posture as `encore watch`);
  `encore matches list` shows the review queue with its ranked candidates,
  and `encore matches resolve`/`skip` decide them. A scheduled match job
  (parity with sync/watch/notify) remains open — this closes the on-demand
  path, not the automatic one.

- **Release publication now has a trusted-main control plane.** A read-only
  verifier checks an existing SSH-signed stable tag, signer, main ancestry,
  package version, changelog, and the full gate. The exact verified commit
  produces distributions, SBOM, provenance, and a scanned image; a separate
  checkout-free publisher rechecks the tag object before creating the release.

### Added

- **Listening-history weighting (M3/F9).** The sync now reads each
  artist's lifetime play count from the Plex server it already talks to
  (read-only, no extra requests — `viewCount` rides the inventory
  response) and stores it per artist (`encore artists list` shows plays
  and the normalized 0–1 listening weight). The most-played artist weighs
  1.0; an unplayed or history-less library weighs all-zero, which is the
  documented degrade-to-unweighted signal downstream consumers (the F7
  rec seeder) check for. Deliberately *not* done here: auto-boosting
  notification priority from play counts would be silent magic — F10's
  explicit `--priority instant` tier is the way an artist breaks a digest
  window, and the weighting stays visible and explainable instead.
- **Per-artist and global watch settings — release types, muting, priority
  (M3/F10).** The noise-control keystone, resolved in one new policy layer
  (`encore.artistsettings`) that every consumer shares. **Types:** a
  release-group reaches you only when its MusicBrainz type tags are all
  opted in — albums-only by default (`encore settings default-types` to
  change globally; `encore artists settings --allow-primary/--allow-secondary`
  per artist). A live album needs both `album` and `live`; an unknown
  future MusicBrainz tag blocks conservatively rather than spraying noise.
  **Muting:** `--mute`, `--mute-until YYYY-MM-DD`, or `--unmute` suppresses
  deliveries while events stay recorded — un-muting never replays what it
  silenced (suppressed events settle without a send). **Priority:**
  `--priority instant` breaks digest windows; `digest` waits even on
  instant channels; normal follows each channel's mode. Filtered releases
  still land in `release_groups` so the diff stays exact, counted as
  `filtered=` in watch output; flipping a filter on starts from today,
  never from yesterday's back catalog.
- **A scheduled matching job closes the F2 automatic-path gap.** The F2
  CLI entry above closed the on-demand path and said plainly that "a
  scheduled match job (parity with sync/watch/notify) remains open." It is
  open no longer. A fourth scheduler runs the same `run_matching_pass` as
  `encore match` — one shared pass, so the manual and automatic paths
  cannot drift — over the synced-but-unmatched backlog, daily by default
  (`$ENCORE_MATCH_INTERVAL_HOURS`; `<= 0` disables). No credential gate
  (MusicBrainz is keyless) and no steady-state cost: an artist with *any*
  decision is excluded from the backlog, so a fully matched library costs
  zero outbound requests, while an artist whose pass failed keeps no
  decision and is retried — alone — by the next cycle. `/readyz` carries
  the heartbeat check for all four schedulers.
- Give the pre-UI Accessibility N/A state an explicit reason in the standards
  register so automated conformance does not accept an unexplained exemption.
- **F5 standing feeds — RSS + iCal behind a capability URL (M2, 2026-08-04).**
  `src/encore/feeds/` gives the release radar two subscribe-once surfaces: an
  RSS 2.0 feed of release events (`/feeds/<token>/releases.xml`, newest 100,
  items rendered by the same translated F4 renderer notifications use, with
  the event id as a stable non-permalink `guid` and MusicBrainz's public
  release-group page as the item link) and an iCal feed of upcoming release
  dates (`/feeds/<token>/upcoming.ics`) — announced releases land in the
  user's actual calendar as all-day, `TRANSP:TRANSPARENT` entries whose `UID`
  is the release-group MBID, so a date change *moves* the entry instead of
  duplicating it. Both surfaces share F4's type label
  (`release_type_label`, lifted out of the notification renderer), so a live
  record reads as "Album (Live)" on the calendar and in the feed rather than
  drifting into a plain "Album" on one of them. The feeds are taste data, so the URL is the auth
  (docs/adr/0013): an unguessable token, minted lazily by `encore feeds show`,
  **Fernet-encrypted at rest** like the Plex token (migration v6), compared in
  constant time, revocable at a stroke with `encore feeds rotate`. Every
  unauthorized shape — wrong token, no token minted, storage not open, a key
  that no longer decrypts, a method other than `GET`/`HEAD`, a trailing slash
  — is the byte-identical bare 404 a nonexistent route returns, pinned probe
  by probe by `no_outing` tests against the sentinel artist. Making that true
  rather than merely intended cost three FastAPI defaults: the app publishes
  **no OpenAPI schema, no `/docs` and no `/redoc`** (all three were handing an
  unauthenticated caller the exact gated URL template), answers a feed-path
  method mismatch with 404 instead of `405 + Allow`, and does not redirect
  trailing slashes. Both feeds send `Cache-Control: private, no-store`. The
  calendar **never invents a day**: only
  day-precision MusicBrainz dates (from today forward, watched artists only)
  become VEVENTs; `2027` and `2027-03` announcements stay in the RSS feed
  verbatim. RFC 5545 mechanics (TEXT escaping, 75-octet folding, CRLF) are
  hand-rolled and pinned by tests rather than imported.

  RFC 5545 escaping covers **every control character the TEXT production
  forbids**, not just `\n`: a title carrying `\r\n` used to emit a bare CR
  inside a content line, which any parser that splits on lone CRs — Python's
  own `str.splitlines`, for one — reads as an injected line.

  Scope honesty: **no real reader or calendar client has subscribed yet** —
  feed shape is proven against the RFCs' rules offline, and "Google Calendar
  accepts this" joins the live-service items in the M2 exit soak. The token
  travels in the URL, so whatever the operator puts in front of encore — a
  reverse proxy, the reader's own history — may see it (docs/adr/0013
  §consequences). Encore's own logs never do, and neither does the server it
  ships: `encore serve` runs uvicorn with `access_log=False`, because
  otherwise every feed poll wrote the capability URL to `docker logs` for
  good. Proven by a `no_secrets_in_logs` test that drives a **real running
  uvicorn** and carries a positive control, the previous one having inspected
  a stream the access log never reaches. One token gates both feeds;
  per-subscriber tokens are out of scope at this size.

- **F4 notifications — Apprise fan-out (M2, 2026-08-01).** `src/encore/notify/`
  turns the events F3 records into messages a human receives. Channels are
  Apprise destinations (ntfy, Discord, email, Telegram, Pushover, and the
  generic webhook that stays the published answer to "wire it to my
  downloader"), stored in a new `channels` table with the **URL Fernet-encrypted
  at rest** — an Apprise URL is a credential, and a raw-bytes test proves the
  plaintext never reaches the database file. Fan-out is a materialized
  `deliveries` queue, one row per (event, channel), so a Discord success and an
  email backoff are independent facts rather than one flag on the event
  (docs/adr/0012, migration v5). Failures **retry with bounded exponential
  backoff** (5/10/20/40 minutes, then terminal) and land on the channel row, so
  `encore channels list` shows a dying webhook instead of silence. Two cadences:
  instant (one notification per event) and digest (a rollup per
  `digest_interval_hours`, capped in length, where a digest of one renders as a
  plain notification). Notifications carry every field the plan asks for —
  artist, title, primary+secondary type, MusicBrainz's partial date verbatim,
  a Cover Art Archive link, and an `app.plex.tv` deep link built from the machine
  identifier the sync now learns read-only. Adding a channel **never replays
  history**: only events newer than the channel fan out to it, the
  don't-flood-on-first-contact rule ADR-0011 applies to artists, applied to
  destinations. A third scheduler (`notify-deliver`,
  `$ENCORE_NOTIFY_INTERVAL_MINUTES`, default 15 minutes, coalescing) runs the
  cycle and joins the `/readyz` scheduler checks; `encore notify` runs it on
  demand; `encore channels add|list|remove|enable|disable|test` manages
  destinations, reading the URL from a hidden prompt or stdin and never printing
  it back.
- **The in-app feed, as `encore events` (F4).** The always-works fallback for
  when every channel is broken. It is deliberately a **CLI surface, not an HTTP
  route**: the feed is pure taste data, encore has no authentication until F6
  sets the admin password, and an unauthenticated `/events` on a published
  container port is exactly the household-observer harm the no-outing lens
  exists to prevent (docs/adr/0012, residual-risk RR-04).
- **The i18n seam is live (I18N-02, 2026-08-01).** F4's notification text is the
  project's first user-facing string, so `src/encore/i18n.py` ships with it
  rather than after it: `_()`/`_n()` with named `%(placeholder)s` substitutions,
  catalogs under `src/encore/locales/`, and a committed extraction template
  gated by `make i18n-check` in `make verify` and CI (I18N gate G2-lite). A
  pseudolocale test compiles a catalog at runtime and asserts the renderer picks
  it up — without it, "the seam exists" would be unfalsifiable. Still
  English-only; G7/G6/G5/G3 stay deferred with reasons in `docs/I18N.md`.

  Scope honesty: **no message has been delivered to a real service.** Every path
  is proven offline against a `NotificationSender` seam plus the real Apprise URL
  parser, but "Discord accepted this" needs a Discord webhook and is recorded as
  an open manual gate in `docs/ROADMAP.md` §7, to be closed during the M2 exit
  soak. Cover-art URLs are constructed, never verified — checking would cost a
  request per event, and the link 404s when the archive has no art. Rendering is
  transport-neutral text: no Discord embeds, ntfy priority headers, or HTML
  email. Notification *filtering* (albums-only defaults, per-artist mutes) is
  F10 at M3, so a digest's only volume control today is its length cap. The
  `deliveries` queue is never pruned; retention lands with F15 at M4. RSS and
  iCal feeds (F5) and the onboarding wizard (F6) are still ahead in M2.

- **F3 release watching (M2, 2026-07-31).** `src/encore/watch/`: the MusicBrainz
  release-group poller and diff engine (docs/adr/0001 + new docs/adr/0011).
  The WS/2 client gains a paginated `browse_release_groups` that draws from the
  **same process-global 1 req/s rate limiter** as F2's search — one MetaBrainz
  budget, never two (encore-plans/04's load-bearing math), with `Retry-After`
  honored and a defensive per-artist page cap. New `release_groups` + `events`
  tables (migration v4) record the diff: first poll of an artist **baselines
  the back catalog silently** (no notification flood on day one); after that,
  unseen groups become `new` events, future-dated groups become `upcoming`
  (announcements pierce the baseline — they feed the F5 calendar), and revised
  first-release dates become `date_changed`. Reissues/edition-adds of a seen
  group can never re-alert, by construction. Watched artists = matched
  (`auto`/`manual`) AND still present in Plex — tombstoned artists unwatch on
  the next cycle (F1 acceptance). A second background scheduler (`mb-watch`,
  `$ENCORE_WATCH_INTERVAL_HOURS`, default daily, first run one interval out,
  coalesce-after-downtime — skip-don't-queue, risk R8) runs the cycle;
  `encore watch` runs it on demand; `/readyz` now includes the promised
  scheduler check (a started-then-dead scheduler is unready; disabled/idle is
  not). Per-artist MetaBrainz failures are counted and skipped, never queued.
  `no_outing` marker tests pin that artist names, MBIDs, and release titles
  never reach a log line at any level. Scope honesty: events are recorded, not
  yet delivered (F4 Apprise fan-out and F5 feeds are next); cover-art capture
  (Cover Art Archive) lands with F4's rendering, so no `cover_url` column yet;
  polling is sequential under the shared limiter rather than staggered across
  the day — the limiter, not stagger, is the politeness guarantee; the
  24h-soak / zero-rate-limit-violation acceptance needs a real deployment.

- **F2 MusicBrainz identity matching + review queue (M1, 2026-07-17).**
  `src/encore/matching/`: a polite MusicBrainz WS/2 search client (descriptive
  User-Agent, process-global 1 req/s rate limiter that F3 must reuse,
  `Retry-After` honored with bounded retries, Lucene escaping); a confidence
  scorer (normalized-name exact > alias > bounded fuzzy, MB-score prior,
  type/country hints, Plex-GUID MBID as a bounded score boost only — never a
  review skip); and a cache-first engine that auto-matches at or above the
  threshold and queues everything else for review. Decisions persist in a new
  `artist_matches` table (migration v3) as a permanent cache — one MB query
  per artist unless a re-match is forced — with manual resolve/re-match/skip
  always available. A 22-case known-nasty fixture battery (homonyms,
  diacritics, aliases, typos, tribute traps, one-album artists, empty
  results) gates ≥95% correct terminal decisions and zero wrong auto-matches
  in CI; `no_outing`/`no_secrets_in_logs` marker tests prove artist names,
  MBIDs, and the Plex token never reach a log line or an outbound MB request
  (httpx request-URL logging is deliberately suppressed for this reason).
  Scope honesty: thresholds are provisional until the real-library validation
  spike (roadmap U8) freezes them; the fixture payloads are synthetic
  WS/2-shaped documents, not live recordings; there is no review UI yet (the
  ≤3-clicks fix flow is M2) and nothing calls the engine on a schedule until
  F1's sync loop integrates it.

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

### Security

- **`cryptography` raised to `>=50,<51` (2026-08-04).** The locked 49.0.0 picked
  up GHSA-g6cj-pr64-35w5 (CVSS 8.2, High) after the 2026-08-01 CI run, so both
  `pip-audit` and `osv-scanner` — and therefore `make verify` and CI — were red
  on `main` before this branch touched anything. The floor moved to 50 rather
  than the ceiling merely widening, because everything below it is the
  vulnerable range. This is the library that encrypts the Plex token, Apprise
  URLs, and now the feed token, so it is not a bump to defer. Lock updated with
  `--upgrade-package cryptography`; nothing else moved.

### Changed

- **Audit truth-up for the F5 surface (2026-08-04).** `docs/audits/residual-risk.md`
  activates RR-06 (feed tokens are live bearer credentials) and adds RR-07
  (one token means all-or-nothing revocation); the DPIA inventory gains the
  encrypted feed token and a feed-*content* row naming cloud feed readers and
  calendar providers as recipients; the threat model records the feeds as the
  first inbound surface returning library content; and `docs/ROADMAP.md` §1
  finally says M2 rather than M1, with the a11y rows explaining why a feed
  document is not an a11y surface and the SLO row marking the RED-metrics debt
  due now that HTTP routes exist.
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
