# 2. Poll Plex; never rely on webhooks

## Status

Accepted

## Context

Plex supports server-side webhooks that fire on library events, which would be the
obviously cheaper integration for detecting library changes. Plex webhooks are a
**Plex Pass** (paid) feature. "No Plex Pass required" is a hard constraint
(`encore-plans/02-positioning.md` §non-goals) — the whole audience thesis is
self-hosted Plex users who may not have or want a subscription, and Plex's own 2025
pricing changes are precedent that entitlement-gated APIs can shift or disappear
under a free-tier user with no notice (R3,
`encore-plans/08-risks-and-counters.md`).

## Decision

Encore's Plex integration is poll-only: a scheduled job (APScheduler) fetches the
artist inventory over the local, token-authenticated API on a cadence (daily plus
on-demand), never registering for or depending on Plex webhooks.

## Consequences

- Library changes on Plex are detected with up to one poll-cycle of latency
  (configurable), not instantly. This is an acceptable, disclosed trade — the
  product's promise is about *release* alerts, not library-change alerts, and
  release polling already runs on its own cadence.
- No feature may ever be gated behind a Plex Pass capability; this is enforced by
  review (CODEOWNERS on `src/encore/plex/`), not currently by a mechanical test,
  since there is no reliable way to assert "no webhook registration ever happened"
  other than reading the adapter code.
- The Plex adapter interface is designed generically enough (see ADR-0007) that a
  future Jellyfin/Navidrome adapter (F12) can also be poll-based without redesign,
  since neither of those systems' free tiers has an equivalent webhook gate to worry
  about, but consistency is simpler to maintain than a mixed model.
