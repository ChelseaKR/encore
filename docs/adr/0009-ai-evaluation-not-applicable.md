# 9. AI Evaluation standard: not applicable, with a named flip trigger

## Status

Accepted

## Context

The portfolio's standards regime requires every applicable standard to be
addressed, and every declared-N/A standard to carry both a reason and a
mechanism for noticing when the reason stops holding (CQ-45). Encore's README
and `docs/RESPONSIBLE-TECH-AUDITS.md` have both declared **AI Evaluation** N/A
since M0, but until now no ADR recorded that decision — the audit conducted
2026-07-05 flagged this as the one gap in an otherwise-correctly-declared N/A
(CQ-45 PARTIAL; AIEV-01 itself PASSes on the declaration content).

Encore contains no LLM, no model inference, and no AI/ML dependency of any
kind: the product is a deterministic pipeline (Plex sync → MusicBrainz
identity matching → release-group diffing → Apprise fan-out → ListenBrainz
collaborative-filtering lookups, none of which are "AI" in the sense the
standard means — collaborative-filtering similarity via ListenBrainz Labs is a
lookup against a precomputed public dataset, not a model Encore trains,
fine-tunes, hosts, or prompts).

## Decision

The AI Evaluation standard (AIEV, and RTF-09 through RTF-15 in the Responsible
Tech framework) is **not applicable** to Encore as of M0.

This is not a permanent exemption. It flips to **Applies** the day either of
these happens:

- **F14 ships** — the parked, deliberately-last "vibe" recommendations feature
  (`docs/ROADMAP.md` §3, "Beyond v1" / cut list) is the only planned feature
  that would introduce a model. It is scoped last specifically so that if it
  never ships, the product never has to carry AI-Evaluation machinery it
  doesn't need.
- **Any LLM/ML SDK import lands** in `src/encore/` for any other reason (e.g. a
  future contributor proposes using an LLM for something not yet imagined).

The flip is mechanically simple to notice: grep `src/encore/` for
`openai|anthropic|transformers|torch|tensorflow|sklearn` (or an equivalent
import-hygiene check) finding nothing is the AUTO half of this control; a new
ADR superseding this one, written the same day an LLM import is proposed (not
after it merges), is the REVIEW half.

## Consequences

- No AI risk register, EU AI Act classification, or model card exists, and
  none should be manufactured to look complete — an empty AI-Evaluation
  section would be worse than a reasoned N/A.
- The day F14 (or an equivalent) is proposed, this ADR is superseded by a new
  one that walks through: the model's provenance, whether it's called via API
  or hosted, what user data would be sent to it (taste data is exactly the
  sensitive category this product already treats carefully — see
  `docs/RESPONSIBLE-TECH-AUDITS.md` §B/§C), and the EU AI Act risk tier if the
  product ever has EU users.
- Until then, `docs/RESPONSIBLE-TECH-AUDITS.md`'s closing note ("RTF-09..15 are
  N/A-with-reason until F14") is the single source of truth for this decision's
  current status; this ADR is the record of *why*, not a second place that
  needs updating in lockstep.
