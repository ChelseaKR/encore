# Data Protection Impact Assessment — encore

**Status:** M0 seed — regenerated in full against the real schema at M1 (see
"Recheck" below). This is not a placeholder: it is a real, if narrow, assessment
of what M0 already knows about the product's data — the health-check scaffold
plus the F1-F5 design committed in `docs/adr/`.

**Owner:** Chelsea Kelly-Reif (sole maintainer and controller — see §5).
**Date:** 2026-07-05. **F0 update (2026-07-11):** the storage layer now exists —
the "Plex base URL + token" inventory row below is implemented exactly as
designed (SQLite, Fernet-encrypted at rest, key file beside the database,
proven by a raw-bytes grep test in `tests/test_secrets_at_rest.py`). No new
data class and no new outbound flow was added, so the inventory itself is
unchanged; the full regeneration against the real schema remains due at M1
exit as stated above.
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
| Plex base URL + token | library sync (F1) | SQLite, encrypted at rest (`docs/adr/0008`) | until user removes it | **High** — grants full Plex access | Nobody. Never leaves the host. |
| Artist inventory + play counts | matching, weighting (F2, F9) | SQLite | mirrors Plex; tombstoned on removal | Medium — taste data, inference-rich (see §4) | MusicBrainz/ListenBrainz receive artist names + MBIDs only, never play counts |
| MBID match table | release watching (F3) | SQLite | permanent cache | Low | Not shared; internal cache |
| Notification channel URLs (Apprise) | delivery (F4) | SQLite, encrypted at rest | until user removes | **High** — many Apprise URLs embed credentials | Whatever destination the user configured (their own Discord/ntfy/SMTP/etc.) |
| Feed tokens (RSS/iCal) | F5 auth | SQLite | rotatable | Medium — feed contents reveal taste | Whoever the user shares the feed URL with (their choice) |
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
| Token thief (stolen backup, stolen disk image) recovers the Plex token or an Apprise URL in plaintext | Low-Medium (depends on the operator's backup hygiene, outside Encore's control) | High — full Plex access, or a notification-channel credential | Fernet encryption at rest (`docs/adr/0008`); boundary stated honestly: this does not protect a live root-level attacker on the running host, only copies |
| A future integration (F11/F12/F14) adds a new outbound data flow without a matching privacy review | Low today (nothing beyond M0 exists) | Depends on the integration | This document's own recheck trigger (above); CODEOWNERS routes `/docs/audits/` and `/src/encore/matching/` to mandatory review |
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

## 7. Regenerate-at-M1 note

At M1, once the real schema exists (`src/encore/plex/`, `src/encore/matching/`
land), this document should be regenerated against actual table definitions
rather than the ADR-level description above, and gain: the sentinel-artist
tripwire test result, the no-exfiltration outbound-request allowlist, and the
residual-risk register cross-reference (`docs/audits/residual-risk.md`, also
created at M1 per `docs/RESPONSIBLE-TECH-AUDITS.md` §F).
