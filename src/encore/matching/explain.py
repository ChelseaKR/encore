"""`encore matches explain` — the evidence behind one match decision.

The review queue says *that* a match was uncertain. It does not say why, and
an auto-match that is wrong (the issue #32 GUID-boost class) is invisible until
a wrong release alert arrives. This module turns a stored decision back into
the arithmetic that produced it: the hints the scorer was given, every
candidate with its score broken into named terms, and the sentence that
decided the outcome.

Two rules run through all of it.

**It re-derives, it does not narrate.** Each candidate's terms come from
`scoring.explain_candidate`, and their sum is checked against
`scoring.score_candidate` by a test over every fixture. An explanation that
does not add up to the number beside it is a story about a different
computation, and it reads as authoritative while being wrong.

**What was not recorded is reported as not recorded.** A decision written
before the evidence columns existed has no hints, so the breakdown genuinely
cannot be recomputed — that is said in one line, not filled in with defaults
that would produce a confident and fictional table. Likewise an artist with no
stored candidates explains as "never matched", not as an empty score table
whose zeroes look like measurements. The recorded `decision_reason` is the
authority on what was decided; the recomputed numbers are labelled as
recomputed at today's threshold, because the threshold in force at match time
was not stored and this module will not pretend otherwise.

Nothing here opens a socket. Explain never re-queries MusicBrainz: it is a
reading of what is already on disk, which is what makes it usable on the
evidence as it stood rather than as upstream has since revised it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from encore.matching.mb import ArtistCandidate
from encore.matching.scoring import (
    AMBIGUITY_MARGIN,
    AUTO_MATCH_THRESHOLD,
    ArtistHints,
    ScoreTerm,
    explain_candidate,
)
from encore.models import ArtistMatch

__all__ = [
    "CandidateExplanation",
    "Explanation",
    "audit_record",
    "explain_match",
    "render_json",
    "render_text",
]

# How much of the arithmetic the stored row can support.
EVIDENCE_COMPLETE = "complete"  # hints and candidate inputs both present
EVIDENCE_CANDIDATES_ONLY = "candidates-only"  # scores recorded, hints were not
EVIDENCE_NONE = "none"  # nothing stored: never matched

# Appended when the gap had to be computed from stored, clamped scores rather
# than the raw ones `decide` ranked on. Saying so is the difference between a
# number and the number.
_CLAMPED_NOTE = (
    " (computed from the stored scores, which are capped at 1.0; the matcher ranked on "
    "the uncapped values, so the real gap may be larger)"
)


@dataclass(frozen=True)
class CandidateExplanation:
    """One candidate, as recorded and (where possible) as re-derived."""

    mbid: str
    name: str
    recorded_score: float | None
    artist_type: str | None
    country: str | None
    disambiguation: str | None
    mb_score: int | None
    chosen: bool
    terms: tuple[ScoreTerm, ...] = ()
    recomputed_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Render this candidate as the object `--json` emits for it."""
        return {
            "mbid": self.mbid,
            "name": self.name,
            "recorded_score": self.recorded_score,
            "recomputed_score": self.recomputed_score,
            "type": self.artist_type,
            "country": self.country,
            "disambiguation": self.disambiguation,
            "mb_score": self.mb_score,
            "chosen": self.chosen,
            "terms": [{"name": t.name, "value": t.value, "detail": t.detail} for t in self.terms],
        }


@dataclass(frozen=True)
class Explanation:
    """Everything this repository can say about one match decision."""

    artist_key: str
    artist_name: str
    status: str
    mbid: str | None
    confidence: float | None
    reason: str
    evidence: str
    deciding: str
    candidates: tuple[CandidateExplanation, ...]
    threshold: float
    ambiguity_margin: float
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Render the whole explanation as the object `--json` emits."""
        return {
            "artist_key": self.artist_key,
            "artist_name": self.artist_name,
            "status": self.status,
            "mbid": self.mbid,
            "confidence": self.confidence,
            "reason": self.reason,
            "evidence": self.evidence,
            "deciding": self.deciding,
            "threshold": self.threshold,
            "ambiguity_margin": self.ambiguity_margin,
            "notes": list(self.notes),
            "candidates": [c.as_dict() for c in self.candidates],
        }


def _stored_candidates(row: ArtistMatch) -> list[dict[str, Any]]:
    if not row.candidates_json:
        return []
    try:
        raw: object = json.loads(row.candidates_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _stored_hints(row: ArtistMatch) -> ArtistHints | None:
    """Return the hints recorded at match time, or `None` when none were.

    `None` is a real answer here, not a default to paper over: without the
    hints the GUID boost and both hint terms cannot be re-derived at all.
    """
    if not row.hints_json:
        return None
    try:
        raw: object = json.loads(row.hints_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        return None
    return ArtistHints(
        name=raw["name"],
        guid_mbid=raw.get("guid_mbid") if isinstance(raw.get("guid_mbid"), str) else None,
        type_hint=raw.get("type_hint") if isinstance(raw.get("type_hint"), str) else None,
        country_hint=raw.get("country_hint") if isinstance(raw.get("country_hint"), str) else None,
    )


def _candidate_from_entry(entry: dict[str, Any]) -> ArtistCandidate | None:
    """Rebuild the scorer's input, or `None` when the row predates keeping it."""
    if not isinstance(entry.get("mbid"), str) or not isinstance(entry.get("name"), str):
        return None
    if not isinstance(entry.get("mb_score"), int):
        # Written before `_candidates_to_json` kept the scorer's inputs. The
        # recorded score stands; the breakdown does not exist to be shown.
        return None
    aliases = entry.get("aliases")
    return ArtistCandidate(
        mbid=entry["mbid"],
        name=entry["name"],
        sort_name=entry.get("sort_name") or "",
        mb_score=entry["mb_score"],
        artist_type=entry.get("type"),
        country=entry.get("country"),
        disambiguation=entry.get("disambiguation"),
        aliases=tuple(a for a in aliases if isinstance(a, str))
        if isinstance(aliases, list)
        else (),
    )


