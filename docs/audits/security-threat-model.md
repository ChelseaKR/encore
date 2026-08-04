# Security threat model

Last reviewed: 2026-08-04. Review cadence: every milestone exit and release.

## Scope and assets

Encore is a single-tenant, self-hosted service. The attack surface is the
FastAPI/CLI process, SQLite database, adjacent Fernet key, container image,
health endpoints, the outbound MusicBrainz and Apprise clients (F2-F4), and —
since F5 — two token-gated HTTP feed routes, the first inbound surface that
returns library content. The highest-value current asset is the encrypted Plex token;
taste-rich artist data (F1-F3), Apprise destinations (F4), and the RSS/iCal
feed token (F5) have since joined it as live assets; play data (F9) is still
future.

The operator controls the host, reverse proxy, backups, Plex server, and chosen
notification destinations. Encore controls its process, database schema,
outbound client code, logs, and container defaults. There is no Encore-operated
cloud service or telemetry collector.

## Actors and trust boundaries

1. A thief with a copied database or backup, but not necessarily the adjacent
   key file.
2. A household/shared-server observer who can see a notification channel or
   feed and infer another person's music taste.
3. Malicious or compromised upstream content returned by Plex, MusicBrainz,
   ListenBrainz, or a notification integration once those clients exist.
4. A contributor or compromised dependency attempting to exfiltrate secrets,
   expand Plex access beyond read-only operations, or hide a security finding.

Trust boundaries are the mounted `/data` volume, local Plex connection, future
metadata APIs, user-configured notification endpoints, logs/stdout, CI supply
chain, and published container/release artifacts.

## Threats, controls, and activation gates

| Threat | Current control | Residual boundary / activation gate |
|---|---|---|
| Database copied without authorization | Secret-bearing settings are Fernet-encrypted; plaintext sentinel test; key mode `0600` | Copying both database and key, or controlling the live host, remains RR-01; operator backup/host security is required |
| Secret or taste data enters logs | Gitleaks plus pinned Semgrep community packs and a tested sensitive-log rule; inline `nosemgrep` disabled; zero-waiver ledger; since F5, `encore serve` runs uvicorn with `access_log=False` so the capability token in the request path never reaches stdout, pinned by a `no_secrets_in_logs` test against a real running server | Add a planted sentinel-log test before F1 handles real Plex/taste payloads |
| Database corruption or unavailable storage is reported healthy | Ordered migrations, WAL mode, real `/readyz` database probe, storage tests, and stopped-service backup/restore procedure | An automated restore rehearsal and broader recovery guarantees remain milestone work |
| Plex mutation or over-broad client behavior | ADR 0007 fixes the client boundary as read-only; CODEOWNERS covers future adapter files | No Plex client exists yet; an operation allowlist and negative mutation tests block F1 |
| Taste information reaches an unintended person | **Both egress surfaces now exist.** F4 delivers only to per-channel destinations the user added by hand (never defaulted, never aggregated); F5's feeds are reachable only with an unguessable, encrypted-at-rest, rotatable capability token; every unauthorized probe shape — wrong/unminted/undecryptable token, wrong method, trailing slash — is one byte-identical 404, and no OpenAPI schema, `/docs` or `/redoc` is published to hand the gated URL template out unauthenticated (`docs/adr/0013` §Decision 2). `no_outing` marker tests are merge-blocking for both | Accepted as RR-04 (push) and RR-06 (pull): a destination or URL the user chose *is* the feature. The shipped server writes no access log, so residual exposure is the token wherever the URL travels outside encore — a reverse proxy the operator adds, the reader's own history — plus timing and traffic volume, which a status code cannot hide; per-subscriber revocation is unbuilt (RR-07) |
| Unexpected outbound exfiltration | No product outbound clients exist at F0; CI egress is audited with Harden-Runner | A deny/allowlisted HTTP transport and sentinel exfiltration tests block F1/F3/F4 |
| Vulnerable dependency or image | Frozen lock, pip-audit, osv-scanner, Semgrep, CodeQL, gitleaks/TruffleHog, and Trivy on every container build | GitHub jobs remain externally budget-blocked; local gates stay mandatory; signing/SBOM/provenance activate at M4 |
| Security finding hidden by suppression | `--disable-nosem`; empty, reviewed `.semgrep-waivers.yml`; ruff suppression/TODO gates | Any future exception requires a scoped config change, expiry, owner, and ledger entry |

## Explicit non-controls

Encryption at rest is not protection from root on the running host or from a
backup containing both the database and key. `/livez` proves only process
liveness; `/readyz` proves current database access, not scheduler freshness.
Harden-Runner is in audit mode, not deny-by-default. CodeQL and scheduled scans
are configured but cannot execute until the GitHub Actions account budget is
restored. No current control is credited for a future Plex, matching,
notification, feed, or recommendation path before its activation tests land.

## Review triggers

Re-review before merging any Plex or outbound HTTP client, notification/feed
surface, new secret-bearing column, authentication surface, migration that
changes retention, dependency waiver, or release-publication implementation.
Update `docs/audits/residual-risk.md` and the DPIA in the same change whenever
the data inventory or trust boundaries move.
