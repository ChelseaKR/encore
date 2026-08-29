"""Scoring/decision unit tests (F2): normalization, boosts, thresholds, margins."""

from __future__ import annotations

from encore.matching.mb import ArtistCandidate
from encore.matching.scoring import (
    AUTO_MATCH_THRESHOLD,
    ArtistHints,
    decide,
    normalize_name,
    score_candidate,
)


def test_normalize_name_unifies_diacritics_case_and_punctuation() -> None:
    assert normalize_name("Motörhead") == normalize_name("Motorhead")
    assert normalize_name("AC/DC") == normalize_name("AC DC")
    assert normalize_name("Simon & Garfunkel") == normalize_name("Simon and Garfunkel")
    assert normalize_name("The Beatles") == normalize_name("the   beatles")
    assert normalize_name("Sigur Rós") == normalize_name("sigur ros")
    # Non-Latin scripts survive normalization (casefolded, not romanized).
    assert normalize_name("Мумий Тролль") == normalize_name("мумий тролль")


def test_exact_name_outscores_alias_outscores_fuzzy() -> None:
    hints = ArtistHints(name="Pink")
    exact = ArtistCandidate(mbid="a", name="Pink")
    alias = ArtistCandidate(mbid="b", name="P!nk Officially", aliases=("Pink",))
    fuzzy = ArtistCandidate(mbid="c", name="Pinku")
    assert score_candidate(hints, exact) > score_candidate(hints, alias)
    assert score_candidate(hints, alias) > score_candidate(hints, fuzzy)


def test_fuzzy_alone_cannot_cross_auto_threshold() -> None:
    # Even a perfect fuzzy ratio is capped below the auto threshold: a
    # non-exact, non-alias name never auto-matches without corroboration.
    hints = ArtistHints(name="Radiohed")
    candidate = ArtistCandidate(mbid="a", name="Radiohead", mb_score=100)
    assert score_candidate(hints, candidate) < AUTO_MATCH_THRESHOLD


def test_guid_boost_is_bounded_not_a_review_skip() -> None:
    # A GUID pointing at a name-mismatched candidate must not rescue it.
    hints = ArtistHints(name="Radiohead", guid_mbid="mb-coldplay")
    mismatched = ArtistCandidate(mbid="mb-coldplay", name="Coldplay", mb_score=100)
    assert score_candidate(hints, mismatched) < AUTO_MATCH_THRESHOLD
    decision = decide(hints, [mismatched])
    assert decision.status == "pending"


def test_guid_boost_cannot_rescue_a_near_miss_name() -> None:
    # Regression, issue #32. The pre-existing bounds test above pairs Radiohead
    # with *Coldplay* — so unrelated that the fuzzy component alone is nowhere
    # near the threshold, and the test passed whether or not the boost was
    # bounded. The break is a NEAR miss: one added character. `_FUZZY_CEILING`
    # bounds only the name component, so before the fix +0.15 stacked on top of
    # it and any fuzzy ratio >= ~0.882 auto-matched on the GUID alone.
    hints = ArtistHints(name="Radiohead", guid_mbid="mb-radioheads")
    near_miss = ArtistCandidate(mbid="mb-radioheads", name="Radioheads", mb_score=100)
    assert score_candidate(hints, near_miss) < AUTO_MATCH_THRESHOLD
    assert decide(hints, [near_miss]).status == "pending"


def test_guid_boost_still_disambiguates_two_exact_name_homonyms() -> None:
    # The other direction: gating the boost on name quality must not disarm it
    # where it is supposed to work. Both candidates match the name exactly, so
    # the GUID is the only signal that separates them.
    hints = ArtistHints(name="Nirvana", guid_mbid="mb-nirvana-us")
    us = ArtistCandidate(mbid="mb-nirvana-us", name="Nirvana", mb_score=95)
    gb = ArtistCandidate(mbid="mb-nirvana-gb", name="Nirvana", mb_score=100)
    decision = decide(hints, [us, gb])
    assert decision.status == "auto"
    assert decision.chosen is not None and decision.chosen.mbid == "mb-nirvana-us"


def test_guid_boost_applies_to_an_exact_alias_match() -> None:
    # An exact alias is a name match, not a mismatch, so the boost applies.
    hints = ArtistHints(name="Florence + The Machine", guid_mbid="mb-florence")
    candidate = ArtistCandidate(
        mbid="mb-florence",
        name="Florence and the Machine",
        aliases=("Florence + The Machine",),
        mb_score=90,
    )
    assert score_candidate(hints, candidate) > AUTO_MATCH_THRESHOLD
    assert decide(hints, [candidate]).status == "auto"


def test_contradicting_hints_penalize_matching_hints_reward() -> None:
    hints = ArtistHints(name="Bush", country_hint="GB", type_hint="Group")
    matching = ArtistCandidate(mbid="a", name="Bush", country="GB", artist_type="Group")
    contradicting = ArtistCandidate(mbid="b", name="Bush", country="CA", artist_type="Person")
    unknown = ArtistCandidate(mbid="c", name="Bush")
    assert score_candidate(hints, matching) > score_candidate(hints, unknown)
    assert score_candidate(hints, unknown) > score_candidate(hints, contradicting)


def test_decide_empty_candidates_is_pending_with_zero_confidence() -> None:
    decision = decide(ArtistHints(name="Nobody"), [])
    assert decision.status == "pending"
    assert decision.chosen is None
    assert decision.confidence == 0.0
    assert decision.ranked == ()


def test_decide_near_tie_goes_to_review_even_above_threshold() -> None:
    hints = ArtistHints(name="Nirvana")
    a = ArtistCandidate(mbid="a", name="Nirvana", mb_score=100)
    b = ArtistCandidate(mbid="b", name="Nirvana", mb_score=95)
    decision = decide(hints, [a, b])
    assert decision.status == "pending"
    assert decision.chosen is None
    # Ranked list is preserved (best first) for the review queue to display.
    assert [candidate.mbid for candidate, _ in decision.ranked] == ["a", "b"]


def test_decide_confidence_is_clamped_but_ranking_uses_raw_scores() -> None:
    hints = ArtistHints(name="Nirvana", guid_mbid="a")
    a = ArtistCandidate(mbid="a", name="Nirvana", mb_score=100)
    b = ArtistCandidate(mbid="b", name="Nirvana", mb_score=95)
    decision = decide(hints, [a, b])
    # The GUID boost breaks the tie on raw scores (both clamp to 1.0).
    assert decision.status == "auto"
    assert decision.chosen is not None and decision.chosen.mbid == "a"
    assert decision.confidence == 1.0
    assert all(score <= 1.0 for _, score in decision.ranked)


def test_decide_single_exact_candidate_auto_matches() -> None:
    decision = decide(
        ArtistHints(name="Radiohead"),
        [ArtistCandidate(mbid="a", name="Radiohead", mb_score=100)],
    )
    assert decision.status == "auto"
    assert decision.chosen is not None and decision.chosen.mbid == "a"
    assert decision.confidence >= AUTO_MATCH_THRESHOLD
