# encore — a release radar and recommender for your Plex music library

> Your music library already knows what you like. Encore watches the horizon for it.

Encore is a free, self-hosted, notification-first companion for Plex music libraries.
It reads the artists you already have, matches them to MusicBrainz, alerts you — by
ntfy, email, Discord, RSS, or iCal — when any of them release something new, and
recommends adjacent artists via ListenBrainz. It never downloads anything.

**Status:** pre-alpha · `M2` in progress · independent personal open-source
project · Apache-2.0 · unaffiliated with any employer or client; contains no
proprietary or client material.

Which features have landed and which have not is tracked in exactly one place —
[`docs/ROADMAP.md`](docs/ROADMAP.md) §1 (snapshot) and §8 (milestones), with the
per-feature detail in [`CHANGELOG.md`](CHANGELOG.md). This README deliberately does
not re-list it: a second copy of that list is a copy that goes stale one merge later,
which is exactly what happened to the one that used to sit here.

## Quickstart (developing)

```sh
make install   # uv sync --locked --all-extras --group dev
make verify    # the whole merge gate; the Makefile's `verify` target lists the stages
make serve     # run the dev server
```

Running the actual product. **`ghcr.io/chelseakr/encore` does not exist yet** — the
GHCR image publishes at M4 (`docs/ROADMAP.md` §8), and pulling it today fails. Until
then the image is built locally, which is the command below:

```sh
docker build -t encore .
docker run -v encore-data:/data -p 8321:8321 encore

# At M4 this becomes a pull, and the build step goes away:
#   docker run -v encore-data:/data -p 8321:8321 ghcr.io/chelseakr/encore
```

## Why this exists

Several tools sync with Plex, alert on new releases, or recommend music — SoulSync,
MusicSeerr, Lidarr, Muspy, Explo, Lidify, ListenBrainz itself. None does all three
*and* stays legal-and-light: the tools that combine sync + alerts + recommendations
are built around downloading music (Soulseek, Lidarr-coupling), which drags in
piracy-adjacent infrastructure and a fundamentally different product identity.
Encore is notification-first and acquisition-never — a position that's fully
recommendable in public without a legality asterisk.

## What it does

- **Syncs** your Plex music library's artist inventory over the local API (no Plex
  Pass, read-only, no writes ever).
- **Matches** each artist to MusicBrainz with a confidence score; low-confidence
  matches go to a review queue instead of guessing.
- **Watches** for new release-groups and alerts through Apprise (ntfy, email,
  Discord, Telegram, Pushover, webhooks — ~90 services via one dependency), plus an
  in-app feed, RSS, and an iCal feed of upcoming release dates.
- **Stays quiet by default**: albums-only until you opt in to EPs, singles, live
  recordings, or compilations — globally or per artist (`encore artists settings`),
  with muting (forever or until a date) and per-artist priority tiers.
- **Recommends** similar artists via ListenBrainz labs, weighted by your actual
  listening, with visible provenance ("similar to X, Y you already own") and one-command
  dismiss/promote (`encore recommend`, `encore recommendations`). Promoting a candidate
  watches its new releases through the same alert pipeline — discovery is strictly
  opt-in.

## Non-goals (hard, not aspirational)

- **Never downloads music.** No Soulseek, no indexers, no YouTube ripping, no Lidarr
  coupling in-product. At most: standard outbound webhooks on new-release events so
  *other* tools can subscribe — Encore's responsibility ends at the notification.
- **Not a media server or player.** No streaming, no in-app playlists — deep-link out
  to Plex/Plexamp instead.
- **Not a cloud service.** Self-hosted only. No accounts, no telemetry, no
  phone-home; outbound calls only to the metadata APIs the user benefits from.
- **Not a metadata editor.** Reads Plex; never writes to Plex or to files.
- **No Plex Pass required.** Every feature works on a free Plex account (poll, never
  webhook — webhooks are a Plex Pass feature).

## Architecture

