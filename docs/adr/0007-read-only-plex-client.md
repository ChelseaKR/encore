# 7. Read-only Plex client, enforced mechanically

## Status

Accepted

## Context

The Plex token Encore stores grants whatever access the configured Plex account has
— potentially full server control. Encore's entire relationship to Plex is "read the
artist inventory and play counts"; it has no legitimate reason to ever call a
mutating endpoint, and a bug or a future feature that accidentally does so would be
a serious trust violation for a tool whose pitch is "point it at your server and
walk away."

## Decision

All Plex access goes through a thin adapter (`src/encore/plex/`) that wraps
`python-plexapi` and exposes only read operations. The wrapper is the *only* module
permitted to import `plexapi` directly (enforced by review via CODEOWNERS, and by a
unit test that asserts the wrapper's public interface contains no mutating verb —
`tests/test_read_only_plex.py`, the `read_only_plex` pytest marker).

## Consequences

- A future Jellyfin/Navidrome adapter (F12) implements the same read-only interface,
  so the constraint generalizes rather than needing to be re-derived per backend.
- README's non-goals state this plainly ("reads Plex; never writes to Plex or to
  files") as a user-facing guarantee, not just an internal implementation detail —
  it's part of why a wary self-hoster can trust the token they're asked to paste in.
- The recommended deployment pattern is a dedicated, least-privilege managed Plex
  user for Encore rather than the account owner's own token (documented in the
  onboarding wizard, F6) — defense in depth even though the wrapper itself never
  requests write scope.
