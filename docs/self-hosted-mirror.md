# Running encore against a self-hosted MusicBrainz mirror

encore is a heavy MusicBrainz user by construction. A 1,000-artist library is
roughly seventeen minutes of polling per watch cycle under the public 1 req/s
budget, every cycle, forever — and MetaBrainz asks users at that scale to run a
mirror rather than to be that load on donation-funded infrastructure
(`docs/adr/0003-metabrainz-sole-metadata-supplier.md`).

Pointing encore at a mirror does two things. It makes matching and the release
poll finish in seconds instead of minutes. And it is the only configuration in
encore that changes who sees the household's taste data: with a mirror, the
artist names in search queries and the artist MBIDs in browse queries never
leave the operator's network (`docs/audits/dpia.md`, mirror update).

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `ENCORE_MB_BASE_URL` | `https://musicbrainz.org/ws/2` | The MusicBrainz web service encore reads. Point it at your mirror's `/ws/2`. |
| `ENCORE_LB_BASE_URL` | `https://labs.api.listenbrainz.org` | The ListenBrainz labs API used for F7/F8 similar-artist recommendations. |
| `ENCORE_COVER_ART_BASE_URL` | `https://coverartarchive.org` | The base encore builds cover-art URLs from. |
| `ENCORE_MB_RATE_LIMIT` | `1` | Requests per second against `ENCORE_MB_BASE_URL`. **Honoured only when that host is not MetaBrainz's own.** |

`encore doctor` prints the endpoint in use on its `metadata_endpoint` line, and
`/readyz` reports it as a named check. Both are the first place to look when a
mirrored install goes quiet.

## The two rules this configuration will not bend

**The public host is pinned at 1 req/s, whatever you set.** Against
`musicbrainz.org` (or any host under it) `ENCORE_MB_RATE_LIMIT` is discarded and
said so — once in the startup log, and as a `warn` on `encore doctor`'s
`metadata_endpoint` line. An installation that hammers MetaBrainz because
somebody exported a variable is a failure encore should not be able to have.
Raising the rate means running a mirror; there is no other door.

**encore never silently falls back to the public host.** A base URL that is
malformed, or that carries a credential (`https://user:pass@host/ws/2` is
refused outright, and never echoed into a log or a health check), is an error.
A mirror that does not answer, or that answers something other than the
MusicBrainz web service, makes `/readyz` unready with the reason and stops
MusicBrainz- and ListenBrainz-dependent polling entirely. Notification delivery
of releases encore *already* knows about keeps running — a metadata outage must
not also silence the alerts you configured encore for.

That refusal is not fastidiousness. Choosing a mirror is a decision to keep your
library's artist names on your own network, and a fallback would reverse that
decision without telling anyone.

## What the startup probe checks, and what it does not

A mirror that is up, serving HTTPS, and answering an nginx welcome page looks
perfectly healthy to any check that only asks whether the host resolves — and
encore would poll it forever, record nothing, and report a green scheduler the
whole time. So at startup encore asks a configured mirror for one MusicBrainz
artist (the "Various Artists" special-purpose MBID, which every mirror of the
database reproduces) and requires a MusicBrainz answer: HTTP 200, JSON, and the
artist that was asked for.

**The public endpoint is not probed.** Its address is a constant in the source,
and a request to donation-funded infrastructure on every boot is not free. That
is reported as `not_applicable`, with the reason, rather than as a check that
passed — an unconfigured install still opens no socket at startup.

## A `musicbrainz-docker` compose fragment

MetaBrainz publishes the mirror itself at
<https://github.com/metabrainz/musicbrainz-docker>; follow its README for the
initial data import, which is the long part. Once it is serving, encore needs
only the address:

```yaml
services:
  encore:
    image: ghcr.io/chelseakr/encore:latest
    environment:
      # Your musicbrainz-docker instance. The path is the web service root.
      ENCORE_MB_BASE_URL: "http://musicbrainz:5000/ws/2"
      # Only honoured because the host above is not musicbrainz.org.
      ENCORE_MB_RATE_LIMIT: "20"
    volumes:
      - encore-data:/data
    depends_on:
      - musicbrainz

volumes:
  encore-data:
```

Put the mirror behind a network boundary — a compose network, a VPN, a reverse
proxy that authenticates for you. encore will not accept a username and password
in the URL, because that URL is named in logs and health checks.

## What a mirror does not cover

**Cover art.** The Cover Art Archive is a separate service and is not part of a
MusicBrainz mirror. It is also not fetched by encore's host at all: cover art
travels to your notification channel as a URL, so it is the *recipient's* client
that contacts the archive (`docs/adr/0012`). Setting
`ENCORE_COVER_ART_BASE_URL` changes which host that client is sent to — useful
if you run a caching proxy — and does not change whether one is contacted.

**ListenBrainz labs.** The similar-artist recommendations (F7/F8) come from the
labs API, which is not part of a MusicBrainz mirror either.
`ENCORE_LB_BASE_URL` exists for operators who run a labs mirror; with the
default, recommendation queries still carry artist MBIDs to MetaBrainz even
when matching and the release poll do not.

**Freshness.** A mirror is as current as its last replication run. encore reports
what its configured endpoint says; a stale mirror produces a quiet radar, and
nothing in encore can tell that apart from a quiet week.
