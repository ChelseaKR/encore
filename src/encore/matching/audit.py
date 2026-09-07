"""Score a filled-in `encore matches audit` sheet (M1 exit U8, issue #46).

`encore matches audit` writes the sample sheet: one JSON object per stored
decision, evidence included, with a ``correct`` field left null for a human
to fill in. Nothing read it back. The path to M1's "≥90% auto-match on the
reference library" therefore ran through a number computed by hand, and a
hand-computed number that gets published is not reproducible by the person
reading it.

This module is the other half. It is deliberately strict about three things,
because each of them is a way to publish an absence as a measurement:

**An unlabelled row is not a data point.** ``correct: null`` means "nobody
has looked at this yet", which is a different fact from "this decision was
wrong" and from "this decision was right". It enters neither the numerator
nor the denominator, and the report says how many there were. A rate over a
partly-labelled sheet is reported only when it is asked for by name
(``--partial``) and never without its coverage stated alongside it.

**A label that is not a boolean is not a label.** ``"correct": "yes"`` is
truthy in Python and would silently score as a correct match. It is refused
by line number instead, along with anything else the line cannot be read
from — bad JSON, a non-object, an unknown ``status``.

**Precision and coverage are different numbers.** ADR-0006 and the roadmap's
§7 metrics table both ask for auto-match *precision*: of the decisions the
matcher made without asking, how many were right. "How many artists
auto-matched at all" is a useful figure too, and it is not that one, so it
is reported separately and labelled. Only ``auto`` rows enter the precision
denominator — folding ``manual``, ``pending`` or ``skipped`` rows into it
would move the rate without measuring anything.

Nothing here decides whether the criterion is met. That is `docs/adr/0006`'s
open question (rebalance the threshold, or freeze it), it needs the library
and the labels first, and it belongs to the maintainer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from encore.models import MATCH_STATUSES

__all__ = [
    "AUTO_MATCH_STATUS",
    "FIELD_PRECISION_TARGET",
    "AuditScore",
    "StatusTally",
    "UnreadableLine",
    "format_report",
    "read_audit_sheet",
    "report_payload",
    "score_audit",
]

# The status whose rows carry the U8 precision claim. A row with any other
# status is a decision a human was involved in (or declined to make), so it
# says nothing about how well the matcher decides on its own.
AUTO_MATCH_STATUS = "auto"

# The M1 exit criterion's target (docs/ROADMAP.md §8, docs/adr/0006). Stated
# here so the report can say "met"/"not met" against a number that is written
# down, rather than one a reader has to remember.
FIELD_PRECISION_TARGET = 0.90


@dataclass(frozen=True)
class UnreadableLine:
    """One line the scorer refused, with the reason, by line number."""

    line_number: int
    reason: str


@dataclass(frozen=True)
class StatusTally:
    """How one decision status was labelled across the sheet."""

    correct: int = 0
    incorrect: int = 0
    unlabelled: int = 0

    @property
    def total(self) -> int:
        """Every readable row with this status, labelled or not."""
        return self.correct + self.incorrect + self.unlabelled

    @property
    def labelled(self) -> int:
        """Rows a human has actually judged."""
        return self.correct + self.incorrect

    @property
    def is_fully_labelled(self) -> bool:
        """Whether every row of this status carries a label.

        A status with no rows at all is *not* fully labelled: there is
        nothing to have labelled, and answering "yes" would let an empty
        sheet report a complete measurement.
        """
        return self.total > 0 and self.unlabelled == 0

    def rate(self) -> float | None:
        """Correct share of the labelled rows, or ``None`` when none are.

        ``None`` rather than ``0.0``: no labels is not a score of zero, and
        the two must not render the same way.
        """
        if self.labelled == 0:
            return None
        return self.correct / self.labelled


@dataclass(frozen=True)
class AuditScore:
    """Everything one sheet supports, with its own gaps counted."""

    tallies: dict[str, StatusTally]
    unreadable: tuple[UnreadableLine, ...]
    lines_seen: int

    @property
    def rows_read(self) -> int:
        """Readable decision rows across every status."""
        return sum(tally.total for tally in self.tallies.values())

    @property
    def auto(self) -> StatusTally:
        """The ``auto`` tally — the only one U8's precision is about."""
        return self.tallies.get(AUTO_MATCH_STATUS, StatusTally())

    def precision(self) -> float | None:
        """Auto-match precision over labelled ``auto`` rows, or ``None``."""
        return self.auto.rate()

    def coverage(self) -> float | None:
        """Share of readable decisions that auto-matched, or ``None``.

        A different figure from `precision` and never a substitute for it:
        this says how often the matcher decided without asking, not how
        often it was right. ``None`` when the sheet holds no decisions.
        """
        if self.rows_read == 0:
            return None
        return self.auto.total / self.rows_read


