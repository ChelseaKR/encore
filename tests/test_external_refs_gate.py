"""Gates for the external-reference ledger and the two documents it freed.

Issue #22: `docs/ROADMAP.md` and `docs/RESPONSIBLE-TECH-AUDITS.md` delegated the
feature plan, the architecture, the market research and the M0-M4 exit criteria
to a planning corpus that is not a git repository, not a submodule, and has
never been in this repository's history. `make external-refs` was built as a
ratchet over that: a ceiling per file, so counts can fall freely and cannot
rise without a reviewable ledger edit.

Two things needed testing once those two documents reached zero.

The ratchet had a blind spot in the direction it is supposed to protect. `_scan`
skipped a file the moment its count hit zero, so a ledger entry left behind at
its old ceiling was never reported, and an entry for a file that is no longer
tracked was never visited at all. Either one silently pre-authorizes references
nobody has reviewed -- the ratchet slipping back without a diff.

And the resolution itself is prose, which nothing reads. Section 3 reconstructs
F0-F14 from the tree, so every row is pinned to a file that backs it -- the first
draft of that section claimed F11-F13 were "not named anywhere in this
repository", and all three are, which is why a reconstruction needs a gate as
much as a delegation did. Section 4's admission that its market claim has no
published evidence, and section 8's that it owns the exit criteria rather than
deferring them, are exactly the sentences a later tidying pass deletes, so they
are asserted here too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.external_refs_gate import _count_refs, _load_ledger, _scan

REPO = Path(__file__).resolve().parents[1]
ROADMAP = REPO / "docs" / "ROADMAP.md"
AUDITS = REPO / "docs" / "RESPONSIBLE-TECH-AUDITS.md"

# The two documents issue #22 called load-bearing: the ones that carried the
# exit criteria and the feature plan.
FREED = ("docs/ROADMAP.md", "docs/RESPONSIBLE-TECH-AUDITS.md")


def _ledger() -> dict[str, int]:
    return _load_ledger(REPO)[0]


def _roots() -> list[str]:
    """Read the external root names from the ledger, never a literal here.

    Spelling the directory name in this module would put a reference to the
    outside path into a committed file -- which is the thing being gated, and
    the gate caught exactly that on this file's first run.
    """
    return _load_ledger(REPO)[1]


# -- the ratchet's blind spot ------------------------------------------------


def test_a_ceiling_for_a_file_with_no_references_left_is_reported() -> None:
    """A tracked file at zero must not keep a ceiling it no longer earns."""
    # README.md carries no external reference, so any ceiling on it is slack.
    _, slack, _ = _scan(REPO, {"README.md": 4}, _roots())
    assert any("README.md" in note and "delete the entry" in note for note in slack), slack


def test_a_ceiling_for_a_file_git_no_longer_tracks_is_reported() -> None:
    """An entry for a path outside the tree is never visited by the file loop."""
    _, slack, _ = _scan(REPO, {"docs/plans/never-committed.md": 1}, _roots())
    assert any("never-committed.md" in note and "no longer tracked" in note for note in slack)


def test_an_earned_ceiling_is_not_reported_as_slack() -> None:
    """The note fires on unearned ceilings only, or it is noise nobody reads."""
    live = {path: count for path, count in _ledger().items() if count}
    assert live, "the ledger is empty; this test would pass vacuously"
    _, slack, _ = _scan(REPO, live, _roots())
    assert slack == []


def test_zero_is_not_written_as_a_ceiling() -> None:
    """An absent path already means zero, so a 0 entry is a line that lies."""
    assert [path for path, count in _ledger().items() if count == 0] == []


# -- the two documents issue #22 freed ---------------------------------------


@pytest.mark.parametrize("path", FREED)
def test_the_freed_documents_hold_no_external_reference(path: str) -> None:
    text = (REPO / path).read_text(encoding="utf-8")
    assert _count_refs(text, _roots(), repo_depth=path.count("/")) == 0


@pytest.mark.parametrize("path", FREED)
def test_the_freed_documents_hold_no_ledger_entry(path: str) -> None:
    """No entry at all, not an entry set to 0 -- absence is the pin."""
    assert path not in _ledger()


# -- the admissions that resolved them ---------------------------------------


@pytest.mark.parametrize("label", [f"F{n}" for n in range(15)])
def test_every_feature_row_is_backed_by_the_file_it_cites(label: str) -> None:
    """Section 3 reconstructs F0-F14 from the tree; each row must survive a check.

    The unpublished plan made this section's first draft assert that F11-F13
    were "not named anywhere in this repository". They are: F11 is in the
    responsible-tech data inventory, F12 in ADR-0007 and the Definition of Done,
    F13 in ADR-0005's relaxation rule. Writing down what a document *lacks* is
    as falsifiable as writing down what it has, and is just as capable of being
    wrong, so every row is pinned to a file that mentions the same label.
    """
    row = _feature_row(ROADMAP.read_text(encoding="utf-8"), label)
    assert row is not None, f"section 3 lost its {label} row"
    cited = re.findall(r"`([^`]+\.md|(?:src|docs)/[^`]*)`", row)
    backing = {ref: file for ref in cited if (file := _resolve(ref)) is not None}
    assert backing, f"{label} cites no resolvable file: {cited}"
    # A parked feature is only as real as the file that names it. A built one is
    # code, so its label need not appear in the module that implements it.
    if "**parked**" in row:
        naming = [ref for ref, file in backing.items() if label in file.read_text(encoding="utf-8")]
        assert naming, f"{label} is parked but no file it cites mentions it: {sorted(backing)}"


def test_the_roadmap_admits_the_ordering_is_not_reconstructible() -> None:
    """The list came back; the ranking and cut list did not. Say which is which."""
    section = _section(ROADMAP.read_text(encoding="utf-8"), "## 3. Product definition")
    assert "What is missing is the ranking, not the list" in section


def test_the_roadmap_labels_the_market_claim_a_premise_not_evidence() -> None:
    """Section 4's claim rests on a document a reader cannot open.

    A verification status nobody can check is a claim, not a finding. The
    section has to say which one it is.
    """
    section = _section(ROADMAP.read_text(encoding="utf-8"), "## 4. Research & evidence")
    assert "No published evidence backs this section" in section
    assert "premise, not as\nresearch" in section or "premise, not as research" in section


def test_section_8_owns_the_exit_criteria_rather_than_deferring_them() -> None:
    section = _section(ROADMAP.read_text(encoding="utf-8"), "## 8. Implementation plan")
    assert "exit criteria below are the ones this repository is held to" in section


def test_every_path_section_3_cites_exists() -> None:
    """A table of paths is a committed artifact standing in for a computation.

    Exactly the drift #41 gated elsewhere: nothing reads it, so it rots. Derive
    it from the tree instead.
    """
    section = _section(ROADMAP.read_text(encoding="utf-8"), "## 3. Product definition")
    cited = set(re.findall(r"`((?:src|docs)/[^`]+)`", section))
    assert len(cited) >= 15, f"section 3 lost its location column: {cited}"
    missing = sorted(ref for ref in cited if not _exists(ref))
    assert missing == [], f"section 3 cites paths that do not exist: {missing}"


def _exists(ref: str) -> bool:
    """Report whether a cited path names a file, a directory, or an ADR stem."""
    return (REPO / ref.rstrip("/")).is_dir() or _resolve(ref) is not None


def _feature_row(text: str, label: str) -> str | None:
    section = _section(text, "## 3. Product definition")
    for line in section.splitlines():
        if line.startswith(f"| {label} |"):
            return line
    return None


def _resolve(ref: str) -> Path | None:
    """Return the file a table cell's path names, or None.

    ADR cells are numeric stems (`docs/adr/0005`) rather than full filenames,
    because the slug is long and changes; treat a stem as a prefix match. A
    directory has no text to search, so it resolves to None here and is handled
    by `_exists`.
    """
    path = REPO / ref.rstrip("/")
    if path.is_file():
        return path
    if path.is_dir():
        return None
    if path.parent.is_dir():
        for child in sorted(path.parent.iterdir()):
            if child.name.startswith(path.name) and child.is_file():
                return child
    return None


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt == -1 else text[start:nxt]