```
encore/
├── README.md
├── src/encore/
│   ├── app.py                 # FastAPI app factory — health endpoints today,
│   │                          #   the htmx UI + JSON API as features land
│   ├── metrics.py             # in-process RED metrics registry + /metrics text
│   │                          #   exposition; route templates, never raw paths (OBS-11)
│   ├── cli.py                 # console_scripts entry point: encore = "encore.cli:main"
│   ├── storage.py             # SQLite (WAL) + migrations + data directory (F0)
│   ├── models.py              # SQLModel tables — settings, artists, matches, releases,
│   │                          #   events, notification channels, delivery queue
│   ├── secretstore.py         # Fernet secrets-at-rest cipher (docs/adr/0008)
│   ├── plex/                  # read-only Plex client wrapper (F1, docs/adr/0007)
│   ├── sync.py                # F1 library sync: inventory, upsert, tombstone
│   ├── i18n.py                # the gettext seam every user-facing string routes through
│   ├── scheduler.py           # background schedulers: Plex sync, MB release watch,
│   │                          #   notification delivery
│   ├── matching/              # MusicBrainz matching + review queue (F2)
│   │                          #   client/scorer/engine + artist_matches cache
│   ├── watch/                 # F3 release watching: poll release-groups, diff,
│   │                          #   baseline-seed, emit new/upcoming/date_changed events
│   ├── notify/                # F4 notifications: render events, Apprise fan-out,
│   │                          #   retry/backoff, instant + digest cadences
│   ├── feeds/                 # F5 standing feeds: RSS release feed + iCal of
│   │                          #   upcoming dates, behind a rotatable token URL
│   ├── artistsettings.py      # F10 watch policy every consumer reads: release
│   │                          #   types, muting, per-artist and global priority
│   └── recommend/             # F7/F8 recommendations: ListenBrainz labs
│                              #   similar-artists with provenance, promote/dismiss
├── docs/                       # ADRs, ROADMAP, RESPONSIBLE-TECH-AUDITS, I18N, audits/
├── slos/                       # SLO declarations (Tier A — this is a running service)
├── tests/
└── Dockerfile                  # single OCI image; the whole install is one `docker run`
```

Full technical plan (data model, the sync/watch and recommend pipelines, the
MusicBrainz rate budget) lives in the ADRs under `docs/adr/`.

## Back up and restore `/data`

Treat the entire data directory as one consistency unit: it contains the SQLite
database, its WAL files when active, and `encore.key`. Stop the Encore process or
container before copying or snapshotting it, then copy **all of `/data`** with your
normal volume-backup tooling before restarting the service. A live copy of only
`encore.db` can be inconsistent, and a database copied without its matching key is
intentionally unrecoverable.

Restore the complete fileset from the same backup while Encore is stopped, keep
`encore.key` owned by the service account with mode `0600`, and only then start the
service. Never mix a database from one backup with a key from another; startup will
fail closed rather than create a replacement key.

A whole-volume backup contains both ciphertext and the key that decrypts it.
Fernet therefore does **not** protect that backup from disclosure: encrypt and
access-control backup media as high-sensitivity secret material. Encryption at
rest here protects only a database-only copy whose companion key remains separately
protected.

## Standards conformance

This repo references the portfolio's private engineering standards rather than
restating them; they are fetched at CI time (`.github/workflows/standards.yml`),
never committed. Per-repo *values* live in [`docs/ROADMAP.md`](docs/ROADMAP.md) and
[`docs/RESPONSIBLE-TECH-AUDITS.md`](docs/RESPONSIBLE-TECH-AUDITS.md).

