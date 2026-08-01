# Residual-risk register

Last reviewed: 2026-08-01 (F4 notifications). Review cadence: every milestone exit and release.

Encore is self-hosted, but its F0 storage layer already holds a Plex credential
that may grant full server access. These are the risks that remain after current
controls; planned features stay dormant until their activation gates exist.

| ID | Residual risk | Current controls | Likelihood / impact | Owner / next review |
|---|---|---|---|---|
| RR-01 | A copied database and adjacent Fernet key expose the Plex token. Encryption at rest does not protect an attacker who steals both files or controls the live host. | Key and database are separate files with key mode `0600`; plaintext-token sentinel test; threat boundary stated in ADR 0008 and the DPIA. | Low-Medium / High | Maintainer; M1 exit |
| RR-02 | The configured Plex account may have broader permissions than Encore needs. Plex does not issue a narrower library-read token. | Read-only adapter decision in ADR 0007; no Plex client exists yet; CODEOWNERS review on the future adapter. | Medium / High once F1 activates | Maintainer; before any F1 merge |
| RR-03 | Future logs or diagnostics could reveal a credential or inference-rich taste data. | No product telemetry endpoint; gitleaks/TruffleHog; blocking Semgrep community packs plus the local sensitive-log rule; empty waiver ledger. | Low / High | Maintainer; every release |
| RR-04 | Notifications can expose a household member's music interests to a shared destination (a family Discord channel, a shared inbox). **Active since F4 (2026-08-01)** — this is a works-as-intended harm, not a bug. | Destinations are user-chosen and per-channel, never defaulted or aggregated; the in-app feed is CLI-only until F6 supplies authentication (`docs/adr/0012`); cover art is a link, so encore adds no fetch of its own; `no_outing`/`no_secrets_in_logs` marker tests pin that notification bodies and channel URLs never reach a log line; the egress is disclosed in the DPIA §3 rather than implied away. | Medium / Medium-High — **accepted and documented**, since delivering to a destination the user picked *is* the feature | Maintainer; M2 exit |
| RR-05 | The `deliveries` queue grows without bound (one row per event per channel) and is never pruned, so an old install accumulates a long record of which release was sent where. | Contains ids, counts, and error strings only — no artist names or titles; lives in the same operator-owned SQLite file as everything else. Retention/pruning is scheduled with the F15 export/wipe work at M4, where the rest of the data-lifecycle story lives. | Low / Low-Medium | Maintainer; M4 (F15) |
| RR-06 | Feed tokens (F5 RSS/iCal) will expose taste data to anyone holding the URL. **Not yet active** — F5 is unbuilt. | Not shipped; rotatable unguessable tokens remain the documented design requirement, and F4's precedent (secrets encrypted at rest, no unauthenticated read surface) is the pattern F5 must follow. | Medium / Medium-High once activated | Maintainer; before any F5 merge |

No risk above is accepted as permission to weaken the read-only, no-outing, or
no-secret-in-logs invariants. A control failure blocks the relevant milestone.