def _ranking_score(candidate: CandidateExplanation) -> float | None:
    """Return the score `decide` actually ranked on, when it can be known.

    `decide` ranks and applies the ambiguity margin on the **raw** score, but
    what is stored beside each candidate is `min(raw, 1.0)`. Two candidates
    that both clamp to 1.0 are stored as a tie and were not one, so a gap
    computed from the stored numbers can read 0.000 for a decision the matcher
    made on 0.004. The recomputed score is the raw one, so prefer it and fall
    back to the clamped value only when the evidence to recompute is absent.
    """
    if candidate.recomputed_score is not None:
        return candidate.recomputed_score
    return candidate.recorded_score


def _deciding_sentence(
    reason: str,
    candidates: tuple[CandidateExplanation, ...],
    threshold: float,
    margin: float,
) -> str:
    """One sentence naming what actually decided this, in the reason's terms."""
    if reason == "no-candidates":
        return "MusicBrainz returned no candidate for this name, so there was nothing to score."
    if reason == "unrecorded":
        return (
            "This decision predates the recorded reason, so why it came out this way is "
            "not knowable from the row; re-run `encore match` to record it."
        )
    if not candidates:
        return "No candidate was stored, so the decision cannot be attributed to one."
    best = candidates[0]
    best_score = _ranking_score(best)
    if best_score is None:
        return "The candidate list was stored without scores."
    runner_up = _ranking_score(candidates[1]) if len(candidates) > 1 else None
    clamped = best.recomputed_score is None
    if reason == "auto-matched":
        if len(candidates) == 1:
            return (
                f"{best.name} scored {best_score:.3f}, clearing the {threshold:.2f} "
                "threshold, and was the only candidate."
            )
        gap = best_score - (runner_up if runner_up is not None else 0.0)
        return (
            f"{best.name} scored {best_score:.3f}, clearing the {threshold:.2f} threshold, "
            f"and led the runner-up by {gap:.3f} (at or above the {margin:.2f} margin)."
            + (_CLAMPED_NOTE if clamped else "")
        )
    if reason == "ambiguous":
        gap = best_score - (runner_up if runner_up is not None else 0.0)
        return (
            f"{best.name} scored {best_score:.3f}, which clears the {threshold:.2f} "
            f"threshold, but the runner-up is only {gap:.3f} behind — inside the "
            f"{margin:.2f} ambiguity margin, so this went to review rather than being "
            "guessed." + (_CLAMPED_NOTE if clamped else "")
        )
    short = threshold - best_score
    if best.terms:
        weakest = min(best.terms, key=lambda t: t.value)
        return (
            f"The best candidate {best.name} scored {best_score:.3f}, {short:.3f} short of "
            f"the {threshold:.2f} threshold. The lowest term was {weakest.name} at "
            f"{weakest.value:+.3f}: {weakest.detail}."
        )
    return (
        f"The best candidate {best.name} scored {best_score:.3f}, {short:.3f} short of the "
        f"{threshold:.2f} threshold."
    )


