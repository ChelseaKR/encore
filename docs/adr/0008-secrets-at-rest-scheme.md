# 8. Secrets-at-rest scheme, with an honest threat boundary

## Status

Accepted

## Context

Encore stores two categories of high-sensitivity secret: the Plex token (full
server access) and Apprise notification-channel URLs (many embed their own
credentials — Discord webhook secrets, SMTP passwords). Both live in the same
SQLite file as everything else. A stolen backup copy or a copied disk image should
not trivially hand over these credentials in plaintext.

## Decision

Both the Plex token and Apprise URLs are encrypted at rest with a Fernet key stored
beside the database (not embedded in it). This protects **copies of the data** — a
backup file, a stolen disk image, a misdirected volume snapshot. It explicitly does
**not** protect against a root-level attacker on the live, running host: that
attacker can read the key file directly, and no application-layer encryption scheme
defeats that. The DPIA (`docs/RESPONSIBLE-TECH-AUDITS.md` §C) states this boundary
honestly rather than implying a stronger guarantee than the design provides.

## Consequences

- Losing the Fernet key file (without a backup of it) makes the encrypted secrets
  unrecoverable — the onboarding/backup docs must tell users to back up the key
  alongside the database, not instead of it.
- This is declared **ASVS L2** posture (identity-adjacent secret handling,
  `encore-plans/05-standards-alignment.md` §security), which brings the full
  SEC-07/08/11/13/17-19/27-29/37 gate battery, not the lighter L1 bar a purely
  local, no-credential tool would carry.
- If Encore ever grows a hosted or multi-tenant mode (F13 and beyond), this ADR's
  boundary statement ("protects copies, not a live root attacker") stops being
  sufficient and a new ADR is required before that mode ships — a hosted service
  holding other people's Plex tokens is a materially different threat model.
