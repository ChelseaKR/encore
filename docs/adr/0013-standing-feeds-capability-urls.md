# 13. Standing feeds: capability URLs, one opaque 404, and no invented dates

Date: 2026-08-04

## Status

Accepted

## Context

F5 adds the standing feeds: an RSS feed of release events and an iCal feed of
upcoming release dates. The planning corpus (`encore-plans/03` §F5) fixes the
*what* — feeds validate, iCal subscribes cleanly, and the URLs carry an
unguessable token because feed contents are taste data. Three design questions
remain open, and each sits directly on the no-outing lens
(`docs/RESPONSIBLE-TECH-AUDITS.md` §B):

1. **How does an HTTP feed exist before F6's admin password?** ADR-0012 §4
   ruled that an unauthenticated `/events` route must wait for F6. Feed
   readers and calendar clients, though, cannot present a password at all —
   token-in-URL is the only authentication the entire ecosystem supports.
2. **What does an unauthorized request learn?** Every distinct failure
   response (401 vs 403 vs 404, "no token configured yet" vs "wrong token")
   is information handed to exactly the household observer the lens exists
   to blind.
3. **What does a partial release date become on a calendar?** MusicBrainz
   publishes `2027` and `2027-03` as real announcements, but a calendar entry
   needs a day.

## Decision

**1. The token is the auth, and it is a first-class secret.** The feed URL is
a capability: `/feeds/<token>/releases.xml` and `/feeds/<token>/upcoming.ics`,
with a `secrets.token_urlsafe(32)` token minted lazily on first `encore feeds
show`, stored Fernet-encrypted beside the Plex token (ADR-0008, migration v6),
compared in constant time, never logged by encore, and printed only by the CLI
command whose entire job is handing it to the operator. Rotation
(`encore feeds rotate`) is the revocation story the DPIA promises: every
previously shared URL dies at once. This does not contradict ADR-0012 §4 —
that ruling was against an *unauthenticated* route, and the capability token
is the authentication, shipped in the same PR as the routes it gates.

**2. Every failure is the same bare 404.** Wrong token, no token ever minted,
storage not yet open — all indistinguishable from a route that does not exist.
A prober learns nothing: not that feeds are enabled, not that setup is
incomplete, not that the path is meaningful. The `no_outing`-marked tests pin
this by comparing the response byte-for-byte against a genuinely unknown
route's 404 and by grepping for the sentinel artist.

**3. The calendar never invents a day.** Only day-precision
(`YYYY-MM-DD`) dates become VEVENTs, from today forward; `2027` and `2027-03`
announcements stay visible in the RSS feed (which prints partial dates
verbatim, per the F4 renderer it reuses) but never appear as a calendar entry
claiming a specific day. The VEVENT `UID` is the release-group MBID, so a
date change *moves* the entry in a subscribed calendar instead of duplicating
it; entries are `TRANSP:TRANSPARENT` so a release date never makes the user
look busy.

**4. RSS items are F4 renderings; iCal is hand-rolled RFC 5545.** The RSS
item title/body reuse `encore.notify.render.render_event` — one translated,
tested rendering, no drift — with the event id as a stable non-permalink
`guid` and MusicBrainz's public release-group page as the item link. The
calendar is generated directly (escaping, 75-octet folding, CRLF) because the
three rules that matter are a dozen pinned-by-test lines, and a calendar
library would be a new dependency whose output we would still have to verify.

## Consequences

- **The token appears in the reader's URL, therefore in intermediary and
  server access logs.** Encore's own log lines never carry it (the
  `no_secrets_in_logs` test), but uvicorn's access log and any reverse proxy
  on the operator's host will see the path. This is inherent to
  token-in-URL — the only scheme calendar/feed clients support — and the
  exposure is on infrastructure the operator already controls, with rotation
  as the remedy. Documented here rather than implied away.
- Sharing a feed URL shares the taste feed — by design, as the user's own
  disclosure choice (dpia.md §3); the CLI says so every time it prints one.
  Recorded as residual risk RR-06.
- **A cloud feed reader becomes a reader of the feed.** Because RSS items reuse
  the F4 rendering verbatim, they carry the same fields a notification does,
  including the `app.plex.tv` deep link and therefore the Plex server's machine
  identifier. That is the identical disclosure ADR-0012 accepted for
  notifications, now reaching a subscriber rather than a push destination;
  self-hosting the reader avoids it entirely, and the machine identifier is a
  server handle, not a credential.
- One token gates both feeds; per-feed or per-subscriber tokens (finer
  revocation) are deliberately not built at this scale — so revoking one
  over-shared URL revokes all of them (RR-07).
- A month-precision announcement is invisible to calendar-only users until
  MusicBrainz records a full date. That is the honest trade against
  fabricating "March 1st"; the RSS feed carries it meanwhile.
- The feeds are read-only GETs against two storage queries; no new outbound
  flows exist (the cover-art rule of ADR-0012 §3 carries over: URLs in item
  bodies, encore fetches nothing).
