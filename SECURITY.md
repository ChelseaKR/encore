# Security policy

Encore stores a credential that grants full read (and technically write) access to
your Plex server, plus notification-channel URLs that often embed credentials of
their own. Security here is inseparable from user safety — please read the reporting
rule below.

## Supported versions

This is a pre-1.0 scaffold; there is no tagged release yet. Security fixes will land
on `main` and the latest tagged release once one exists. Pin a tag and watch releases
for advisories.

| Version | Supported |
| ------- | --------- |
| `main` / latest tag | ✅ |
| older tags | ❌ |

## Reporting a vulnerability

**Email ckellyreif@gmail.com** with `encore security` in the subject — this is the
primary channel today: the repo is private, and GitHub's private vulnerability
reporting ("Report a vulnerability" under the *Security* tab) is not functional on
a private free-plan repo. Once the repo is public, GitHub PVR becomes the preferred
channel and this section will be reordered (tracked in the roadmap, DOC-09).
Expect an acknowledgement within a few days; this is a volunteer project, so please
be patient and do not disclose publicly until a fix is available.

### Redaction-safe reporting (please read)

When you report a bug or a security issue, **never paste a real Plex token, a real
Apprise/notification-channel URL, or real listening/taste data.** If a flaw exposes
one of these, describe the *shape* of the leak — "the token appears in the DEBUG log
line on route X" — and reproduce it with the synthetic sentinel fixtures that live
under `tests/` (never with real values).

## What we consider a vulnerability

In addition to the usual (RCE, auth bypass, injection, secret exposure), the
following are **first-class** security bugs in Encore:

- **Any path by which the Plex token, an Apprise URL, or a feed token renders** in a
  view, the JSON API, an export, a log line above DEBUG, a metric label, or an error
  message.
- **Any path by which Encore writes to the configured Plex server** — it is meant to
  be read-only, always (`docs/adr/0007-read-only-plex-client.md`).
- **Any path by which taste data (artist/listening history) is exposed to a viewer
  who shouldn't see it**, or is used to infer a demographic/identity attribute about
  the person who owns the library (see `docs/RESPONSIBLE-TECH-AUDITS.md` §B).
- **Any outbound network call to anything other than the metadata APIs
  (MusicBrainz/ListenBrainz/Cover Art Archive) and the user's own configured
  notification channels.** Encore has no telemetry endpoint; any other egress is a
  bug, not a feature.

See `docs/RESPONSIBLE-TECH-AUDITS.md` §F for the full threat model (household/shared-
server observer, token thief, the developer) and the guarantees the code must meet.

## Our commitments

- We fix secret-exposure and read-only-Plex regressions with the highest priority.
- We credit reporters who want credit, and respect those who want anonymity.
- Dependencies are pinned and scanned (pip-audit, fail-closed, against the locked third-party set exported from `uv.lock`,
  CodeQL for python + the workflows themselves, gitleaks in pre-commit + CI + a
  weekly full-history sweep, zizmor for the workflows, Trivy on every container
  build); releases are signed once a release pipeline exists (see `docs/adr/`).
