# 1. Watch release-groups, not releases

## Status

Accepted

## Context

MusicBrainz models a musical work at two levels: a **release-group** (the abstract
"album," e.g. *OK Computer*) and a **release** (a specific edition — a 1997 CD, a
2017 remaster, a Japanese pressing with two bonus tracks). A popular album can have
a dozen or more release entries. Encore's whole promise is "tell me when an artist
puts out something new" — if it watched at the release level, a single new album
would fire once per edition as reissues and regional variants trickle in over
months, and the product would train users to mute it.

## Decision

Encore polls and diffs MusicBrainz **release-groups** per matched artist, not
individual releases. A new release-group (by MBID) is a new event. Additional
release entries added to an already-seen release-group (a reissue, a remaster, a
regional edition) do **not** generate a new event.

Release-groups still carry `primary_type` and `secondary_types` (album, EP, single,
live, compilation, remix), which Encore surfaces so per-artist filtering (F10, albums-
only by default) works without needing release-level granularity. First-release-date
on the release-group is used as-is, including future dates, so an announced-but-
unreleased album becomes an "upcoming" entry rather than being invisible until it
ships.

## Consequences

- Reissue/remaster noise is eliminated by construction, not by a dedup heuristic
  layered on top — the correctness guarantee is structural.
- A release-group's `first-release-date` can itself be revised by MusicBrainz
  editors (an announced date slips); Encore must treat a date change on an
  already-seen release-group as an update event ("date changed"), not a duplicate
  new-release event — this is the `events.kind = date_changed` case in the data
  model.
- The MusicBrainz "browse release-groups by artist" endpoint is the one Pipeline 1
  polls; the rate-budget math in this ADR's sibling documentation
  (`encore-plans/04-architecture.md` §external API budget) is scoped to that
  endpoint's page sizes.
