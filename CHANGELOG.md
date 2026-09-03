# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`make security` could not fail on a dependency advisory, and on `main` it was
  auditing Enthought's package as this project.** The dependency step was a bare
  `uv run pip-audit` over the installed environment. Without `--strict`, a
  distribution pip-audit cannot resolve is *skipped* with a note and the exit code
  stays 0, so a step whose one job is to fail on findings had a path to green that
  did not depend on them. The installed environment also includes this project's
  own editable distribution, and pip-audit resolves a distribution by asking PyPI
  about its name and version. Measured on a copy of `main`, where the distribution
  was still named `encore` 0.1.0: no skip row, `Auditing encore (0.1.0)`, "No
  known vulnerabilities found". PyPI answers 200 for `encore/0.1.0` because
  Enthought published `encore` 0.1 and PEP 440 reads `0.1.0` as the same version;
  the gate was asking whether a stranger's release has advisories, and was green
  because that answer happens to be no. Under the corrected name `encore-plex`
  (above) the same lookup 404s, so adding `--strict` alone fails the gate for the
  wrong reason.

  `make security` now depends on a new `make audit`, which audits the locked
  third-party set exported from `uv.lock`: `uv export --locked --no-emit-project`
  with the same selectors as `make install`, then `pip-audit --strict
  --require-hashes`. This project is never looked up, and every one of the 80 real
  dependencies fails closed, hashes included. Same shape as chalkline's
  `make audit`. Proven able to fail: a scratch copy with `jinja2==3.1.3` pinned
  exits 1 with four advisories (PYSEC-2026-1471, -1472, -1474, -1475). CI calls
  `make security` by name, so no workflow changed.