def _read_correct(payload: dict[str, object]) -> tuple[bool | None, str | None]:
    """Read one row's ``correct`` field, refusing anything that is not a label.

    Absent and ``null`` both mean unlabelled — `audit_record` writes the key
    explicitly as null, and an older sheet may not carry it at all; neither
    is a judgement. Every other type is refused rather than coerced: a bare
    string, a 0/1, or a "y" would all evaluate as truthy or falsy in Python
    and silently become a data point nobody entered.
    """
    if "correct" not in payload:
        return None, None
    raw = payload["correct"]
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return raw, None
    return None, f"'correct' must be true, false or null, not {json.dumps(raw)}"


def read_audit_sheet(text: str) -> tuple[list[tuple[str, bool | None]], list[UnreadableLine]]:
    """Parse a JSONL audit sheet into ``(status, correct)`` rows plus refusals.

    Blank lines are skipped silently — a trailing newline is not an error.
    Every other line either yields a row or an `UnreadableLine` naming what
    was wrong with it; nothing is dropped without being counted.
    """
    rows: list[tuple[str, bool | None]] = []
    unreadable: list[UnreadableLine] = []
    for index, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload: object = json.loads(line)
        except json.JSONDecodeError as exc:
            unreadable.append(UnreadableLine(index, f"not valid JSON ({exc.msg})"))
            continue
        if not isinstance(payload, dict):
            unreadable.append(UnreadableLine(index, "not a JSON object"))
            continue
        status_raw = payload.get("status")
        if not isinstance(status_raw, str) or status_raw not in MATCH_STATUSES:
            unreadable.append(
                UnreadableLine(
                    index,
                    f"unknown decision status {json.dumps(status_raw)}; "
                    f"expected one of {', '.join(MATCH_STATUSES)}",
                )
            )
            continue
        correct, problem = _read_correct(payload)
        if problem is not None:
            unreadable.append(UnreadableLine(index, problem))
            continue
        rows.append((status_raw, correct))
    return rows, unreadable


def score_audit(text: str) -> AuditScore:
    """Tally one audit sheet by status, keeping every refusal."""
    rows, unreadable = read_audit_sheet(text)
    counters: dict[str, list[int]] = {}
    for status, correct in rows:
        bucket = counters.setdefault(status, [0, 0, 0])
        if correct is None:
            bucket[2] += 1
        elif correct:
            bucket[0] += 1
        else:
            bucket[1] += 1
    tallies = {
        status: StatusTally(correct=values[0], incorrect=values[1], unlabelled=values[2])
        for status, values in counters.items()
    }
    return AuditScore(
        tallies=tallies,
        unreadable=tuple(unreadable),
        lines_seen=len(rows) + len(unreadable),
    )


def _percent(value: float) -> str:
    """Render a share as a percentage with one decimal place."""
    return f"{value * 100:.1f}%"


def _status_lines(score: AuditScore) -> list[str]:
    """One line per decision status present, in the schema's own order."""
    lines: list[str] = []
    for status in MATCH_STATUSES:
        tally = score.tallies.get(status)
        if tally is None:
            continue
        noun = "row" if tally.total == 1 else "rows"
        lines.append(
            f"  {status:<8} {tally.total:>5} {noun} — "
            f"{tally.correct} correct, {tally.incorrect} wrong, "
            f"{tally.unlabelled} unlabelled"
        )
    return lines


