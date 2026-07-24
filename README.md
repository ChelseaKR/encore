# encore — a release radar and recommender for your Plex music library

> Your music library already knows what you like. Encore watches the horizon for it.

Encore is a free, self-hosted, notification-first companion for Plex music libraries.
It reads the artists you already have, matches them to MusicBrainz, alerts you — by
ntfy, email, Discord, RSS, or iCal — when any of them release something new, and
recommends adjacent artists via ListenBrainz. It never downloads anything.

**Status:** pre-alpha · `M1` in progress (F0 storage layer, F1 read-only Plex
library sync, and F2 MusicBrainz matching engine landed; sync does not feed the
matcher automatically yet) · independent personal open-source project ·
Apache-2.0 · unaffiliated with any employer or client; contains no proprietary or
client material.

## Quickstart (developing)

```sh
make install   # uv sync --frozen --all-extras --group dev
make verify    # the full merge gate: format+lint, type, test+coverage, security, todo-gate
make serve     # run the dev server
```

Running the actual product (the GHCR image publishes at M4 — until then, build the
image locally with `docker build`):

```sh
docker run -v encore-data:/data -p 8321:8321 ghcr.io/chelseakr/encore
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
- **Recommends** similar artists via ListenBrainz labs, with visible provenance
  ("similar to X, Y you already own"), and — the synthesis feature — watches for new
  releases *from artists you don't have yet*.

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
│   ├── cli.py                 # console_scripts entry point: encore = "encore.cli:main"
│   ├── storage.py             # SQLite (WAL) + migrations + data directory (F0)
│   ├── models.py              # SQLModel tables — settings, artists inventory, artist matches
│   ├── secretstore.py         # Fernet secrets-at-rest cipher (docs/adr/0008)
│   ├── plex/                  # read-only Plex client wrapper (F1, docs/adr/0007)
│   ├── sync.py                # F1 library sync: inventory, upsert, tombstone
│   ├── scheduler.py           # background sync scheduler (daily default, off w/o creds)
│   ├── matching/              # MusicBrainz matching + review queue (F2)
│   │                          #   client/scorer/engine + artist_matches cache
│   ├── watch/                  # release-group polling + diffing (F3)               [M2]
│   ├── notify/                  # Apprise fan-out, RSS/iCal feeds (F4, F5)           [M2]
│   └── recommend/              # ListenBrainz labs similar-artists (F7, F8)          [M3]
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
| CI/CD | ✅ | Single `ci.yml`, ordered stages; least-privilege tokens; SHA-pinned actions; Harden-Runner (audit mode) on every workflow; `make verify` is the literal command CI and `release.yml` run, not a parallel reimplementation |
| Security & Supply-Chain | ✅ | ASVS **L2** (holds a Plex token + taste data) — pinned Semgrep (`p/default`, `p/python`, custom no-sensitive-log rule; zero waivers), pip-audit + osv-scanner + gitleaks (locked env, pre-commit + CI + weekly full-history TruffleHog sweep), CodeQL (python + actions), zizmor, Trivy on every container build; cosign+SBOM **at first tagged release (M4)** |
| Release & Versioning | ✅ | SemVer; signed tags (from M4, first release); Keep-a-Changelog; GHCR by digest, never `:latest` |
| Accessibility | **Applies from M2** | WCAG 2.2 AA — **N/A — no UI surface today**; becomes merge-blocking at M2 with the first real UI |
| Observability | ✅ | Tier A (running service) — `/livez`+`/readyz` today; structured JSON logs + PII/secret redaction **land at M1–M2** with the routes/pollers they measure (`docs/ROADMAP.md` §11) |
| Internationalization | **N/A — no user-facing strings at M0** | Gettext seam activates at M2 when the first user-facing string ships — see [`docs/I18N.md`](docs/I18N.md) |
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
exceptions to "local-first." Your Plex token and any Apprise URLs are encrypted at
rest. Nothing is sent anywhere else, ever. Full data inventory and threat model in
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
