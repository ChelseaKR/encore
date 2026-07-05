# 4. Server-rendered htmx UI, no SPA

## Status

Accepted

## Context

Encore's UI surface (onboarding wizard, review queue, settings, recommendation
browse page) needs live-feeling interactivity — a progress bar during the initial
17-minute match run, inline review-queue actions, dismissible recommendation cards —
but nothing in F1–F13 requires client-side application state, offline capability, or
a component framework. A SPA (React/Vue + a TS toolchain) would roughly double the
gated CI surface this project's standards conformance requires: a second linter, a
second type checker, a second test runner, and a second, much larger accessibility
battery, none of which the actual UI complexity here justifies.

## Decision

The UI is server-rendered HTML (Jinja2 templates) progressively enhanced with
[htmx](https://htmx.org/) for the interactive pieces (live progress updates, inline
review-queue confirm/skip, dismiss actions). No SPA framework, no client-side build
step, no TypeScript.

## Consequences

- The portfolio's frontend code-quality battery (CQ-13..18: ESLint, Vitest,
  Playwright, bundle size-limit) is out of scope by construction — real HTML means
  the accessibility gates (axe/pa11y/Lighthouse) carry the UI quality bar instead,
  which is both cheaper to run and a better match for what a server-rendered app
  needs.
- Real HTML also makes WCAG 2.2 AA dramatically more tractable: correct landmarks,
  labels, and focus order are close to free with semantic markup and require
  active work to get wrong, the inverse of a client-rendered app.
- **Relaxation rule:** revisit only if a shipped feature demonstrably needs rich
  client-side interactivity that htmx cannot express reasonably (a candidate:
  a force-directed recommendation graph, F7/F8). Even then, the answer is an
  *island* of client-side code around that one feature, not a framework migration
  of the whole app.