- **The distribution name was Enthought's, and `__version__` was reading it.**
  `pyproject.toml` declared `name = "encore"`. That name has belonged to Enthought,
  Inc. on PyPI since long before this project — `encore` 0.8.0, "Low-level core
  modules for building Python applications"
  (<http://docs.enthought.com/encore/>) — so `make wheel` was building an artifact
  that could never be published, and any install instruction written around the
  name would have sent a reader to Enthought's package. The distribution is now
  `encore-plex`, verified free on PyPI (HTTP 404 from the JSON API, 2026-09-01),
  following the descriptive-suffix pattern this portfolio already uses for the same
  collision class (`gauntlet-evals`, `cairn-assistant`, `nearmiss-safety`,
  `plumbline-eval`). Nothing has been published to or claimed on PyPI; this is a
  source-only correction.

  The collision was not purely latent. `src/encore/__init__.py` single-sources
  `__version__` from installed distribution metadata via `version("encore")` — the
  *distribution* name, not the import name — so in any environment that also had
  Enthought's `encore` installed, this project would have reported `0.8.0` as its
  own version, in `/healthz`, in logs, and in the MusicBrainz User-Agent this
  project is required to identify itself with. It now reads `version("encore-plex")`.

  The import package stays `src/encore/`, and so do the console script, the
  container entrypoint, `OTEL_SERVICE_NAME`, the `encore-data` volume, the
  `ghcr.io/chelseakr/encore` image and the project's own name. A distribution name
  and an import name are allowed to differ, and only the distribution collided.

- **The README's install command named an image that does not exist.** The
  Quickstart's runnable block was `docker run … ghcr.io/chelseakr/encore`. The
  surrounding prose did say the GHCR image publishes at M4, but the command inside
  the fence is the part a reader copies, and it fails: `ghcr.io/chelseakr/encore`
  is unpublished (`gh api users/ChelseaKR/packages/container/encore` → 404, and an
  anonymous registry pull is denied). The block now runs `docker build -t encore .`
  followed by `docker run … encore`, which works today, and keeps the GHCR line as
  a commented-out note of what it becomes at M4.

- **The roadmap sent readers to a directory that is in no repository, and
  quietly failed to notice when the pointer count went to zero (issue #22).**
  `docs/ROADMAP.md` made seven references to an earlier planning corpus and
  `docs/RESPONSIBLE-TECH-AUDITS.md` three more — a plain local directory on one
  machine, not a git repository, not a submodule, never in this repository's
  history. Two were load-bearing: §3 delegated the F1–F14 feature plan, and §8
  said the M0–M4 exit criteria were "specified in full" elsewhere, which meant
  the criteria deciding whether a milestone is done were not reviewable in the
  repository claiming to have met them. Both documents are now at zero
  references and are pinned there, resolved per document rather than in bulk:

  - **§3 reconstructs F0–F14 from the tree.** Every row, including the four
    parked ones, cites the committed file it was read off — F11 from the
    responsible-tech data inventory, F12 from ADR-0007 and the Definition of
    Done, F13 from ADR-0005's relaxation rule, F14 from ADR-0009. The first
    draft of this section asserted that F11–F13 were "not named anywhere in
    this repository"; they are, and
    `tests/test_external_refs_gate.py::test_every_feature_row_is_backed_by_the_file_it_cites`
    exists because writing down what a document *lacks* is exactly as
    falsifiable, and exactly as capable of being wrong, as writing down what it
    has. What genuinely did not survive is the *ranking*: the ordering rationale
    and the cut list are not reconstructible, and the section says so instead of
    implying it is a prioritization.
  - **§4 is relabelled a premise, not research.** Its market claim — no free
    tool combines Plex-native sync, release alerts and recommendations without
    being built around downloading — rested entirely on an unpublished scan, so
    its per-claim verification statuses and its `2026-07-05` currency stamp
    could not be checked from a clone. A verification status nobody can check is
    a claim, not a finding. The section now records what was believed and by
    when, and claims nothing about whether it still holds.
  - **§8 owns the exit criteria** rather than deferring to a fuller copy
    elsewhere, and §1, §2, §6, §10, §11 and the audits' threat model either
    carry their content or say plainly it is not published here. Nothing was
    invented to fill a gap.

  The ratchet itself had a blind spot in the direction it protects: `_scan`
  `continue`d the moment a file's count hit zero, so a ledger entry left at its
  old ceiling was never reported, and an entry for a file git no longer tracks
  was never visited at all. Either one silently pre-authorizes references nobody
  reviewed. The gate now reports both, and immediately caught a ceiling for
  `docs/plans/improvement-plan.md`, a file that has never been committed.
  Absence of an entry already means zero, so the fix is always to delete the
  line — asserted, along with "no entry is written as 0".

  Remaining under #22, and left deliberately: 29 references in the ADR set, the
  Definition of Done, `docs/I18N.md`, this file, and six source docstrings.
  Those are dated records citing the draft they were reasoned from, which is a
  different disposition question from a live document telling a reader to go
  and open it.

- **The anti-drift doc audit was itself a source of drift: it counted untracked
  files.** Adding the test module above surfaced it. `scripts/doc_audit.py`
  enumerated with `ROOT.rglob("*.md")` and `Path.glob`, so a contributor's
  untracked scratch note under `docs/` joined the inventory and moved
  "Hand-authored docs" and "other docs" — on that machine only. CI regenerates
  from a clean checkout, so `make docs-audit-check` disagreed with itself across
  two checkouts of the *same commit*, in the one gate whose whole purpose is
  byte-equality with what the commit produces. `EXCLUDED_DIR_NAMES` was the
  hand-maintained defence and could only ever list the build artifacts someone
  had already been bitten by; an arbitrary untracked file was never in reach.

  `tests/test_doc_audit.py::test_every_authored_doc_is_tracked_by_git` had
  asserted this invariant since #41, and it was **red on this working tree**
  before the change — a genuine failure that only appears when someone keeps a
  local note, which is why it survived review. Every collector now enumerates
  from `git ls-files` and fails closed when git cannot answer, rather than
  falling back to a walk that silently resumes answering the different question.
  `test_a_stray_untracked_markdown_file_cannot_move_the_counts` plants one and
  proves `render()` is byte-identical, so the invariant no longer depends on the
  developer's disk being tidy. The module docstring's "identical tree, identical
  bytes" is corrected to identical *commit*, which is the property `--check`
  actually needs.

- **The pass above fixed five documents that overclaimed `make verify`/CI
  equivalence. One sentence survived it.** The README's CI/CD row still read
  "`make verify` is the literal command CI and `release.yml` run, not a parallel
  reimplementation". `grep -rn "make verify" .github/workflows/*.yml` finds
  `run: make verify` at exactly one line, `release.yml:121`; `ci.yml` does not
  run it and says so itself in its own header comment. Nor is the difference
  only a split for `fetch-depth: 0`: `ci.yml` reaches ten of the twelve targets
  `verify` composes and never `todo-gate` or `external-refs`, which therefore
  run in local `make verify` and at the tag but not on a pull request; and it
  runs one gate with no Makefile target at all, `zizmor` over the workflows. The
  row now states both asymmetries. `tests/test_published_claims.py` derives them
  from the workflows and the Makefile rather than trusting the sentence: it
  fails if a workflow other than the one the README names runs the literal
  command, if the set of targets `ci.yml` omits stops matching the set the
  README lists, or if CI ever runs a target `make verify` does not compose.
  (Whether `todo-gate` and `external-refs` should join `ci.yml` is a separate
  decision; this change reports the gap rather than closing it.)

- **The README's status line was four features stale, and nothing in the repo
  could tell it.** It read "F0 … F5 landed; F6 onboarding wizard remains",
  accurate the day it was written and wrong from #27 onward: `git log` shows F10
  (#27), F9 (#28), F7 (#29) and F8 (#30) all merged 2026-08-22,
  `src/encore/recommend/` and `src/encore/artistsettings.py` ship with tests,
  `encore recommend` and `encore recommendations` are registered CLI commands,
  and this file documents all four. F6 does genuinely remain — every occurrence
  of "wizard" under `src/` is a forward reference — so the correction is
  downward on M3, not upward on M2. The enumeration is gone rather than
  corrected, because a corrected copy goes stale one merge later: feature status
  lives in `docs/ROADMAP.md` §1 and §8 and in this file, the README points
  there, and a test fails if a bare feature id reappears in the status block.
  The architecture tree, the README's other hand-maintained list, gained the two
  modules it never grew (`metrics.py`, `artistsettings.py`), lost the `[M3]`
  marker that made `recommend/` read as future work, and is now derived from
  `src/encore/`. `docs/ROADMAP.md` §8's M3 row, which still read as untouched,
  records what landed and which exit criteria stay unmeasurable until F6 brings
  the first rendered UI.

- **The merge gate now contains the stage that has actually been failing.**
  `ci.yml`'s Stage 9 (docker build, Trivy CVE scan, `/livez` bring-up) existed
  only in the workflow: no Makefile target built or scanned a container, and
  Trivy's severity/exit-code/ignore-unfixed lived as `trivy-action` inputs.
  Five documents nonetheless stated that `make verify` is the CI gate with no
  drifted second implementation. It was not, and the gap was the expensive one:
  Trivy accounts for eight of the last twenty CI failures, so the single stage
  with a real failure history was the one a contributor could not run —
  `make verify` reported "all gates green" on trees CI then rejected on a real
  HIGH CVE. Stage 9 is now `make container-build`/`container-scan`/
  `container-bringup`, composed as `make container-verify`, called by both
  `ci.yml` and `release.yml`, and included in `make verify`. It fails closed
  when `docker` or `trivy` is missing rather than skipping.

- **The lockfile control could not fail (CQ-09).** `make install` ran
  `uv sync --frozen`, which installs straight from `uv.lock` without reading
  `pyproject.toml` and therefore exits 0 on a lock that no longer satisfies the
  manifest; no `uv lock --check` existed anywhere. Measured: a dependency added
  to `pyproject.toml` and absent from `uv.lock` gives `--frozen` exit 0 and
  `--locked` exit 1. Now `--locked`.

- **The custom Semgrep rule's self-test passed with no tests.**
  `semgrep test .semgrep-rules` prints `No unit tests found` as a warning and
  **exits 0**, so deleting the rule's test file — or adding a second rule and
  forgetting one — left `make security` green while proving nothing about the
  repo's only bespoke SAST rule, the one backing the no-secrets-in-logs promise.
  `scripts/semgrep-test-gate.sh` now requires every rule file to have a sibling
  test file and refuses a run that reports no tests.

- **The secret scan could not see an uncommitted secret.** `make security` ran
  `gitleaks detect --source .`, which scans git *history*; a live-shaped AWS key
  sitting in the working tree gave exit 0. CI was covered (it scans a committed
  tree at `fetch-depth: 0`), but the local gate a contributor runs *before*
  committing — the last moment the secret can be stopped — was not, and the
  pre-commit hook only helps if it was installed. A `--no-git` working-tree scan
  now runs alongside the history scan.

- **Gate code is now gated.** `scripts/`, which holds the todo-gate, i18n-check,
  SLO-validator, and the two new gates, was neither linted nor type-checked, and
  no shell script was shellchecked. `make lint` now covers `scripts` with ruff
  and `shellcheck scripts/*.sh`; mypy's scope includes it.

- **A Plex GUID could rescue a near-miss name mismatch past the auto-match
  threshold (#32).** `scoring.py` documented that the +0.15 GUID boost "cannot
  rescue a name mismatch", but the boost was added *after* the fuzzy ceiling,
  so any fuzzy ratio above ~0.882 auto-matched on the GUID alone — a
  one-character difference, a tribute band, or any close-but-wrong entry a
  mis-tagged Plex GUID points at. The existing test paired Radiohead with
  *Coldplay*, so unrelated that it passed either way. The boost now applies only
  where the name component already reached exact or exact-alias, and the fixture
  battery carries the near-miss case.

- **The release-type filter was silently bypassed for promoted artists (#33).**
  `effective_watch_settings_for_mbids` omitted any MBID with no live Plex owner,
  which is precisely what an F8-promoted recommendation is, and `watch_artist`
  read that absence as "no restriction". Every EP, single, live album and
  compilation from a promoted artist fired a real notification while the same
  release from an owned artist was correctly filtered. Unowned identities now
  resolve to the global defaults, so "quiet by default" means the same thing for
  discovered and owned music.

- **The iCal feed showed releases RSS and notifications suppressed (#34).** The
  type filter is enforced at event-creation time, which RSS inherits for free;
  the calendar reads `release_groups` directly — deliberately, so a date change
  moves an entry instead of duplicating it — and so bypassed the filter. A user
  who kept the albums-only default still got singles and EPs on their calendar.
  `list_upcoming_releases` now applies the same policy. Muting is deliberately
  still not applied there: muting suppresses deliveries only.

- **The roadmap said two different things about M1 (#21).** §1 called M1
  complete while §7 and §8 recorded its ≥90% field auto-match criterion as
  unmet. All three now say the same thing: M1 is feature-complete with the U8
  validation spike outstanding, so the auto-match thresholds remain provisional.

### Added

- **A ratchet on references to paths outside the repository (#22).** 39
  committed references point at a sibling planning directory that is not a git
  repository and never has been (issue #22 names it); they resolve to nothing
  for anyone who clones encore, and the two most load-bearing — the M0–M4 exit
  criteria and the F1–F14 feature plan — would go public with the repo at M4. Retiring them is a
  per-document decision for the maintainer; `make external-refs` meanwhile makes
  `.external-refs.yml` a ceiling, so the count can fall freely but cannot grow
  without a deliberate, reviewable edit.

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

- **RED metrics for the HTTP surface — the observability debt, paid
  (OBS-11).** Every request is observed into a thread-safe in-process
  registry (Rate, Errors, Duration count/sum/max) and exposed at
  `/metrics` in Prometheus text format, dependency-free. Labels are
  **route templates, never raw paths** — feed URLs embed a capability
  token in the path, so unmatched requests aggregate under one opaque
  `unmatched` label and a regression test pins that a token-bearing
  request leaves nothing but the template behind. `/metrics` carries no
  taste data (templates, methods, statuses only) and is unauthenticated
  by design on the localhost default bind.
- **Promoted recommendations join the watched library (M3/F8).** Promoting
  a candidate (`encore recommendations promote --mbid …`) is the explicit
  opt-in: its MBID joins the release-watch pool, so its new releases and
  future-dated announcements flow through the same event → channel →
  feed pipeline as owned music, rendered with the candidate's name even
  though it has no Plex row (the calendar includes it too). Dismissing —
  or dismissing after promoting — removes it from the pool again. A
  dismissed or untouched candidate is never watched: discovery stays
  strictly opt-in, capped at exactly the artists you chose.
- **Similar-artist recommendations with visible provenance (M3/F7).** A
  weekly refresh (`$ENCORE_REC_INTERVAL_HOURS`, default 168; fifth
  scheduler with its `/readyz` heartbeat) seeds ListenBrainz labs'
  similar-artists dataset from the watched library — batched fifty MBIDs
  per polite request, scores aggregated across seeds and **weighted by F9
  listening history** (an all-zero play history degrades to unweighted
  seeding). Every candidate carries provenance: which owned artists
  produced it and what each contributed, rendered as "similar to X, Y"
  in `encore recommendations list`. `dismiss` pins a candidate out of all
  future refreshes; `promote` marks it for F8's discovery watch. Owned
  artists are never candidates. The intelligence is MetaBrainz's open
  dataset — no ML in-process, ADR-0009 stays true.
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

- **The image takes Debian's published security updates at build time
  (2026-08-26).** The runtime stage now runs `apt-get upgrade` before dropping
  to the non-root user. `python:3.12-slim` is rebuilt on its own cadence, so
  between a Debian security upload and the next base-image push the tag ships
  packages Debian has already fixed — and because Trivy runs with
  `--ignore-unfixed`, exactly those packages are what turns the SEC-28
  Container CVE scan red. It first went red on `main` on 2026-08-22 with 36
  HIGH findings across the `util-linux` binaries
  (CVE-2026-53612/53613/53614/53615, `mount` TOCTOU and a SUID
  nosuid/noexec bypass) and stayed red for four days. Deliberately not a
  hand-listed set of packages: by 2026-08-26 the base image had picked
  util-linux up on its own and openssl CVE-2026-14456 (QUIC unbounded memory
  growth, HIGH) had taken its place, so a list pinned to the CVE of the week
  is stale before it merges. The image runs as `encore` (uid 10001) exactly
  as before; the Python layer is untouched and stays pinned by `uv.lock`.

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
