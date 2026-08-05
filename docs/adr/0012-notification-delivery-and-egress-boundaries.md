# 12. Notification delivery: a materialized queue, and where F4's egress stops

Date: 2026-08-01

## Status

Accepted

## Context

F3 records release events; F4 has to get them to a human. The planning corpus
(`encore-plans/03` §F4, `encore-plans/04` §pipelines) fixes the *what* — Apprise
as the single fan-out dependency, instant and digest cadences, an in-app feed as
the always-works fallback, cover art, a Plex deep link, retry with backoff, and
failures that surface instead of dying silently. It does not fix the *how*, and
four of those requirements turn out to carry design decisions that are easier to
get wrong than to get right:

1. **What the delivery queue is.** `events.notified_at` was created by F3 as
   "the delivery queue's cursor." A single timestamp on the event cannot
   describe a five-channel install where Discord succeeded, email is backing
   off, and a webhook has exhausted its retries.
2. **What happens when a channel is added.** A library with three years of
   history and a channel added today could either say nothing or say
   everything.
3. **Whether cover art means fetching cover art.** Apprise can attach a remote
   image, which makes *encore's host* download it.
4. **Where the in-app feed lives.** Encore has no authentication until F6.

## Decision

**1. Deliveries are materialized: one row per (event, channel).** The
`deliveries` table carries its own `status`, `attempts`, `next_attempt_at`, and
`last_error`, so per-channel outcomes are independent and a backoff is a stored
timestamp rather than an in-memory timer that a restart forgets.
`events.notified_at` survives as a **summary**: it is stamped once every
delivery fanned out from that event is terminal (`delivered` *or* `failed`). It
means "encore has finished trying," not "the user was told" — an event with no
channels at all keeps `NULL` forever, because claiming otherwise would put a lie
in the one column an operator would check.

Retry is exponential and bounded: 5 minutes, then 10, 20, 40, and after five
attempts the delivery goes terminal-`failed`. Every failure is also written to
the channel row (`consecutive_failures`, `last_error`), which is what
`encore channels list` prints and what the F6 UI will render.

**2. Adding a channel never replays history.** A delivery row is only created
for events whose `created_at` is at or after the channel's own `created_at`.
This is ADR-0011's silent-baseline rule applied to a new destination instead of
a newly watched artist: the first thing a new channel does must not be to
deliver three years of back catalog.

**3. Cover art ships as a URL, not as an attachment.** The Cover Art Archive
address for a release-group is deterministic
(`https://coverartarchive.org/release-group/<mbid>/front`), so it can be
rendered into the message body with **zero** requests from encore. Passing it to
Apprise as an attachment would make encore's own host fetch every cover — a new,
undisclosed outbound flow, and one that would fire for artists whose events the
user never opens. As a link, the fetch (if any) belongs to the notification
service or the reader's client. Consequences: the link 404s when the archive has
no art for that group, and we do not check in advance, because checking costs one
request per event for a cosmetic improvement.

**4. The in-app feed is `encore events`, a CLI surface, until F6.** The feed is
pure taste data. Encore has no authentication until the F6 wizard sets the
single admin password, and the deployment shape is a container whose port people
publish. An unauthenticated `/events` route would hand a household observer
exactly the feed the no-outing lens (`docs/RESPONSIBLE-TECH-AUDITS.md` §B,
`docs/audits/dpia.md` §4) exists to protect. Reading the feed over the CLI
requires the filesystem access the operator already has. The HTTP feed lands
*with* the auth that makes it safe, not before it.

**5. Apprise URLs are secrets.** `ntfy://user:pass@host`,
`discord://id/token`, and `mailto://user:pass@server` are credentials. They are
stored as Fernet ciphertext under ADR-0008's scheme, decrypted only at the
moment of sending, never logged, never printed by the CLI, and never echoed into
a `DeliveryError` message — the adapter reports the exception *type*, because a
third-party plugin is free to put the URL in its own message.

## Consequences

- The delivery queue is inspectable and restart-safe: a backoff survives a
  container restart, and "why did this not arrive?" is answerable from the
  database.
- `deliveries` grows as events × channels. Nothing prunes it yet; a retention
  policy belongs with the F15 export/wipe work at M4, where the rest of the
  data-lifecycle story lives.
- Digest channels deliver on the *first* cycle after they are created and then
  every `digest_interval_hours`; a digest of exactly one item renders as a plain
  notification, because a rollup of one is a notification.
- Rendering is transport-neutral text (title + body). Service-specific richness
  (Discord embeds, ntfy priority headers, HTML email) is deliberately not
  modeled: it multiplies per-service behavior we cannot test offline, and
  Apprise already degrades a text message sensibly everywhere.
- What this ADR cannot settle: whether real services accept these messages. That
  needs a live ntfy/Discord/SMTP endpoint and is recorded as an open acceptance
  item in `docs/ROADMAP.md` §7 rather than asserted.
