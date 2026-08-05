# Data Protection Impact Assessment — encore

**Status:** F0 assessment — grounded in the implemented settings schema and
regenerated again when Plex/matching data and outbound flows activate (see
"Recheck" below). This is not a placeholder: it assesses current storage plus
the F1-F5 design committed in `docs/adr/`.

**Owner:** Chelsea Kelly-Reif (sole maintainer and controller — see §5).
**Date:** 2026-07-05. **F0 update (2026-07-11):** the storage layer now exists —
the Plex credential inventory rows below are implemented in SQLite with the
token Fernet-encrypted under a key beside the database and the base URL stored
in plaintext. A raw-bytes test in `tests/test_secrets_at_rest.py` proves both
facts. No new data class and no new outbound flow was added, so the full
regeneration against the real schema remains due at M1 exit as stated above.
**F1 update (2026-07-17):** the artist-inventory data class is now real — the
`artists` table stores artist name, Plex GUID, Plex rating key, library key,
and first/last-seen + tombstone timestamps, synced read-only from the
operator's own Plex server (the only party contacted; enforced at the
transport layer, `docs/adr/0007`). Play counts are **not** collected — that
column arrives with F9, not F1. Logging of this taste data is sentinel-tested
(`no_outing`/`no_secrets_in_logs` markers): counts at INFO, names at DEBUG
only. The full regeneration against the real schema remains due at M1 exit.
**F2 update (2026-07-17):** the MusicBrainz matching layer now exists
(`src/encore/matching/`), which makes two rows below real rather than planned:
the "MBID match table" is implemented as `artist_matches` (artist name, MBID,
confidence, status, and a ranked-candidates JSON column for the review queue —
all local, permanent-cache retention), and the MetaBrainz outbound flow is
live in the code path: artist names travel to musicbrainz.org in search
queries over HTTPS, tied to the operator's IP, exactly as the risk table
already discloses. Egress carries a descriptive User-Agent and nothing else —
no Plex token, no play counts — proven by `no_outing`/`no_secrets_in_logs`
marker tests, which also pin that artist names/MBIDs never appear in log
output (httpx request-URL logging is suppressed in the MB client for this
reason). No *new* data class was added; the full regeneration against the
real schema remains due at M1 exit.
**F3 update (2026-07-31):** release watching now exists (`src/encore/watch/`,
`release_groups` + `events` tables). The new data class is **derived taste
data**: per matched artist, MusicBrainz release-group MBIDs, titles, types,
and first-release dates, plus an event log (`new`/`upcoming`/`date_changed`)
with a `notified_at` delivery cursor — all local-only, retained as a permanent
diff baseline. No new *outbound* data class: the poll sends only artist MBIDs
(already-disclosed identifiers from the F2 flow) to musicbrainz.org over the
same rate-limited, descriptive-User-Agent channel, tied to the operator's IP
exactly as already disclosed below. The event log records *releases* observed,
never user behavior — it is not a listening/usage log. `no_outing` marker
tests extend to this layer: artist names, MBIDs, and release titles never
appear in log output at any level. The full regeneration against the real
schema remains due at M1 exit.
**F4 update (2026-08-01):** notification delivery now exists
(`src/encore/notify/`, `channels` + `deliveries` tables) and it is the first
feature that sends **taste data to a destination the user chose**, so it is the
first real test of the no-outing lens rather than a design commitment about it.
Three things changed in the inventory below. (1) Apprise channel URLs are held
Fernet-encrypted under ADR-0008's scheme — they are credentials, and a raw-bytes
test proves the plaintext never reaches the database file. (2) A `deliveries`
queue holds per-channel attempt state; it contains ids, counts, and error
strings, no taste data, and is **not yet pruned** — retention lands with F15 at
M4. (3) The notification *content* — artist, release title, type, date, a Cover
Art Archive URL, and a Plex deep link — leaves the host in readable form to
whatever service the user configured. That is the feature working as designed,
and it is disclosed as an egress rather than implied away: choosing a Discord
webhook means choosing Discord as a reader of that feed. Two boundaries were
drawn deliberately (`docs/adr/0012`): cover art travels as a **URL, not an
attachment**, so encore's own host never contacts the Cover Art Archive; and the
in-app event feed is a **CLI surface, not an HTTP route**, because encore has no
authentication until F6 and an unauthenticated feed on a published container
port is precisely the outing risk RR-04 names. `no_outing`/`no_secrets_in_logs`
marker tests extend to this layer: neither the notification body nor the channel
URL ever reaches a log line, and a third-party plugin's exception message is
reduced to its type before it can echo a URL. The full regeneration against the
real schema remains due at M1 exit.
**Recheck trigger:** re-verify and expand this document whenever any of the
following lands, and in any case no later than M1 (`docs/ROADMAP.md` §8):
F11 (ListenBrainz account linking), F12 (Jellyfin/Navidrome adapter), F14
(optional "vibe" recommendations, the only path by which an LLM could enter the
product — see `docs/RESPONSIBLE-TECH-AUDITS.md`'s AI-Evaluation N/A). Each of
these adds an outbound integration or a new class of held data; per
`docs/RESPONSIBLE-TECH-AUDITS.md` §C, "any new outbound integration requires a
DPIA update before merge."

This document instantiates the portfolio's private `RESPONSIBLE-TECH-FRAMEWORK.md`
DPIA requirement (RTF-04). It is the regenerated companion to the data-inventory
seed table in `docs/RESPONSIBLE-TECH-AUDITS.md` §C — that table is authoritative
for day-to-day edits (update it first on any change to what's collected); this
file adds the assessment structure a DPIA proper requires: necessity, legal
basis, risk, and mitigation, not just an inventory.

## 1. Description of processing

Encore is a single-tenant, self-hosted service: one operator runs one container
against their own Plex server and their own SQLite database, on their own
hardware. There is no multi-tenancy, no Encore-operated backend, and no
"processing on Encore's behalf" in the GDPR sense — the operator is the sole
controller of their own instance's data, and the maintainer (Chelsea Kelly-Reif)
never receives, stores, or has access to any operator's Plex token, library, or
taste data. This DPIA is written as if Encore were subject to GDPR-style
scrutiny (it is not, per §5), because the discipline of asking the questions is
worth it regardless of legal applicability — and because a self-hosted tool that
handles someone else's household taste data has an ethical obligation here even
absent a legal one (`docs/RESPONSIBLE-TECH-AUDITS.md` §A/§C).

## 2. Necessity and proportionality

Every field in the inventory below exists to serve one of the product's five
core features (F1 sync, F2 match, F3 watch, F4 notify, F5 feeds — see
`README.md` and the ranked feature plan referenced from `docs/ROADMAP.md` §3).
Nothing is collected "in case it's useful later": there is no analytics table,
no event log of user behavior, no usage telemetry sent anywhere. The Plex token
and Apprise URLs are collected because the product cannot function without
them (read access to the library; a delivery destination for alerts) — there is
no lower-privilege alternative Plex currently exposes for read-only library
listing, which is why `docs/adr/0007-read-only-plex-client.md` constrains what
Encore does with that access rather than trying to avoid holding it.

## 3. Data inventory

| Data | Why held | Where | Retention | Sensitivity | Shared with |
|---|---|---|---|---|---|
| Plex base URL | locate the operator's Plex server for library sync (F1) | SQLite, plaintext | until user removes it | Low — endpoint location, not an access credential | Nobody. Never leaves the host. |
| Plex token | authenticate library sync (F1) | SQLite, Fernet-encrypted at rest (`docs/adr/0008`) | until user removes it | **High** — grants full Plex access | Nobody. Never leaves the host. |
| Artist inventory (implemented with F1: name, Plex GUID, rating key, library key, seen/tombstone timestamps) | matching (F2) | SQLite | mirrors Plex; tombstoned on removal | Medium — taste data, inference-rich (see §4) | Artist names go to MusicBrainz as search queries at match time (F2, the disclosed MetaBrainz flow below); never play counts |
| Play counts (not yet collected — F9) | weighting (F9) | SQLite | mirrors Plex | Medium — taste data | Never shared — local weighting only |
| MBID match table (`artist_matches`: name, MBID, confidence, status, candidates) | identity matching + review queue (F2); release watching (F3) | SQLite | permanent cache; manually re-matchable/skippable | Medium — artist names + candidate lists are taste data | Not shared; internal cache. Artist names go to MusicBrainz as search queries at match time (the disclosed MetaBrainz flow below) |
| Release-group + event tables (implemented with F3: MBIDs, titles, types, first-release dates, event kinds, delivery cursor) | release watching diff baseline (F3); delivery queue (F4/F5) | SQLite | permanent diff baseline | Medium — derived taste data (what the user's artists release, not what the user plays) | Not shared; artist MBIDs go to MusicBrainz in browse queries at poll time (the same disclosed MetaBrainz flow) |
| Notification channel URLs (Apprise) — **implemented with F4** | delivery (F4) | SQLite, Fernet-encrypted at rest (`docs/adr/0008`, `docs/adr/0012`) | until user removes | **High** — many Apprise URLs embed credentials | Whatever destination the user configured (their own Discord/ntfy/SMTP/etc.) |
| Delivery queue (`deliveries`, implemented with F4: event/channel ids, attempt counts, backoff timestamps, last error) | per-channel retry state so a failure is visible instead of silent (F4) | SQLite | not yet pruned — retention lands with the F15 export/wipe work at M4 | Low on its own (ids and counts), but it links an event to a destination | Nobody. Never leaves the host. |
| **Notification *content*** (artist, release title, type, date, cover-art URL, Plex deep link) — implemented with F4 | the notification itself | transient; not stored beyond the tables above | n/a | Medium-High — this is the taste feed leaving the host in readable form | **The destination the user chose, and its operator.** A Discord webhook means Discord; an SMTP relay means that mail provider. This is the F4 egress, disclosed rather than implied away — and it is exactly the flow the no-outing lens in §4 governs: the user picks a destination, so the user picks who can read their taste |
| Cover-art URLs (Cover Art Archive) — implemented with F4 | show the album art in the notification | constructed on the fly from the release-group MBID; not stored | n/a | Low-Medium — the URL contains a release-group MBID | **Not fetched by encore.** The URL travels inside the notification body, so any fetch is done by the notification service or the reader's client, revealing a release MBID to the Cover Art Archive at that point. Attaching the image instead — which would make encore's host fetch every cover — was rejected in `docs/adr/0012` |
| Feed tokens (RSS/iCal) — **implemented with F5** | authenticate the standing feeds; the URL *is* the capability (`docs/adr/0013`) | SQLite, Fernet-encrypted at rest (`docs/adr/0008`) | minted lazily on first `encore feeds show`; rotatable at will, which revokes every URL already shared | **High** — the token is a bearer credential for the whole taste feed | Nobody by encore. The user hands the URL to the readers they choose; the token also reaches their own access logs and any reverse proxy they run |
| **Feed *content*** (the same artist/title/type/date/cover-art/Plex-deep-link fields as a notification) — implemented with F5 | the RSS and iCal documents themselves | transient; rendered per request from the tables above | n/a | Medium-High — the taste feed leaving the host in readable form | **Whoever holds the feed URL**, plus their feed reader or calendar provider. A cloud reader (Feedly, Google Calendar) becomes a reader of the feed; a self-hosted one does not. Same disclosure shape as the F4 notification-content row, with subscription rather than push |
| Optional ListenBrainz username (F11, not yet built) | account linking | SQLite | until unlinked | Medium | ListenBrainz (by definition of linking an account there) |

**Not collected, ever:** telemetry, analytics, crash reports to third parties,
accounts, email addresses (beyond a user-supplied SMTP target), music files,
IP address logs beyond what the OS/reverse-proxy layer keeps outside Encore's
control.

## 4. Risk assessment

The controlling insight (`docs/RESPONSIBLE-TECH-AUDITS.md` §B, §threat model):
music *taste* is quietly *sensitive-inference* data. Genre, artist, and
listening-pattern data can correlate with religion, politics, sexuality, or
mental state as reliably as more obviously regulated categories, even though
nothing in the inventory above is itself a "special category" under a strict
legal reading.

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Household/shared-server observer is outed by a taste feed landing somewhere visible to them (e.g. a shared Discord channel, a shared iCal calendar) | Medium — this is a "works as intended" harm, not a bug | Medium-High, personal | No silent multi-library aggregation; non-goals published in README; rotatable feed tokens; sentinel-artist tripwire test from M1 |
| Token thief obtains a database-only copy without its separately protected key | Low-Medium (depends on operator storage hygiene) | High — full Plex access, or a notification-channel credential | Fernet encryption at rest (`docs/adr/0008`) keeps the secret columns ciphertext without the matching key |
| Token thief obtains the whole `/data` volume, snapshot, or backup | Low-Medium (depends on operator backup hygiene, outside Encore's control) | High — the adjacent key can decrypt every stored secret | Encryption is not credited as a control here; stop-and-copy guidance in the README preserves recoverability, while host access control and encrypted/restricted backup storage mitigate RR-01 |
| A future integration (F11/F12/F14) adds a new outbound data flow without a matching privacy review | Low today (nothing beyond F0 storage exists) | Depends on the integration | This document's own recheck trigger (above); CODEOWNERS routes `/docs/audits/` and `/src/encore/matching/` to mandatory review |
| MusicBrainz/ListenBrainz correlate a user's IP with their artist list over time (an inherent property of any API call, not an Encore bug) | Certain, by design of using a third-party API | Low-Medium — reveals taste to two nonprofits' infrastructure, not to the public | Disclosed plainly in README §Privacy as a "disclosure choice," not implied away as "local-first means zero egress" |

## 5. Legal basis and applicability

No regulated-data compliance regime applies in the strict sense: Encore does
not process health data, financial data, or children's data, has no
Encore-operated backend that would make Chelsea Kelly-Reif a data controller
for anyone else's instance, and is not offered as a service to EU/UK
residents in a way that would trigger GDPR controller obligations on the
maintainer. Each self-hosted operator is their own controller for their own
instance, exactly as with any self-hosted software (Nextcloud, Jellyfin, etc.)
— this mirrors `docs/ROADMAP.md` §10's compliance note. This DPIA exists
anyway because the *product design* obligation (don't build a tool that makes
outing a housemate easy) doesn't depend on which statute would or wouldn't
apply.

## 6. Data subject rights, in a single-tenant context

Because there is no Encore-operated backend, "data subject rights" collapse to
"operator rights over their own SQLite file": full read access (it's their
disk), full deletion (`rm` the file, or a future in-app wipe — recovery/backup
guarantees are pytest-marked `recovery` and tracked in `docs/ROADMAP.md`),
and full portability (SQLite is already a portable, inspectable format with no
proprietary export step required).

## 7. Regenerate when Plex/matching activates

When the Plex and matching schemas (`src/encore/plex/`, `src/encore/matching/`)
land, this document must be regenerated against their actual table definitions
rather than the ADR-level description above, and gain the sentinel-artist
tripwire result and no-exfiltration outbound-request allowlist. The
pre-activation residual-risk register already exists at
`docs/audits/residual-risk.md`; update its dormant F1/M2 rows in the same PR.
