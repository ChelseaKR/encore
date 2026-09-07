"""`encore matches score` — reading a filled-in audit sheet back (issue #46).

`encore matches audit` writes the sample sheet the U8 validation spike needs
and nothing read it back, so the ≥90% figure M1 exits on would have been
computed by hand. These tests are about the three ways a scorer can invent
that figure rather than measure it:

* an **unlabelled** row counted as anything — it is neither right nor wrong,
  it is unexamined, and a rate that swallows it is a rate over a sample
  nobody finished;
* a label that is not a boolean **coerced by truthiness** — ``"correct":
  "yes"`` is truthy in Python, ``"correct": ""`` is falsy, and neither is a
  judgement a human entered;
* the **wrong denominator** — auto-match precision is a claim about ``auto``
  rows, and folding in ``pending``/``manual``/``skipped`` moves the number
  without measuring anything.

`test_a_real_audit_sheet_scores_as_entirely_unlabelled` is the one that ties
the two commands together: the sheet under test is written by
`encore matches audit` itself, so the format cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from encore.cli import main
from encore.matching.audit import (
    FIELD_PRECISION_TARGET,
    format_report,
    report_payload,
    score_audit,
)
from encore.matching.explain import audit_record, explain_match
from encore.models import MATCH_STATUSES
from encore.storage import Storage


def _sheet(*rows: dict[str, object]) -> str:
    """Render rows as the JSONL `matches audit` writes."""
    return "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)


def _auto(correct: object) -> dict[str, object]:
    return {"artist_key": "k", "status": "auto", "correct": correct}


class TestUnlabelledRowsAreNotDataPoints:
    """`correct: null` is an absence, and absence is not a measurement."""

    def test_an_unlabelled_auto_row_enters_neither_half(self) -> None:
        score = score_audit(_sheet(_auto(True), _auto(False), _auto(None)))
        assert score.auto.total == 3
        assert score.auto.labelled == 2
        assert score.auto.unlabelled == 1
        # 1 of 2 labelled, not 1 of 3 and not 2 of 3.
        assert score.auto.rate() == pytest.approx(0.5)

    def test_a_partly_labelled_sheet_reports_no_precision_at_all(self) -> None:
        score = score_audit(_sheet(_auto(True), _auto(None)))
        report = format_report(score)
        assert "not reported" in report
        assert "1 of 2 auto decisions are unlabelled" in report
        # The rate itself must not appear in the precision paragraph. (It is
        # scoped: coverage legitimately reports 100% here, because both rows
        # are auto rows — which is exactly the confusion worth guarding.)
        precision_section = report.split("Auto-match coverage")[0]
        assert "100.0%" not in precision_section

    def test_partial_prints_the_rate_and_the_gap_and_disclaims_the_criterion(self) -> None:
        score = score_audit(_sheet(_auto(True), _auto(True), _auto(None)))
        report = format_report(score, partial=True)
        assert "100.0%" in report
        assert "1 of 3 auto decisions are unlabelled" in report
        assert "not the U8 figure" in report

    def test_partial_over_a_wholly_unlabelled_sheet_invents_no_denominator(self) -> None:
        score = score_audit(_sheet(_auto(None), _auto(None)))
        report = format_report(score, partial=True)
        assert "none of the 2 auto decisions carry a label" in report
        assert score.precision() is None

    def test_json_precision_is_null_rather_than_a_number_to_distrust(self) -> None:
        payload = report_payload(score_audit(_sheet(_auto(True), _auto(None))))
        assert payload["precision"] is None
        assert payload["auto_unlabelled"] == 1
        assert payload["meets_target"] is None

    def test_a_status_with_no_rows_is_not_fully_labelled(self) -> None:
        # An empty tally answering "yes, fully labelled" would let a sheet
        # with no auto rows report a complete measurement.
        score = score_audit(_sheet({"status": "pending", "correct": None}))
        assert score.auto.total == 0
        assert score.auto.is_fully_labelled is False


class TestLabelsAreNotCoerced:
    """A non-boolean `correct` is refused by line number, never made truthy."""

    @pytest.mark.parametrize(
        "value",
        ["yes", "no", "", "true", 1, 0, ["true"], {"correct": True}],
    )
    def test_a_non_boolean_label_is_refused_not_coerced(self, value: object) -> None:
        score = score_audit(_sheet(_auto(value)))
        assert score.rows_read == 0
        assert [line.line_number for line in score.unreadable] == [1]
        assert "must be true, false or null" in score.unreadable[0].reason

    def test_an_absent_correct_key_reads_as_unlabelled_not_as_an_error(self) -> None:
        score = score_audit(_sheet({"artist_key": "k", "status": "auto"}))
        assert score.unreadable == ()
        assert score.auto.unlabelled == 1

    def test_the_refusal_names_the_line_number_in_a_long_sheet(self) -> None:
        rows = [_auto(True)] * 40 + [_auto("yes")] + [_auto(True)] * 9
        score = score_audit(_sheet(*rows))
        assert [line.line_number for line in score.unreadable] == [41]

    def test_unparseable_and_non_object_lines_are_counted_not_dropped(self) -> None:
        score = score_audit(json.dumps(_auto(True)) + "\n" + "{not json\n" + "[1, 2, 3]\n" + "\n")
        assert score.rows_read == 1
        assert [line.line_number for line in score.unreadable] == [2, 3]
        assert "not valid JSON" in score.unreadable[0].reason
        assert score.unreadable[1].reason == "not a JSON object"

    def test_an_unknown_status_is_refused_rather_than_tallied_under_a_new_key(self) -> None:
        score = score_audit(_sheet({"status": "probably", "correct": True}))
        assert score.tallies == {}
        assert "unknown decision status" in score.unreadable[0].reason


class TestTheDenominatorIsAutoRowsOnly:
    """Auto-match precision is a claim about decisions nobody was asked about."""

    def test_manual_pending_and_skipped_rows_stay_out_of_the_rate(self) -> None:
        score = score_audit(
            _sheet(
                _auto(True),
                _auto(False),
                {"status": "manual", "correct": True},
                {"status": "manual", "correct": True},
                {"status": "pending", "correct": None},
                {"status": "skipped", "correct": False},
            )
        )
        assert score.rows_read == 6
        assert score.auto.total == 2
        assert score.precision() == pytest.approx(0.5)

    def test_every_schema_status_is_accepted_and_tallied_separately(self) -> None:
        rows: list[dict[str, object]] = [{"status": s, "correct": None} for s in MATCH_STATUSES]
        score = score_audit(_sheet(*rows))
        assert set(score.tallies) == set(MATCH_STATUSES)
        assert score.rows_read == len(MATCH_STATUSES)

    def test_a_sheet_with_no_auto_rows_says_so_instead_of_scoring_zero(self) -> None:
        score = score_audit(_sheet({"status": "pending", "correct": None}))
        report = format_report(score)
        assert "no auto decisions in this sheet" in report
        assert score.precision() is None
        assert "0.0%" not in report.split("Auto-match coverage")[0]


class TestCoverageIsNotPrecision:
    """Two different figures that a reader will otherwise conflate."""

    def test_coverage_counts_all_readable_decisions_and_precision_does_not(self) -> None:
        score = score_audit(
            _sheet(
                _auto(True),
                _auto(True),
                {"status": "pending", "correct": None},
                {"status": "pending", "correct": None},
            )
        )
        assert score.coverage() == pytest.approx(0.5)
        assert score.precision() == pytest.approx(1.0)

    def test_the_report_says_which_is_which(self) -> None:
        report = format_report(score_audit(_sheet(_auto(True))))
        assert "Coverage is not precision" in report

    def test_coverage_is_none_rather_than_zero_on_an_empty_sheet(self) -> None:
        assert score_audit("").coverage() is None


class TestTheTargetVerdict:
    """`met` / `NOT met` is reported only over a completely labelled sample."""

    def test_exactly_the_target_counts_as_met(self) -> None:
        rows = [_auto(True)] * 9 + [_auto(False)]
        score = score_audit(_sheet(*rows))
        assert score.precision() == pytest.approx(FIELD_PRECISION_TARGET)
        assert "met on this sample" in format_report(score)
        assert report_payload(score)["meets_target"] is True

    def test_just_below_the_target_is_reported_as_not_met(self) -> None:
        rows = [_auto(True)] * 8 + [_auto(False)] * 2
        score = score_audit(_sheet(*rows))
        assert "NOT met" in format_report(score)
        assert report_payload(score)["meets_target"] is False

    def test_a_partial_score_never_produces_a_verdict(self) -> None:
        rows = [_auto(True)] * 9 + [_auto(False)] + [_auto(None)]
        payload = report_payload(score_audit(_sheet(*rows)), partial=True)
        assert payload["precision"] == pytest.approx(FIELD_PRECISION_TARGET)
        assert payload["precision_is_partial"] is True
        # A rate exists, but the criterion is a claim about the whole sample.
        assert payload["meets_target"] is None


class TestTheCommand:
    """Exit codes and the CLI wiring."""

    def test_a_clean_sheet_prints_the_rate_and_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sheet = tmp_path / "audit.jsonl"
        sheet.write_text(_sheet(*([_auto(True)] * 9 + [_auto(False)])), encoding="utf-8")
        assert main(["matches", "score", "--in", str(sheet)]) == 0
        assert "90.0%" in capsys.readouterr().out

    def test_an_unreadable_line_exits_non_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sheet = tmp_path / "audit.jsonl"
        sheet.write_text(_sheet(_auto(True)) + "{oops\n", encoding="utf-8")
        assert main(["matches", "score", "--in", str(sheet)]) == 1
        assert "line 2" in capsys.readouterr().out

    def test_an_unlabelled_sheet_still_exits_zero_because_it_read_cleanly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sheet = tmp_path / "audit.jsonl"
        sheet.write_text(_sheet(_auto(None), _auto(True)), encoding="utf-8")
        assert main(["matches", "score", "--in", str(sheet)]) == 0
        assert "not reported" in capsys.readouterr().out

    def test_an_empty_sheet_exits_non_zero_rather_than_scoring_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sheet = tmp_path / "audit.jsonl"
        sheet.write_text("\n\n", encoding="utf-8")
        assert main(["matches", "score", "--in", str(sheet)]) == 1
        assert "Nothing to score" in capsys.readouterr().out

    def test_a_missing_file_is_an_error_not_an_empty_result(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["matches", "score", "--in", str(tmp_path / "nope.jsonl")]) == 1
        assert "cannot read" in capsys.readouterr().err

    def test_json_output_is_machine_readable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sheet = tmp_path / "audit.jsonl"
        sheet.write_text(_sheet(_auto(True), _auto(False)), encoding="utf-8")
        assert main(["matches", "score", "--in", str(sheet), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["precision"] == pytest.approx(0.5)
        assert payload["precision_target"] == pytest.approx(FIELD_PRECISION_TARGET)


class TestItScoresWhatAuditActuallyWrites:
    """The two halves must not drift apart — one writes, the other reads."""

    def test_a_real_audit_sheet_scores_as_entirely_unlabelled(self, tmp_path: Path) -> None:
        storage = Storage(tmp_path / "data")
        try:
            storage.save_artist_match(
                artist_key="rk-1",
                artist_name="Mogwai",
                status="auto",
                mbid="11111111-1111-1111-1111-111111111111",
                confidence=0.97,
            )
            storage.save_artist_match(
                artist_key="rk-2",
                artist_name="Nadja",
                status="pending",
                mbid=None,
                confidence=0.42,
            )
            rows = storage.list_artist_matches()
        finally:
            storage.close()
        text = "".join(
            json.dumps(audit_record(explain_match(row)), sort_keys=True) + "\n" for row in rows
        )
        score = score_audit(text)
        assert score.unreadable == ()
        assert score.rows_read == 2
        # A freshly written sheet is a question, not an answer.
        assert score.auto.unlabelled == 1
        assert score.precision() is None
        assert "not reported" in format_report(score)