def _precision_lines(score: AuditScore, *, partial: bool) -> list[str]:
    """Render the precision paragraph, or the reason there is not one."""
    auto = score.auto
    if auto.total == 0:
        return [
            "Auto-match precision: no auto decisions in this sheet.",
            "  Nothing was scored. An empty denominator is not a rate, and this is"
            " not evidence for or against the U8 criterion.",
        ]
    rate = auto.rate()
    if auto.is_fully_labelled and rate is not None:
        met = "met" if rate >= FIELD_PRECISION_TARGET else "NOT met"
        return [
            f"Auto-match precision: {auto.correct} of {auto.labelled} auto decisions"
            f" correct — {_percent(rate)}.",
            "  Every auto decision in this sheet is labelled.",
            f"  M1 exit (U8) asks for ≥{_percent(FIELD_PRECISION_TARGET)}: {met} on this sample.",
        ]
    if not partial:
        return [
            f"Auto-match precision: not reported — {auto.unlabelled} of {auto.total}"
            " auto decisions are unlabelled.",
            "  A rate over a partly-labelled sample is not the U8 figure. Fill in"
            " the remaining `correct` fields,",
            "  or pass --partial to score only the ones that are labelled.",
        ]
    if rate is None:
        return [
            f"Auto-match precision (partial): none of the {auto.total} auto decisions"
            " carry a label.",
            "  There is nothing to score. --partial does not invent a denominator.",
        ]
    return [
        f"Auto-match precision (partial): {auto.correct} of {auto.labelled} labelled"
        f" auto decisions correct — {_percent(rate)}.",
        f"  {auto.unlabelled} of {auto.total} auto decisions are unlabelled and are"
        " counted in neither half.",
        "  This is not the U8 figure: that criterion is a claim about the whole sample.",
    ]


def format_report(score: AuditScore, *, partial: bool = False) -> str:
    """Render the human-readable report for `encore matches score`."""
    lines: list[str] = [
        f"Read {score.lines_seen} line(s): {score.rows_read} decision(s),"
        f" {len(score.unreadable)} unreadable."
    ]
    for refused in score.unreadable:
        lines.append(f"  line {refused.line_number}: {refused.reason}")
    if score.rows_read == 0:
        lines.append("")
        lines.append("Nothing to score.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Decisions by status:")
    lines.extend(_status_lines(score))
    lines.append("")
    lines.extend(_precision_lines(score, partial=partial))
    coverage = score.coverage()
    if coverage is not None:
        lines.append("")
        lines.append(
            f"Auto-match coverage: {score.auto.total} of {score.rows_read} decisions"
            f" auto-matched — {_percent(coverage)}."
        )
        lines.append(
            "  Coverage is not precision: it says how often the matcher decided without asking,"
        )
        lines.append("  not how often it was right.")
    return "\n".join(lines)


def report_payload(score: AuditScore, *, partial: bool = False) -> dict[str, object]:
    """Render the same report as JSON, for ``--json``.

    ``precision`` is ``null`` unless a rate was actually computed, and
    ``precision_is_partial`` says which of the two it is. A consumer that
    reads ``precision`` without reading ``auto_unlabelled`` gets a null
    rather than a number it would have to know not to trust.
    """
    auto = score.auto
    reportable = auto.is_fully_labelled or (partial and auto.labelled > 0)
    rate = auto.rate() if reportable else None
    return {
        "lines_seen": score.lines_seen,
        "rows_read": score.rows_read,
        "unreadable": [
            {"line": refused.line_number, "reason": refused.reason} for refused in score.unreadable
        ],
        "by_status": {
            status: {
                "total": tally.total,
                "correct": tally.correct,
                "incorrect": tally.incorrect,
                "unlabelled": tally.unlabelled,
            }
            for status, tally in sorted(score.tallies.items())
        },
        "auto_total": auto.total,
        "auto_labelled": auto.labelled,
        "auto_unlabelled": auto.unlabelled,
        "precision": rate,
        "precision_is_partial": bool(rate is not None and not auto.is_fully_labelled),
        "precision_target": FIELD_PRECISION_TARGET,
        "meets_target": (
            rate >= FIELD_PRECISION_TARGET if rate is not None and auto.is_fully_labelled else None
        ),
        "coverage": score.coverage(),
    }
