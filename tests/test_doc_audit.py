"""The committed doc audit still describes this tree, and the tool that says so works.

``docs/DOCUMENTATION-AUDIT.md`` had no generator and no check. Its verdict rows stayed
``pass`` while its counts stopped describing the repository: "3 test files" against 33,
"architecture and interfaces | 12" against 15 ADR files, "safety, privacy … | 3" against
5. A document whose purpose is to show that this project's process claims are real was
itself reporting success about records it no longer inspected.

``make docs-audit-check`` closes that, and this module makes the closure hold from the
test suite as well, so the gate survives a Makefile edit. It also tests the gate itself:
a gate nobody tests is a gate that silently stops gating.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.doc_audit import (
    AUDIT,
    BEGIN,
    END,
    ROOT,
    authored_docs,
    check_links,
    main,
    render,
    splice,
)

# A name no repository file will ever have, so a leaked temp file is obvious.
_STRAY = "doc-audit-stray-file-under-test"


def test_committed_block_matches_the_tree() -> None:
    """The whole point: the generated block equals what the tree produces today."""
    document = AUDIT.read_text(encoding="utf-8")
    assert splice(document, render()) == document, (
        "docs/DOCUMENTATION-AUDIT.md's generated block has drifted from the tree. "
        "Run `make docs-audit` and commit the result."
    )


def test_check_mode_agrees_and_writes_nothing() -> None:
    """``--check`` must pass here, and must not repair the document it is judging."""
    before = AUDIT.read_bytes()
    assert main(["--check"]) == 0
    assert AUDIT.read_bytes() == before


def test_check_mode_fails_on_a_drifted_block(tmp_path: Path) -> None:
    """A single altered character inside the markers must go red."""
    document = AUDIT.read_text(encoding="utf-8")
    damaged = document.replace("| Test modules |", "| Test modules (stale) |", 1)
    assert damaged != document, "the fixture no longer matches the generated block"
    assert splice(damaged, render()) != damaged


def test_missing_markers_are_a_loud_failure() -> None:
    """Deleting the markers must not be a quiet way to stop auditing."""
    try:
        splice("# no markers here\n", render())
    except SystemExit as exc:
        assert "missing the generated-block markers" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("splice() accepted a document with no markers")


def test_no_gitignored_markdown_reaches_the_inventory() -> None:
    """The inventory must describe the repository, not one contributor's checkout.

    This is the exclusion a port of this tool is most likely to get wrong, and the
    symptom only appears on the machine that ran the tooling: the audit starts
    describing that checkout, the drift gate fails on an unmodified tree, and the fix it
    asks for writes an ignored local path into a public, committed document. Asking git
    which files it ignores is the check that cannot rot as `.gitignore` changes.
    """
    docs = authored_docs()
    if not docs:  # pragma: no cover - a repo with no docs would fail elsewhere first
        return
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local paths
        ["git", "-C", str(ROOT), "check-ignore", "--stdin"],  # noqa: S607
        input="\n".join(docs),
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, f"gitignored paths reached the doc inventory: {ignored}"


def test_every_authored_doc_is_tracked_by_git() -> None:
    """The other half: a file git does not know about cannot be repository content."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local paths
        ["git", "-C", str(ROOT), "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = set(result.stdout.splitlines())
    untracked = [rel for rel in authored_docs() if rel not in tracked]
    assert not untracked, f"untracked paths reached the doc inventory: {untracked}"


def test_a_stray_untracked_markdown_file_cannot_move_the_counts() -> None:
    """The generated block must describe the commit, not the checkout.

    The test above states the invariant but only exercises whatever happens to
    be on disk, so it passed for as long as nobody kept a local note. The
    collectors walked the filesystem, so an untracked `docs/` scratch file
    joined the inventory and moved the "Hand-authored docs" and "other docs"
    counts on one machine and not on CI -- `--check` disagreeing with itself
    across two checkouts of the same commit, which is drift produced by the
    anti-drift gate. Plant one and prove it changes nothing.
    """
    before = render()
    stray = ROOT / "docs" / f"{_STRAY}.md"
    assert not stray.exists(), "a previous run leaked its temp file"
    stray.write_text("# stray\n", encoding="utf-8")
    try:
        assert f"docs/{_STRAY}.md" not in authored_docs()
        assert render() == before
    finally:
        stray.unlink()


def test_link_check_actually_checks_links() -> None:
    """A link check reporting "0 unresolved" over 0 links is a check that cannot fail."""
    checked, unresolved = check_links(authored_docs())
    assert checked > 20, f"only {checked} relative links swept; the sweep has gone vacuous"
    assert unresolved == []


def test_link_check_is_case_sensitive() -> None:
    """Case matters: macOS folds it, github.com does not, and readers get the 404.

    The audit's own Link Check section records a `roadmap.md` link that passed a
    case-insensitive sweep and 404'd on GitHub. Assert the mis-cased spelling is
    rejected even on a filesystem that would happily open it.
    """
    from scripts.doc_audit import _exists_case_sensitively

    assert _exists_case_sensitively(ROOT / "docs" / "ROADMAP.md")
    assert not _exists_case_sensitively(ROOT / "docs" / "roadmap.md")


def test_markers_name_the_tool_that_writes_the_block() -> None:
    """A reader who edits by hand should be told what to run instead."""
    document = AUDIT.read_text(encoding="utf-8")
    assert BEGIN in document
    assert END in document
    assert "scripts/doc_audit.py" in BEGIN
