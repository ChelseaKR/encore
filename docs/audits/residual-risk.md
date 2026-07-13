# Residual-risk register

Last reviewed: 2026-07-12. Review cadence: every milestone exit and release.

Encore is self-hosted, but its F0 storage layer already holds a Plex credential
that may grant full server access. These are the risks that remain after current
controls; planned features stay dormant until their activation gates exist.

| ID | Residual risk | Current controls | Likelihood / impact | Owner / next review |
|---|---|---|---|---|
| RR-01 | A copied database and adjacent Fernet key expose the Plex token. Encryption at rest does not protect an attacker who steals both files or controls the live host. | Key and database are separate files with key mode `0600`; plaintext-token sentinel test; threat boundary stated in ADR 0008 and the DPIA. | Low-Medium / High | Maintainer; M1 exit |
| RR-02 | The configured Plex account may have broader permissions than Encore needs. Plex does not issue a narrower library-read token. | Read-only adapter decision in ADR 0007; no Plex client exists yet; CODEOWNERS review on the future adapter. | Medium / High once F1 activates | Maintainer; before any F1 merge |
| RR-03 | Future logs or diagnostics could reveal a credential or inference-rich taste data. | No product telemetry endpoint; gitleaks/TruffleHog; blocking Semgrep community packs plus the local sensitive-log rule; empty waiver ledger. | Low / High | Maintainer; every release |
| RR-04 | Future notifications or feeds could expose a household member's music interests to a shared destination. | Feature not active; no-outing/sentinel tripwire is a required F1/M2 gate; user-selected destinations and rotatable feed tokens are documented design requirements. | Medium / Medium-High once activated | Maintainer; before notification/feed merge |

No risk above is accepted as permission to weaken the read-only, no-outing, or
no-secret-in-logs invariants. A control failure blocks the relevant milestone.
