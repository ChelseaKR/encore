# Outbound webhooks

Encore can POST a signed, versioned JSON event to a URL you control every time a
release event happens, so another tool can subscribe without scraping a message
written for a person.

This is the machine-readable half of the boundary the README's non-goals draw:

> At most: standard outbound webhooks on new-release events so *other* tools can
> subscribe — Encore's responsibility ends at the notification.

Encore sends the event. What happens next is your tool's business, and Encore
never learns whether anything happened at all beyond the status code.

## Why not Apprise's `json://`

Apprise can already POST to a URL, and if a rendered message is what you want,
use it. What it sends is the *notification*: a title and a body of prose, the
same text a person would read in Discord. There is no schema, no event type, no
MBIDs, and no signature, so a subscriber can only pattern-match on English that
is free to change in any release — and has no way to tell an Encore request from
anything else that finds the URL.

## Adding a webhook channel

```bash
encore channels add --name home-assistant --kind webhook
```

You are prompted twice, both hidden, and neither value is ever passed as a flag
(a flag lands in your shell history and in `ps`):

- the **URL** to POST to;
- a **signing secret**, which you choose. Anything with real entropy;
  `openssl rand -hex 32` is a fine source.

Both are encrypted at rest under the same scheme as the Plex token
([adr/0012](adr/0012-notification-delivery-and-egress-boundaries.md),
[adr/0008](adr/0008-secrets-at-rest-scheme.md)) and neither is ever printed or logged.
**Keep your own copy of the secret**: it cannot be read back out of Encore.

A webhook channel takes no secret-less shortcut. Adding one without a secret is
refused rather than defaulted, because an unsigned webhook looks exactly like a
working one right up until somebody else finds the URL.

Then prove the path works before waiting for a release:

```bash
encore channels test --name home-assistant
```

That fires a real `channel.test` envelope — the same shape a release event has,
with the parts it cannot have set to `null` — so it exercises your parser and
your signature check rather than bypassing both.

## The request

```
POST <your URL>
Content-Type: application/json
X-Encore-Event: release.new
X-Encore-Signature: t=1757203200,v1=8f3c...e1
User-Agent: encore-webhook/1
```

`X-Encore-Event` repeats the envelope's `event_type` so you can route without
parsing the body.

### Body

The full shape is pinned by
[`webhook-event-v1.schema.json`](webhook-event-v1.schema.json). One example:

```json
{"artist":{"mbid":"a74b1b7f-71a5-4011-9441-d0b5e4122711","name":"Radiohead"},"event_id":412,"event_type":"release.new","links":{"cover_art":"https://coverartarchive.org/release-group/b1392450-e666-3926-a536-22c65f834433/front","plex":"https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F1234"},"occurred_at":"2026-09-06T21:00:00Z","release_group":{"first_release_date":"2027-03","mbid":"b1392450-e666-3926-a536-22c65f834433","primary_type":"Album","secondary_types":[],"title":"A New Record"},"schema_version":1}
```

Four things about that body are deliberate.

**Every key is always present.** A value the record does not have is `null`, not
missing. You should never have to tell "this release has no cover art" from
"this build stopped sending the key".

**`first_release_date` is MusicBrainz's partial date verbatim** — `2027`,
`2027-03`, or `2027-03-14`. It is never padded to a full date, because padding
invents precision MusicBrainz did not publish, and it is `null` when
MusicBrainz publishes no date at all. That is an ordinary case, not an edge
one: an undated release group still raises a `release.new` event, and it used
to arrive here as `""` — a value your parser would have had to special-case,
against the rule in the paragraph above.

**`links.plex` is `null` until a Plex sync has run**, and for an artist with no
Plex row (one promoted from a recommendation, say). A deep link built from a
guessed machine identifier goes nowhere.

**Nothing else about your library is in it.** No Plex token, no internal
identifiers beyond `event_id`, nothing about any artist the event is not about.

`event_id` is stable. **Deduplicate on it**: a retry after a timeout can deliver
the same event twice, and that is the one thing a bounded-retry sender cannot
rule out.

### Event types

| `event_type` | What happened |
|---|---|
| `release.new` | A release group Encore had not seen appeared for a watched artist. |
| `release.upcoming` | A future-dated release group was announced. |
| `release.date_changed` | An announced release group's date moved. |
| `channel.test` | You ran `encore channels test`. Not a recorded event: `event_id` is `null`. |

## Verifying the signature

The header is

```
X-Encore-Signature: t=<unix seconds>,v1=<hex>
```

where `<hex>` is `HMAC-SHA256(secret, "<t>." + raw_request_body)`.

Sign and compare the **raw bytes you received**, not a re-serialisation of the
parsed JSON. Encore emits a canonical body — sorted keys, compact separators,
UTF-8 — so the bytes are stable, but any re-encoding on your side may not
reproduce them, and then a valid signature fails for no reason a user can see.

The timestamp is inside the signed material, not merely carried beside it, so a
captured request cannot be replayed under a fresh one. Reject a signature whose
`t` is more than a few minutes from your own clock; Encore's own verifier
defaults to 300 seconds.

Python:

```python
import hmac
from hashlib import sha256

def verify(secret: str, body: bytes, header: str, now: int, tolerance: int = 300) -> bool:
    parts = dict(p.strip().split("=", 1) for p in header.split(","))
    t = int(parts["t"])
    if abs(now - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + body, sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])
```

Encore ships that same function as
`encore.notify.webhook.verify`, and its own tests run it, so the check described
here is the check that is exercised.

## Delivery, retries and failure

A webhook channel is an ordinary channel. Everything that already applies to an
Apprise channel applies to it unchanged:

- **routing** (`encore channels route`) and **per-artist filters and muting**
  decide what reaches it. A muted artist's event creates no webhook delivery at
  all, exactly as it creates no Discord message;
- **retries** are bounded and back off — 5 minutes, 10, 20, 40, then the
  delivery goes terminal `failed`. Any non-2xx status is a failure;
- **channel health** is visible in `encore channels list`, with the last error
  and the consecutive-failure count. The error names the status code and nothing
  else: your endpoint's response body is never stored or logged, because it is
  free to echo the URL it was reached at.

One difference, and it is about packaging rather than timing. If an artist is
set to `digest` priority, or the channel's mode is `digest`, a webhook channel
still **waits** for the window — that preference is about when you want to hear,
not about who is reading. When the window opens, each event goes as its own
signed request rather than as one rollup. A rollup is a kindness to a human
inbox; to a machine it is a single request whose failure would leave several
deliveries ambiguous, and Encore promises no duplicate deliveries.

## What Encore will not do

- **No inbound webhooks**, and no listening endpoint for Plex to call. Plex
  webhooks need Plex Pass ([adr/0002](adr/0002-poll-dont-webhook.md)).
- **No per-channel payload templating.** The envelope is one documented shape so
  that a subscriber written against it keeps working.
- **Nothing about acquisition.** A webhook says a release exists. Where you get
  it, if you get it, is not Encore's business and never appears in the payload.