| Standard | Applies | This repo's posture |
|---|---|---|
| Code Quality | ✅ | `ruff` (incl. complexity + TODO/suppression gates) + `mypy --strict`; branch coverage ≥85%; src layout; uv + frozen lock |
| CI/CD | ✅ | Single `ci.yml`, ordered stages; least-privilege tokens; SHA-pinned actions; Harden-Runner (audit mode) on every workflow; one Makefile is the gate, not a parallel reimplementation — `release.yml`'s `verify-at-tag` runs the literal `make verify` at the tag, and `ci.yml` calls the same targets split across jobs, so gitleaks can take `fetch-depth: 0` without that clone cost on every matrix leg. The split is not free and this row does not pretend it is: `ci.yml` omits `todo-gate` and `external-refs`, which then run only in local `make verify` and at the tag, and it adds `zizmor` over the workflows, which has no Makefile target. Both asymmetries are gated by `tests/test_published_claims.py` |
| Security & Supply-Chain | ✅ | ASVS **L2** (holds a Plex token + taste data) — pinned Semgrep (`p/default`, `p/python`, custom no-sensitive-log rule; zero waivers), pip-audit + osv-scanner + gitleaks (locked env, pre-commit + CI + weekly full-history TruffleHog sweep), CodeQL (python + actions), zizmor, Trivy on every container build; cosign+SBOM **at first tagged release (M4)** |
| Release & Versioning | ✅ | SemVer; signed tags (from M4, first release); Keep-a-Changelog; GHCR by digest, never `:latest` |
| Accessibility | **Applies from M2** | WCAG 2.2 AA — **N/A — no UI surface today**; becomes merge-blocking at M2 with the first real UI |
| Observability | ✅ | Tier A (running service) — `/livez`+`/readyz` today; structured JSON logs + PII/secret redaction **land at M1–M2** with the routes/pollers they measure (`docs/ROADMAP.md` §11) |
| Internationalization | **Seam live, English-only** | The gettext seam (`src/encore/i18n.py`) went live with F4's notification strings — the project's first user-facing text — with the extraction template gated in `make verify`. No second catalog ships yet; see [`docs/I18N.md`](docs/I18N.md) |
| AI Evaluation | **N/A — no LLM/model** | Flips to Applies if F14 ("vibe" recs) ever lands; accepted decision in ADR-0009 |
| Quality & Metrics | ✅ | Metrics ledger in `docs/ROADMAP.md`; `make verify` reproduces the CI gate set |
| Documentation | ✅ | This README + ADRs + ROADMAP + RESPONSIBLE-TECH-AUDITS + CHANGELOG, kept current |
| Responsible-Tech Framework | ✅ | Audits A–F in `docs/RESPONSIBLE-TECH-AUDITS.md`; DPIA in [`docs/audits/dpia.md`](docs/audits/dpia.md) |

No standard is a bare `N/A`: Internationalization names its M2 activation trigger, and
AI Evaluation carries both a reason and an accepted ADR.

## Privacy, in one paragraph

Encore keeps everything in its own SQLite file on your own disk. It talks to
MusicBrainz and ListenBrainz (your artist names and MBIDs leave your machine, tied to
your IP) and to whatever notification channel you configure (release titles and
artist names go wherever you pointed it) — both are disclosure choices, not
exceptions to "local-first." The RSS/iCal feed URLs carry an unguessable, rotatable
token, because the feed *is* your taste data — sharing the URL is sharing that.
Your Plex token, any Apprise URLs, and the feed token are encrypted at rest.
Nothing is sent anywhere else, ever. Full data inventory and threat model in
[`docs/RESPONSIBLE-TECH-AUDITS.md`](docs/RESPONSIBLE-TECH-AUDITS.md) and the
[DPIA](docs/audits/dpia.md).

MusicBrainz and ListenBrainz are free, donation-funded infrastructure run by the
MetaBrainz Foundation. If Encore is useful to you, please consider
[donating to MetaBrainz](https://metabrainz.org/donate) — every self-hosted install
is independent load on a project that isn't charging you for it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the merge gate, commit style, and ADR
process, and [`SECURITY.md`](SECURITY.md) to report a vulnerability. Supported
versions (REL-24): `main` and the latest tag only, pre-1.0 — see
[`SECURITY.md`](SECURITY.md#supported-versions) for the table.

## License

[Apache-2.0](LICENSE).
