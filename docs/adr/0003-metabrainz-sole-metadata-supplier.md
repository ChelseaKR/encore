# 3. MetaBrainz (MusicBrainz + ListenBrainz) as the sole metadata supplier

## Status

Accepted

## Context

Spotify's API restrictions in late 2024 broke Lidify's Spotify-based similar-artist
discovery (verified, `encore-plans/01-market-landscape.md`) — a commercial platform
changed its terms and a dependent open-source project lost a feature overnight, with
no recourse. Deezer, Tidal, and Qobuz APIs are used elsewhere in this space
(SoulSync) but are unofficial, keyed, and carry the same rug-pull risk. MetaBrainz
(MusicBrainz for identity/release data, ListenBrainz for recommendations) is
keyless, openly licensed, and has run as donation-funded nonprofit infrastructure
for 20+ years.

## Decision

MusicBrainz is Encore's only source of artist identity, release-group, and release
data. ListenBrainz labs (`similar-artists`, `fresh_releases`) is its only source of
recommendation data. No commercial streaming-platform API (Spotify, Deezer, Tidal,
Qobuz, Apple Music) is used for matching, watching, or recommending.

A keyless, verification-only fallback (e.g., checking Deezer's public catalog to
corroborate a release MusicBrainz hasn't listed yet, R2's counter) is a researched
option for later, and even then it may only *verify*, never *supply*, matching or
recommendation data — it must not become a second dependency this decision was
written to avoid.

## Consequences

- Encore inherits MusicBrainz's real limitation: community-maintained coverage means
  small/Bandcamp-only artists can have late or missing release entries (R2). This is
  disclosed honestly in the UI rather than hidden, and "add the missing release to
  MusicBrainz" is a first-class linked action — turning the limitation into a
  contribution loop that strengthens the commons Encore depends on.
- The global rate limit (1 req/s per IP, MusicBrainz's own published limit) is a
  hard design constraint on every polling job, detailed in
  `encore-plans/04-architecture.md` §external API budget, and on the User-Agent
  policy (R8) protecting shared donation-funded infrastructure from Encore's
  installed base.
- If MetaBrainz ever changes its terms in a way that breaks this, that is a
  portfolio-level event requiring a new ADR, not a quiet substitution — the whole
  positioning thesis in `encore-plans/02-positioning.md` rests on this supplier's
  institutional stability being real.
