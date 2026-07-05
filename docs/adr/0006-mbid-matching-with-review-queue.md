# 6. Confidence-scored MBID matching with a human review queue

## Status

Accepted

## Context

Every alert Encore ever sends depends on knowing which MusicBrainz artist a Plex
artist actually is. Music metadata is full of name collisions — homonym bands (there
are multiple distinct artists called "Mogwai"-adjacent names across MusicBrainz),
transliteration variants, and "Various Artists" compilation noise. A wrong match
means alerts for the wrong artist, which breaks the product's one promise (R1,
`encore-plans/08-risks-and-counters.md`, the portfolio's highest-ranked risk for
this project). Plex's own artist GUIDs are not reliably MusicBrainz IDs — they
depend on which metadata agent tagged the library and its history — so they can hint
but cannot be trusted blindly.

## Decision

Each Plex artist is matched against the MusicBrainz search API and scored on name,
alias, type, and country signals (and, if present, boosted — never auto-accepted —
by a Plex-supplied MBID hint). Matches scoring at or above a threshold auto-match;
matches below go to an in-UI **review queue** where the user confirms a candidate or
marks "skip." A confirmed or auto-matched MBID is cached permanently; re-matching is
manual-only, never silently redone by a later sync.

The threshold is **not frozen by this ADR**. `encore-plans/CONTEXT.md` requires a
validation spike against a real library (Chelsea's) before M1 exit — if field
auto-match precision lands below ~90%, the threshold is rebalanced and this ADR is
updated (not silently overridden in code) with the spike's findings.

## Consequences

- The review queue is on the critical path for a good first-run experience (a
  1,000-artist library can produce a meaningful review backlog) — its UI must make a
  correction cheap (≤3 clicks, per F2's acceptance criterion in
  `encore-plans/03-feature-plan.md`), or users will rubber-stamp wrong matches to
  get through it.
- A fixture library of known-nasty cases (homonyms, unicode names, one-album
  artists) lives in CI so matching-logic changes can't silently regress precision on
  the cases most likely to break.
- Everything downstream — watching, notifications, recommendations — inherits this
  layer's quality. Any change to the scoring function is exactly the kind of decision
  ADR-0000 says gets a linked ADR and a CODEOWNER review.
