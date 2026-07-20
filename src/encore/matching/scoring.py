"""Confidence scoring and the auto-match/review decision (F2).

The score composes, per the roadmap's F2 specification:

- **Name similarity** carries the decision: normalized exact match (1.0),
  exact alias match (0.95), else a bounded fuzzy ratio (≤0.85 — fuzzy alone
  can never cross the auto-match threshold without corroboration).
- **MusicBrainz's own search score** is a small prior (≤0.05).
- **Type/country hints** add small bonuses or a larger penalty on
  contradiction — a hint can disambiguate homonyms, not overrule the name.
- **A Plex-GUID MBID is a score boost only, never a review skip** (+0.15):
  it strengthens a plausible candidate but cannot rescue a name mismatch.

Two candidates within `AMBIGUITY_MARGIN` of each other (homonyms) always go
to review — a wrong auto-match is strictly worse than a queued one.

**Thresholds are provisional.** `AUTO_MATCH_THRESHOLD` is frozen only after
the ≥90% field-rate validation spike on a real library (roadmap U8, human
input); the fixture battery in `tests/test_matching_engine.py` gates the
≥95% fixture-precision half of the acceptance criterion in CI.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from encore.matching.mb import ArtistCandidate

__all__ = [
    "AMBIGUITY_MARGIN",
    "AUTO_MATCH_THRESHOLD",
    "ArtistHints",
    "MatchDecision",
    "decide",
    "normalize_name",
    "score_candidate",
]

# Provisional until the U8 validation spike freezes them (see module docstring).
AUTO_MATCH_THRESHOLD = 0.90
AMBIGUITY_MARGIN = 0.03

_ALIAS_SCORE = 0.95
_FUZZY_CEILING = 0.85
_MB_PRIOR_WEIGHT = 0.05
_HINT_BONUS = 0.03
_HINT_PENALTY = 0.10
_GUID_BOOST = 0.15


@dataclass(frozen=True)
class ArtistHints:
    """What we know about an artist going into matching.

    ``guid_mbid`` is an MBID extracted from a Plex GUID when Plex's own
    metadata agent already identified the artist — a boost signal only.
    """

    name: str
    guid_mbid: str | None = None
    type_hint: str | None = None
    country_hint: str | None = None


@dataclass(frozen=True)
class MatchDecision:
    """The outcome of scoring one artist's candidates.

    ``status`` is ``"auto"`` (chosen ≥ threshold, unambiguous) or
    ``"pending"`` (review queue). ``confidence`` is the chosen/best raw score
    clamped to [0, 1]; ``ranked`` pairs every candidate with its clamped
    score, best first, for the review queue to display.
    """

    status: str
    chosen: ArtistCandidate | None
    confidence: float
    ranked: tuple[tuple[ArtistCandidate, float], ...] = field(default_factory=tuple)


def normalize_name(name: str) -> str:
    """Normalize for comparison: strip diacritics, casefold, unify punctuation.

    ``Motörhead`` and ``Motorhead`` compare equal; ``AC/DC`` and ``AC DC``
    compare equal; ``&`` and ``and`` compare equal. Non-Latin scripts pass
    through casefolded but otherwise intact.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = stripped.casefold().replace("&", " and ")
    cleaned = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return " ".join(cleaned.split())


def _name_component(hint_name: str, candidate: ArtistCandidate) -> float:
    """Name similarity: exact (1.0) > alias exact (0.95) > bounded fuzzy (≤0.85)."""
    target = normalize_name(hint_name)
    if target == normalize_name(candidate.name):
        return 1.0
    if any(target == normalize_name(alias) for alias in candidate.aliases):
        return _ALIAS_SCORE
    ratios = [SequenceMatcher(None, target, normalize_name(candidate.name)).ratio()]
    ratios.extend(
        SequenceMatcher(None, target, normalize_name(alias)).ratio() for alias in candidate.aliases
    )
    return max(ratios) * _FUZZY_CEILING


def _hint_component(hint: str | None, actual: str | None) -> float:
    """Small bonus when a hint corroborates, larger penalty when it contradicts."""
    if hint is None or actual is None:
        return 0.0
    if hint.casefold() == actual.casefold():
        return _HINT_BONUS
    return -_HINT_PENALTY


def score_candidate(hints: ArtistHints, candidate: ArtistCandidate) -> float:
    """Raw (uncapped) confidence score for one candidate.

    Raw scores are used for ranking and the ambiguity margin so a boost is
    never erased by clamping; persist ``min(score, 1.0)`` as the confidence.
    """
    score = _name_component(hints.name, candidate)
    score += (candidate.mb_score / 100) * _MB_PRIOR_WEIGHT
    score += _hint_component(hints.type_hint, candidate.artist_type)
    score += _hint_component(hints.country_hint, candidate.country)
    if hints.guid_mbid is not None and hints.guid_mbid == candidate.mbid:
        score += _GUID_BOOST
    return score


def decide(
    hints: ArtistHints,
    candidates: list[ArtistCandidate],
    auto_threshold: float = AUTO_MATCH_THRESHOLD,
    ambiguity_margin: float = AMBIGUITY_MARGIN,
) -> MatchDecision:
    """Score all candidates and decide: auto-match or review queue.

    Auto requires the best raw score to clear ``auto_threshold`` AND to lead
    the runner-up by ``ambiguity_margin`` — near-ties (homonyms) go to
    review even when both clear the threshold. No candidates → review.
    """
    if not candidates:
        return MatchDecision(status="pending", chosen=None, confidence=0.0)
    scored = sorted(
        ((candidate, score_candidate(hints, candidate)) for candidate in candidates),
        key=lambda pair: pair[1],
        reverse=True,
    )
    ranked = tuple((candidate, min(raw, 1.0)) for candidate, raw in scored)
    best_candidate, best_raw = scored[0]
    unambiguous = len(scored) == 1 or (best_raw - scored[1][1]) >= ambiguity_margin
    if best_raw >= auto_threshold and unambiguous:
        return MatchDecision(
            status="auto",
            chosen=best_candidate,
            confidence=min(best_raw, 1.0),
            ranked=ranked,
        )
    return MatchDecision(
        status="pending",
        chosen=None,
        confidence=min(best_raw, 1.0),
        ranked=ranked,
    )
