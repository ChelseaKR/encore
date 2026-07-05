# 0. Record architecture decisions

## Status

Accepted

## Context

Encore makes a small number of consequential, hard-to-reverse decisions — how it
watches releases, how it handles a Plex credential and taste data, which metadata
supplier it depends on, how it's deployed. This is a nights-and-weekends personal
project: when the maintainer's attention moves elsewhere for a while, the reasoning
behind a structural choice must not live only in a commit message or a closed PR
thread, or a later change will either re-litigate a settled question or unknowingly
reverse a decision made for a reason nobody re-reads.

## Decision

We will record architecture decisions in **Architecture Decision Records (ADRs)**
using the format described by Michael Nygard.

- Each ADR is a short Markdown file in `docs/adr/`, numbered sequentially and named
  `NNNN-title-in-kebab-case.md`.
- Each ADR has the sections **Title**, **Status**, **Context**, **Decision**, and
  **Consequences**.
- **Status** is one of *Proposed*, *Accepted*, *Deprecated*, or *Superseded*. A
  superseded ADR is not deleted; it is marked superseded and points to the ADR that
  replaces it, and the replacement points back.
- ADRs are immutable once accepted, except to change their status. A new decision is
  a new ADR, not an edit to an old one.

This ADR is the first record and establishes the practice for all that follow.
ADRs 0001–0008 below are the seed set carried over from the planning corpus
(`encore-plans/04-architecture.md` §ADR seeds), written at M0 per
`encore-plans/07-metrics-and-sequencing.md`.

## Consequences

- The reasoning behind structural decisions is preserved and versioned alongside the
  code it explains.
- Writing an ADR is a small, deliberate friction on consequential change — intended,
  since it makes reversing a load-bearing decision a visible act rather than an
  accident.
- ADRs add a modest maintenance habit. They are not a substitute for
  `docs/ROADMAP.md` or `docs/RESPONSIBLE-TECH-AUDITS.md` — they capture decisions,
  not the full design or the audit trail.
