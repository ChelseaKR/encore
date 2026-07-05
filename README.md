# encore — a release radar and recommender for your Plex music library

> Your music library already knows what you like. Encore watches the horizon for it.

Encore is a free, self-hosted, notification-first companion for Plex music libraries.
It reads the artists you already have, matches them to MusicBrainz, alerts you — by
ntfy, email, Discord, RSS, or iCal — when any of them release something new, and
recommends adjacent artists via ListenBrainz. It never downloads anything.

**Status:** pre-alpha · scaffold (`M0`) · independent personal open-source project ·
Apache-2.0 · unaffiliated with any employer or client; contains no proprietary or
client material.

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
│   ├── plex/                  # read-only Plex client wrapper (F1)                 [M1]
│   ├── matching/               # MusicBrainz identity matching + review queue (F2)  [M1]
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

## Getting started (developing)

```sh
make install   # uv sync --all-extras --frozen
make verify    # the full merge gate: format+lint, type, test+coverage, security
make serve     # run the dev server
```

Running the actual product (once Plex sync lands, M1+):

```sh
docker run -v encore-data:/data -p 8321:8321 ghcr.io/chelseakr/encore
```

## Standards conformance

This repo references the portfolio's private engineering standards rather than
restating them; they are fetched at CI time (`.github/workflows/standards.yml`),
never committed. Per-repo *values* live in [`docs/ROADMAP.md`](docs/ROADMAP.md) and
[`docs/RESPONSIBLE-TECH-AUDITS.md`](docs/RESPONSIBLE-TECH-AUDITS.md).

| Standard | Applies | This repo's posture |
|---|---|---|
| Code Quality | ✅ | `ruff` + `mypy --strict`; branch coverage ≥85%; src layout; uv + frozen lock |
| CI/CD | ✅ | Single `ci.yml`, ordered stages; least-privilege tokens; SHA-pinned actions; `make verify` = CI |
| Security & Supply Chain | ✅ | ASVS **L2** (holds a Plex token + taste data) — pip-audit, gitleaks, CodeQL, cosign+SBOM on release |
| Release & Versioning | ✅ | SemVer; signed tags; Keep-a-Changelog; GHCR by digest, never `:latest` |
| Accessibility | ✅ | WCAG 2.2 AA — **N/A-with-reason at M0**: no UI surface exists yet; goes merge-blocking at M2 (first real UI) |
| Observability | ✅ | Tier A (running service) — `/livez`+`/readyz`, structured JSON logs, PII/secret redaction |
| Internationalization | ✅ | Catalog infra from day 1; **English-only at launch** — see [`docs/I18N.md`](docs/I18N.md) |
| AI Evaluation | **N/A** | No LLM/model in the product — flips to Applies if F14 ("vibe" recs) ever lands |
| Quality & Metrics | ✅ | Metrics ledger in `docs/ROADMAP.md`; `make verify` reproduces the CI gate set |
| Documentation | ✅ | This README + ADRs + ROADMAP + RESPONSIBLE-TECH-AUDITS + CHANGELOG, kept current |
| Responsible Tech | ✅ | Audits A–F in `docs/RESPONSIBLE-TECH-AUDITS.md`; DPIA in `docs/audits/dpia.md` |

No standard is a bare `N/A` — the one that is (AI Evaluation) carries its reason above.

## Privacy, in one paragraph

Encore keeps everything in its own SQLite file on your own disk. It talks to
MusicBrainz and ListenBrainz (your artist names and MBIDs leave your machine, tied to
your IP) and to whatever notification channel you configure (release titles and
artist names go wherever you pointed it) — both are disclosure choices, not
exceptions to "local-first." Your Plex token and any Apprise URLs are encrypted at
rest. Nothing is sent anywhere else, ever. Full data inventory and threat model in
[`docs/RESPONSIBLE-TECH-AUDITS.md`](docs/RESPONSIBLE-TECH-AUDITS.md) and
`docs/audits/dpia.md`.

MusicBrainz and ListenBrainz are free, donation-funded infrastructure run by the
MetaBrainz Foundation. If Encore is useful to you, please consider
[donating to MetaBrainz](https://metabrainz.org/donate) — every self-hosted install
is independent load on a project that isn't charging you for it.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the merge gate, commit style, and ADR
process, and [`SECURITY.md`](SECURITY.md) to report a vulnerability.

## License

[Apache-2.0](LICENSE).
