"""`encore matches explain` — the evidence behind a match decision.

The invariant that makes any of this trustworthy is
`test_the_terms_sum_to_the_score`: the breakdown printed beside a score has
to add up to that score. An explanation that does not is a story about a
different computation, and it is worse than no explanation because it reads
as authoritative.

The rest divides in two. What the row *does* record, explained exactly — over
all 23 fixture cases, not a hand-picked one. And what the row does *not*
record, reported as not recorded: a decision written before the evidence
columns existed, an artist that was never matched at all, and candidates
stored before the scorer's inputs were kept. None of those may be filled in
with defaults, because zeroes in a score table look exactly like measurements.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from encore.matching.engine import MatchEngine
from encore.matching.explain import (
    EVIDENCE_CANDIDATES_ONLY,
    EVIDENCE_COMPLETE,
    EVIDENCE_NONE,
    audit_record,
    explain_match,
    render_json,
    render_text,
)
from encore.matching.mb import ArtistCandidate, MusicBrainzClient, RateLimiter
from encore.matching.scoring import (
    AUTO_MATCH_THRESHOLD,
    DECISION_REASONS,
    ArtistHints,
    decide,
    decision_reason,
    explain_candidate,
    score_candidate,
)
from encore.storage import Storage
from tests.mb_fixtures import MATCH_CASES, mb_artist, mb_search_response


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _engine(storage: Storage) -> MatchEngine:
    client = MusicBrainzClient(rate_limiter=RateLimiter(min_interval=0), sleep=lambda _s: None)
    return MatchEngine(storage, client)


class TestTheArithmeticIsTheArithmetic:
    """The explanation must equal the thing it explains."""

    def test_the_terms_sum_to_the_score(self) -> None:
        # Over every candidate in every fixture, with every hint combination
        # the fixtures carry. `explain_candidate` re-derives each addend
        # independently of `score_candidate`, so this compares two
        # computations rather than a function against itself.
        checked = 0
        for case in MATCH_CASES:
            artists = case.response.get("artists")
            assert isinstance(artists, list)
            for entry in artists:
                candidate = ArtistCandidate(
                    mbid=str(entry["id"]),
                    name=str(entry["name"]),
                    mb_score=int(entry.get("score", 0)),
                    artist_type=entry.get("type"),
                    country=entry.get("country"),
                    disambiguation=entry.get("disambiguation"),
                    aliases=tuple(a["name"] for a in entry.get("aliases", [])),
                )
                total, terms = explain_candidate(case.hints, candidate)
                assert total == pytest.approx(score_candidate(case.hints, candidate))
                assert sum(term.value for term in terms) == pytest.approx(total)
                checked += 1
        assert checked >= len(MATCH_CASES), "the fixtures stopped carrying candidates"

    def test_every_term_carries_a_reason(self) -> None:
        candidate = ArtistCandidate(mbid="mb-1", name="Radiohead", mb_score=100)
        _total, terms = explain_candidate(ArtistHints(name="Radiohead"), candidate)
        assert {t.name for t in terms} == {
            "name",
            "mb_prior",
            "type_hint",
            "country_hint",
            "guid_boost",
        }
        for term in terms:
            assert term.detail, f"{term.name} has a value and no reason for it"

    def test_the_guid_boost_explains_why_it_did_not_apply(self) -> None:
        # Issue #32: the boost used to rescue a fuzzy name. The explanation
        # has to say that it was withheld and why, or the operator reading a
        # near-miss cannot tell this case from "no GUID at all".
        candidate = ArtistCandidate(mbid="mb-1", name="Radiohed", mb_score=90)
        _total, terms = explain_candidate(
            ArtistHints(name="Radiohead", guid_mbid="mb-1"), candidate
        )
        boost = next(t for t in terms if t.name == "guid_boost")
        assert boost.value == 0.0
        assert "does not rescue a name mismatch" in boost.detail


class TestTheDecisionReason:
    def test_it_agrees_with_decide_over_every_fixture(self) -> None:
        # `decision_reason` reads the same inputs a second time. It is only
        # trustworthy while the two readings agree, so this holds them
        # together rather than asserting a list of expected strings.
        for case in MATCH_CASES:
            artists = case.response.get("artists")
            assert isinstance(artists, list)
            candidates = [
                ArtistCandidate(
                    mbid=str(e["id"]),
                    name=str(e["name"]),
                    mb_score=int(e.get("score", 0)),
                    artist_type=e.get("type"),
                    country=e.get("country"),
                    disambiguation=e.get("disambiguation"),
                    aliases=tuple(a["name"] for a in e.get("aliases", [])),
                )
                for e in artists
            ]
            outcome = decide(case.hints, candidates)
            reason = decision_reason(outcome)
            assert reason in DECISION_REASONS
            if outcome.status == "auto":
                assert reason == "auto-matched"
            elif not candidates:
                assert reason == "no-candidates"
            else:
                best = outcome.ranked[0][1]
                expected = "below-threshold" if best < AUTO_MATCH_THRESHOLD else "ambiguous"
                assert reason == expected, f"{case.case_id}: {reason} for best {best}"

    def test_unrecorded_is_not_one_of_the_real_reasons(self) -> None:
        # A row that predates the column must be distinguishable from every
        # decision that was actually made.
        assert "unrecorded" in DECISION_REASONS
        for case in MATCH_CASES[:3]:
            outcome = decide(case.hints, [])
            assert decision_reason(outcome) != "unrecorded"


class TestExplainingAStoredDecision:
    def test_every_fixture_case_explains_with_complete_evidence(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        # The issue's headline criterion: all 23 cases reproduce their
        # recorded decision and name what decided it.
        explained = 0
        for case in MATCH_CASES:
            httpx_mock.add_response(json=case.response)
            row = _engine(storage).match_artist(f"key-{case.case_id}", case.hints)
            explanation = explain_match(row)

            assert explanation.status == row.status
            assert explanation.mbid == row.mbid
            assert explanation.reason == row.decision_reason
            assert explanation.reason != "unrecorded", case.case_id
            assert explanation.deciding, case.case_id
            if explanation.candidates:
                assert explanation.evidence == EVIDENCE_COMPLETE, case.case_id
                for candidate in explanation.candidates:
                    assert candidate.terms, f"{case.case_id}: {candidate.name} has no breakdown"
                    assert candidate.recomputed_score is not None
                    assert sum(t.value for t in candidate.terms) == pytest.approx(
                        candidate.recomputed_score
                    )
            explained += 1
        assert explained == len(MATCH_CASES) == 23

    def test_the_chosen_candidate_is_marked(self, storage: Storage, httpx_mock: HTTPXMock) -> None:
        case = next(c for c in MATCH_CASES if c.expected_status == "auto")
        httpx_mock.add_response(json=case.response)
        row = _engine(storage).match_artist("key-auto", case.hints)
        explanation = explain_match(row)
        chosen = [c for c in explanation.candidates if c.chosen]
        assert len(chosen) == 1
        assert chosen[0].mbid == row.mbid

    def test_the_gap_uses_the_uncapped_score_the_matcher_ranked_on(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        # `decide` ranks on the raw score; the stored score is min(raw, 1.0).
        # Two candidates that both cap at 1.0 are stored as a tie and were not
        # one, so a gap read off the stored numbers says 0.000 for a decision
        # made on something else.
        response = mb_search_response(
            mb_artist("mb-a", "Bush", 100, "Group", "GB"),
            mb_artist("mb-b", "Bush", 92, "Group", "CA"),
        )
        httpx_mock.add_response(json=response)
        row = _engine(storage).match_artist("key-tie", ArtistHints(name="Bush"))
        explanation = explain_match(row)

        assert [c.recorded_score for c in explanation.candidates] == [1.0, 1.0]
        assert "0.000 behind" not in explanation.deciding
        assert "0.004 behind" in explanation.deciding


class TestWhatWasNotRecorded:
    def test_an_artist_that_was_never_matched_says_so(self, storage: Storage) -> None:
        # Not an empty score table. "Never matched" and "matched, found
        # nothing" are different findings and must not render the same.
        row = storage.save_artist_match("key-never", "Nobody", "pending")
        explanation = explain_match(row)

        assert explanation.evidence == EVIDENCE_NONE
        assert explanation.candidates == ()
        assert explanation.reason == "unrecorded"
        report = render_text(explanation)
        assert "never been matched" in report
        assert "0.0000" not in report, "an absent score was rendered as a number"

    def test_a_row_without_hints_reports_that_it_cannot_break_the_score_down(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        case = next(c for c in MATCH_CASES if c.expected_status == "auto")
        httpx_mock.add_response(json=case.response)
        row = _engine(storage).match_artist("key-old", case.hints)
        # Simulate a decision written before the evidence columns existed.
        storage.save_artist_match(
            "key-old", row.artist_name, row.status, row.mbid, row.confidence, row.candidates_json
        )
        with storage.session() as session:
            from sqlmodel import select

            from encore.models import ArtistMatch

            stale = session.exec(
                select(ArtistMatch).where(ArtistMatch.artist_key == "key-old")
            ).one()
            stale.hints_json = None
            stale.decision_reason = None
            session.add(stale)
            session.commit()

        reloaded = storage.get_artist_match("key-old")
        assert reloaded is not None
        explanation = explain_match(reloaded)
        assert explanation.evidence == EVIDENCE_CANDIDATES_ONLY
        assert explanation.reason == "unrecorded"
        assert all(c.terms == () for c in explanation.candidates)
        assert all(c.recomputed_score is None for c in explanation.candidates)
        assert any("cannot be re-derived" in note for note in explanation.notes)
        # The recorded scores still stand — they were measured, just not
        # decomposable.
        assert all(c.recorded_score is not None for c in explanation.candidates)

    def test_candidates_stored_before_the_scorer_inputs_were_kept(self, storage: Storage) -> None:
        legacy = json.dumps([{"mbid": "mb-1", "name": "Radiohead", "score": 0.95, "type": "Group"}])
        row = storage.save_artist_match(
            "key-legacy",
            "Radiohead",
            "auto",
            "mb-1",
            0.95,
            legacy,
            hints_json=json.dumps({"name": "Radiohead"}),
            decision_reason="auto-matched",
        )
        explanation = explain_match(row)

        assert explanation.evidence == EVIDENCE_CANDIDATES_ONLY
        assert explanation.candidates[0].terms == ()
        assert explanation.candidates[0].recorded_score == 0.95
        assert any("before the scorer's own inputs" in note for note in explanation.notes)


class TestTheRenderers:
    def test_the_json_carries_the_terms_and_a_schema_tag(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        case = next(c for c in MATCH_CASES if c.expected_status == "auto")
        httpx_mock.add_response(json=case.response)
        row = _engine(storage).match_artist("key-json", case.hints)
        document = render_json(explain_match(row))

        assert document["schema"] == "encore.matches.explain/1"
        assert document["reason"] == row.decision_reason
        assert document["candidates"][0]["terms"], "the JSON dropped the breakdown"
        json.dumps(document)  # must be serialisable as-is

    def test_the_text_report_names_the_threshold_as_current_not_historical(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        # The threshold in force at match time was never stored. Printing
        # today's as though it were the one used would be the same class of
        # error this module exists to avoid.
        case = MATCH_CASES[0]
        httpx_mock.add_response(json=case.response)
        row = _engine(storage).match_artist("key-th", case.hints)
        report = render_text(explain_match(row))
        assert "not necessarily those in force at match time" in report


class TestTheAuditFile:
    def test_one_record_per_decision_with_a_label_column(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        for case in MATCH_CASES[:5]:
            httpx_mock.add_response(json=case.response)
            _engine(storage).match_artist(f"key-{case.case_id}", case.hints)

        rows = storage.list_artist_matches()
        assert len(rows) == 5
        records = [audit_record(explain_match(row)) for row in rows]
        for record in records:
            assert record["correct"] is None, "the label column must start empty"
            assert record["artist_name"], "the sample sheet needs the name to be labellable"
            assert record["reason"] in DECISION_REASONS
            json.dumps(record)

    def test_it_includes_auto_matches_not_only_the_review_queue(
        self, storage: Storage, httpx_mock: HTTPXMock
    ) -> None:
        # A wrong auto-match is exactly what the sample exists to find, so
        # filtering to the review queue would make the audit unable to measure
        # the thing it is for.
        auto = next(c for c in MATCH_CASES if c.expected_status == "auto")
        pending = next(c for c in MATCH_CASES if c.expected_status == "pending")
        for case in (auto, pending):
            httpx_mock.add_response(json=case.response)
            _engine(storage).match_artist(f"key-{case.case_id}", case.hints)

        statuses = {row.status for row in storage.list_artist_matches()}
        assert statuses == {"auto", "pending"}

    def test_it_carries_no_plex_credential(self, storage: Storage, httpx_mock: HTTPXMock) -> None:
        storage.set_plex_credentials("http://plex.local:32400", "plex-token-abcdef")
        case = MATCH_CASES[0]
        httpx_mock.add_response(json=case.response)
        _engine(storage).match_artist("key-cred", case.hints)

        blob = json.dumps([audit_record(explain_match(r)) for r in storage.list_artist_matches()])
        assert "plex-token-abcdef" not in blob
        assert "plex.local" not in blob


class TestTheDocumentedConstants:
    """`docs/how-matching-decides.md`'s figures, held against the scorer.

    A page explaining a scorer is the kind of document that stops being true
    without anyone noticing: the constant moves, the prose does not, and the
    page keeps reading like an explanation. Every number quoted there is
    derived from `scoring.py` here, so that cannot happen quietly.
    """

    PAGE = Path(__file__).resolve().parent.parent / "docs" / "how-matching-decides.md"

    def _text(self) -> str:
        return self.PAGE.read_text(encoding="utf-8")

    def test_the_page_exists_and_names_the_command(self) -> None:
        text = self._text()
        assert "encore matches explain" in text
        assert "encore matches audit" in text

    @pytest.mark.parametrize(
        ("attribute", "rendering"),
        [
            ("AUTO_MATCH_THRESHOLD", "0.90"),
            ("AMBIGUITY_MARGIN", "0.03"),
            ("_ALIAS_SCORE", "0.95"),
            ("_FUZZY_CEILING", "0.85"),
            ("_MB_PRIOR_WEIGHT", "0.05"),
            ("_HINT_BONUS", "0.03"),
            ("_HINT_PENALTY", "0.10"),
            ("_GUID_BOOST", "0.15"),
        ],
    )
    def test_each_quoted_constant_is_the_real_one(self, attribute: str, rendering: str) -> None:
        from encore.matching import scoring

        # The rendering in the table has to be the value in the module, so a
        # changed constant fails here rather than leaving stale prose behind.
        assert float(rendering) == pytest.approx(getattr(scoring, attribute))
        assert rendering in self._text(), f"{attribute} is not quoted as {rendering}"

    def test_every_reason_is_documented(self) -> None:
        text = self._text()
        for reason in DECISION_REASONS:
            assert f"`{reason}`" in text, f"{reason} is not explained on the page"

    def test_every_evidence_level_is_documented(self) -> None:
        text = self._text()
        for level in (EVIDENCE_COMPLETE, EVIDENCE_CANDIDATES_ONLY, EVIDENCE_NONE):
            assert f"`{level}`" in text, f"{level} is not explained on the page"

    def test_every_score_term_is_documented(self) -> None:
        candidate = ArtistCandidate(mbid="mb-1", name="Radiohead", mb_score=100)
        _total, terms = explain_candidate(ArtistHints(name="Radiohead"), candidate)
        text = self._text()
        for term in terms:
            assert f"`{term.name}`" in text, f"{term.name} is not in the terms table"
