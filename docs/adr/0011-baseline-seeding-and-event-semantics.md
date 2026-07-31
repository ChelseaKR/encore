# 11. First-poll baseline seeding and release-event semantics

## Status

Accepted (2026-07-31)

## Context

ADR-0001 fixed *what* Encore watches (release-groups). F3 has to decide *when a
stored observation becomes an event* — the rows F4 will turn into notifications
and F5 into feed/calendar entries. The hard case is the very first poll of an
artist: browsing a matched artist returns their entire back catalog, and a naive
"unseen group ⇒ event" rule would fire hundreds of "new release!" notifications
for decades-old albums the moment a library finishes matching. A product that
opens by spamming its one job — signal — would train users to mute it on day one
(the same failure mode ADR-0001 exists to prevent).

A second decision hides in date handling: MusicBrainz first-release dates are
*partial* (`1999`, `2026-09`, `2026-09-18`, or absent), so "is this in the
future?" needs a defined reading.

## Decision

Three event kinds exist (`new`, `upcoming`, `date_changed`), produced by these
rules in `encore.watch.engine`:

1. **Baseline seeding.** The first poll of an artist (no stored release-groups
   for that MBID) records the whole catalog **silently** — no `new` events.
   The catalog is inventory, not news.
2. **Upcoming pierces the baseline.** A future-dated group is news even at
   baseline: it becomes an `upcoming` event. Announcements are exactly what a
   release radar exists to surface, and they feed the F5 iCal calendar.
3. **After baseline:** an unseen group ⇒ `new` (or `upcoming` when
   future-dated); a revised first-release date on a seen group ⇒
   `date_changed`. Anything else — including new *release* entries inside a
   seen group — produces nothing (ADR-0001's reissue dedupe).
4. **Partial dates read conservatively.** A partial date resolves to its
   *earliest* possible day (`2026` → 2026-01-01) for the future test, so a
   bare-year group only counts as upcoming before that year starts; malformed
   or absent dates are treated as released (`new`). Dates are stored verbatim
   as MB's text and parsed only at diff time.

## Consequences

- A fresh install reaches "quiet, then only real news" without any tuning —
  the F4 signal-to-noise budget starts intact.
- The `events` table carries `notified_at` (NULL until delivered) so F4/F5
  consume the same rows F3 writes — no shadow queue to drift.
- An artist re-matched to a different MBID (F2 manual re-match) has no stored
  groups under the new MBID, so their next poll is a fresh silent baseline —
  a wrong match never converts into a burst of stale notifications.
- Trade-off accepted: a release-group published *between* matching and the
  first poll is swallowed by the baseline. The window is at most one poll
  cadence (default 24h) and the alternative (alert on the whole catalog) is
  strictly worse.
