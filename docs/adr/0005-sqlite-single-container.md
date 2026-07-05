# 5. SQLite, single container

## Status

Accepted

## Context

Encore's audience is a self-hosted Plex user running Docker already; "consumer-
grade" here means the install is `docker run -v encore-data:/data -p 8321:8321 ...`
and nothing else. A separate database service (Postgres/MySQL) would mean a second
container, a second set of credentials to secure, and a second thing that can fail —
real cost for a single-operator, single-writer workload (one Plex library, one set
of settings, one review queue) that SQLite is squarely built for.

## Decision

Encore uses SQLite in WAL mode, via SQLModel, as its only datastore, in a single
container/single volume deployment. There is no multi-container/orchestration story
for v1.

## Consequences

- Backup is copying one file (plus WAL/SHM siblings during a checkpoint); the
  `encore export`/`encore wipe` CLI pair (privacy §subject rights,
  `encore-plans/06-privacy-responsible-tech.md`) is correspondingly simple to
  implement and to reason about.
- Write concurrency is bounded by SQLite's single-writer model. This is a
  non-issue for the single-user, single-library v1 workload (one scheduled sync job,
  one scheduled watch job, occasional interactive writes from the review queue and
  settings), but it is the first thing to revisit if F13 (multi-user households) is
  ever built.
- **Relaxation rule:** revisit at F13 (multi-user, per-user data isolation) if write
  contention or per-tenant isolation demands a different datastore. Not before —
  this is a deliberate soft constraint, not a permanent one.