def explain_match(
    row: ArtistMatch,
    threshold: float = AUTO_MATCH_THRESHOLD,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> Explanation:
    """Turn one stored decision back into the evidence behind it."""
    entries = _stored_candidates(row)
    hints = _stored_hints(row)
    reason = row.decision_reason or "unrecorded"
    notes: list[str] = []

    if not entries:
        evidence = EVIDENCE_NONE
        if reason == "unrecorded":
            notes.append(
                "No candidate was ever stored for this artist: it has not been through the "
                "matcher, or it was matched before candidates were kept. This is not the "
                "same as a search that returned nothing."
            )
    elif hints is None:
        evidence = EVIDENCE_CANDIDATES_ONLY
        notes.append(
            "The hints the scorer was given were not recorded on this row, so the per-term "
            "breakdown cannot be re-derived. The scores below are the ones stored at match "
            "time. Re-run `encore match` for this artist to record the full evidence."
        )
    else:
        evidence = EVIDENCE_COMPLETE

    candidates: list[CandidateExplanation] = []
    incomplete_inputs = False
    for index, entry in enumerate(entries):
        candidate = _candidate_from_entry(entry) if hints is not None else None
        terms: tuple[ScoreTerm, ...] = ()
        recomputed: float | None = None
        if hints is not None and candidate is not None:
            recomputed, terms = explain_candidate(hints, candidate)
        elif hints is not None:
            incomplete_inputs = True
        score = entry.get("score")
        mb_score = entry.get("mb_score")
        candidates.append(
            CandidateExplanation(
                mbid=str(entry.get("mbid", "")),
                name=str(entry.get("name", "")),
                recorded_score=float(score) if isinstance(score, int | float) else None,
                artist_type=entry.get("type"),
                country=entry.get("country"),
                disambiguation=entry.get("disambiguation"),
                mb_score=mb_score if isinstance(mb_score, int) else None,
                chosen=row.mbid is not None and entry.get("mbid") == row.mbid,
                terms=terms,
                recomputed_score=recomputed,
                # `index` is unused beyond ordering; entries are already ranked.
            )
        )
        del index

    if incomplete_inputs:
        evidence = EVIDENCE_CANDIDATES_ONLY
        notes.append(
            "Some candidates were stored before the scorer's own inputs were kept, so their "
            "terms cannot be re-derived. Their recorded scores stand."
        )

    if reason == "unrecorded" and entries:
        notes.append(
            "This decision predates the recorded reason. The scores are the stored ones; the "
            "sentence below is inferred from them rather than read from the row."
        )

    return Explanation(
        artist_key=row.artist_key,
        artist_name=row.artist_name,
        status=row.status,
        mbid=row.mbid,
        confidence=row.confidence,
        reason=reason,
        evidence=evidence,
        deciding=_deciding_sentence(reason, tuple(candidates), threshold, ambiguity_margin),
        candidates=tuple(candidates),
        threshold=threshold,
        ambiguity_margin=ambiguity_margin,
        notes=tuple(notes),
    )


def render_text(explanation: Explanation) -> str:
    """Render the operator's report for one artist."""
    lines = [
        f"artist:     {explanation.artist_name}",
        f"key:        {explanation.artist_key}",
        f"status:     {explanation.status}"
        + (f" → {explanation.mbid}" if explanation.mbid else ""),
        f"reason:     {explanation.reason}",
        f"threshold:  {explanation.threshold:.2f} auto, {explanation.ambiguity_margin:.2f} "
        "ambiguity margin (current values, not necessarily those in force at match time)",
        f"evidence:   {explanation.evidence}",
        "",
    ]
    for note in explanation.notes:
        lines.append(f"note: {note}")
    if explanation.notes:
        lines.append("")

    if not explanation.candidates:
        lines.append("candidates: none stored — this artist has never been matched.")
    else:
        lines.append("candidates, best first:")
        for candidate in explanation.candidates:
            marker = "*" if candidate.chosen else " "
            recorded = (
                f"{candidate.recorded_score:.4f}"
                if candidate.recorded_score is not None
                else "  n/a"
            )
            extras = ", ".join(
                part
                for part in (
                    candidate.artist_type,
                    candidate.country,
                    candidate.disambiguation,
                )
                if part
            )
            lines.append(
                f" {marker} {recorded}  {candidate.name}  [{candidate.mbid}]"
                + (f"  ({extras})" if extras else "")
            )
            for term in candidate.terms:
                lines.append(f"        {term.value:+.4f}  {term.name:<13} {term.detail}")
            if candidate.terms and candidate.recomputed_score is not None:
                lines.append(
                    f"        {candidate.recomputed_score:+.4f}  {'total':<13} "
                    "sum of the terms above, uncapped as the matcher ranks it"
                )
    lines.append("")
    lines.append(f"deciding:   {explanation.deciding}")
    return "\n".join(lines)


def render_json(explanation: Explanation) -> dict[str, Any]:
    """Build the `--json` document for one artist."""
    return {"schema": "encore.matches.explain/1", **explanation.as_dict()}


def audit_record(explanation: Explanation) -> dict[str, Any]:
    """One JSONL line for `encore matches audit`.

    Carries the artist name and MBID on purpose — the whole point of the file
    is that a human can label a sample and compute the field-precision number
    M1's exit criterion still lacks. It carries no Plex token, no Plex URL and
    no log text, because none of those is evidence about a match.
    """
    best = explanation.candidates[0] if explanation.candidates else None
    runner_up = explanation.candidates[1] if len(explanation.candidates) > 1 else None
    return {
        "artist_key": explanation.artist_key,
        "artist_name": explanation.artist_name,
        "status": explanation.status,
        "mbid": explanation.mbid,
        "confidence": explanation.confidence,
        "reason": explanation.reason,
        "evidence": explanation.evidence,
        "candidate_count": len(explanation.candidates),
        "best_score": best.recorded_score if best else None,
        "runner_up_score": runner_up.recorded_score if runner_up else None,
        "deciding": explanation.deciding,
        # The column a human fills in. Present and empty so the file is a
        # sample sheet rather than something a reviewer has to reshape first.
        "correct": None,
    }
