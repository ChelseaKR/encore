# How matching decides

What `encore match` does when it binds one of your Plex artists to a
MusicBrainz identity, and how to read `encore matches explain` when you
disagree with it.

Every number on this page is checked against `src/encore/matching/scoring.py`
by `tests/test_matching_explain.py::TestTheDocumentedConstants`. If someone
changes a constant and not this page, the build fails — the figures here
cannot quietly stop being true, which is the failure mode a page like this
otherwise has.

## The score

Each candidate MusicBrainz returned gets one score, the sum of five terms.
`encore matches explain --artist-key KEY` prints exactly these, with the
reason each has the value it does, and their sum is held against the scorer's
own result by a test.

| Term | Range | What it means |
| --- | --- | --- |
| `name` | 0 – 1.00 | Name similarity after normalization (diacritics stripped, casefolded, `&` and `and` unified). **1.00** for an exact match, **0.95** for an exact match on one of the artist's aliases, otherwise a fuzzy ratio bounded by a **0.85** ceiling. |
| `mb_prior` | 0 – 0.05 | MusicBrainz's own search score for the row, out of 100, weighted **0.05**. It breaks ties; it cannot carry a decision. |
| `type_hint` | −0.10 / 0 / +0.03 | **+0.03** when a known artist type corroborates, **−0.10** when it contradicts, 0 when either side is unknown. |
| `country_hint` | −0.10 / 0 / +0.03 | The same, for country. |
| `guid_boost` | 0 or +0.15 | **+0.15** when Plex's own metadata agent already named this exact MBID **and** the `name` term is already at least 0.95. |

The penalty for a contradicting hint (**0.10**) is deliberately larger than
the bonus for a corroborating one (**0.03**): a hint that disagrees is
evidence of a different artist, while a hint that agrees is only mild
confirmation of a name that already matched.

## The decision

- **Auto-match** requires the best score to reach **0.90** *and* to lead the
  runner-up by at least **0.03**.
- Anything else goes to the **review queue** — including a candidate that
  clears 0.90 but is inside the margin. Two artists genuinely called the same
  thing are a question for a person, not a coin flip.
- No candidates at all is also a review-queue outcome, recorded as
  `no-candidates` so it reads differently from "scored badly".

The ranking and the margin both use the **raw** score, which can exceed 1.0
once the GUID boost applies; what is stored beside each candidate is that
score capped at 1.0. `explain` shows the raw sum, because that is the number
the decision was made on.

## Why the GUID boost is gated on the name

`guid_boost` used to apply to any candidate whose MBID matched Plex's GUID,
regardless of how well the name matched. Because the 0.85 ceiling bounds the
*name term* and never bounded the composite, any fuzzy ratio above roughly
0.882 crossed the 0.90 threshold on the boost alone — a one-character
difference could auto-match (issue #32).

It is now gated on the name term already being at least 0.95, which makes the
stated invariant true: **a GUID strengthens a plausible candidate; it does not
rescue a name mismatch.** When the boost is withheld for that reason,
`explain` says so in the term's own line rather than showing a silent zero.

## Reading an explanation

`reason` is recorded at match time and is one of:

| `reason` | Meaning |
| --- | --- |
| `auto-matched` | Cleared the threshold and the margin. |
| `ambiguous` | Cleared the threshold; the runner-up was inside the margin. |
| `below-threshold` | Nothing reached 0.90. |
| `no-candidates` | MusicBrainz returned nothing to score. |
| `unrecorded` | The decision predates this field. Not a fifth outcome — it means the row cannot say. Re-run `encore match` for that artist to record it. |

`evidence` says how much of the arithmetic the stored row can support:
`complete` (hints and the scorer's inputs were both kept), `candidates-only`
(scores were stored, the inputs behind them were not, so no breakdown is
shown), or `none` (this artist has never been through the matcher).

An explanation never re-queries MusicBrainz. It is a reading of what is on
disk, so it shows the evidence as it stood when the decision was made rather
than as upstream has since revised it.

## These thresholds are provisional

They are set from the 23-case fixture battery in
`tests/test_matching_engine.py`, which gates ≥95% correct decisions and zero
wrong auto-matches on every run. The ≥90% *field* rate on a real library is
the U8 validation spike, which has not run (issues #45, #46) — until it does,
these numbers are a defensible starting point and not a measured optimum.

`encore matches audit --out audit.jsonl` writes every current decision with
its evidence and an empty `correct` column, which is the sample sheet that
spike needs. `encore matches score --in audit.jsonl` reads it back once the
column is filled in and computes the rate, so the figure that eventually
freezes these thresholds is reproducible from the sheet rather than from
somebody's arithmetic. It scores `auto` rows only, and it does not print a
rate while any of them are unlabelled.
