"""CLI tests for `encore match` and `encore matches` (F2's CLI surface).

The matching engine itself (scoring, the fixture battery, the review-queue
storage methods) is already exhaustively covered by test_matching_engine.py
and test_match_storage.py — these tests exercise only the CLI's own job:
wiring `list_unmatched_artists` to the engine, skip-don't-queue on a
per-artist MusicBrainz failure, and the review-queue list/resolve/skip
commands. `MusicBrainzClient` is faked (not `httpx_mock`) so these tests
don't pay the real client's 1 req/s rate limiter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from encore import cli
from encore.matching.mb import ArtistCandidate, MusicBrainzError
from encore.models import Artist
from encore.storage import Storage


class FakeMBClient:
    """A `MusicBrainzClient` stand-in: canned results or an error, per name."""

    def __init__(self, results: dict[str, list[ArtistCandidate] | Exception]) -> None:
        self._results = results
        self.calls: list[str] = []
        self.closed = False

    def search_artists(self, name: str, limit: int = 8) -> list[ArtistCandidate]:
        self.calls.append(name)
        outcome = self._results.get(name, [])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def _add_artist(storage: Storage, key: str, name: str) -> None:
    """Insert one Artist row directly (bypassing the Plex sync pipeline)."""
    with storage.session() as session:
        session.add(Artist(plex_rating_key=key, name=name, library_key="1"))
        session.commit()


def test_match_with_no_synced_artists_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_client = FakeMBClient({})
    monkeypatch.setattr(cli, "MusicBrainzClient", lambda: fake_client)

    assert cli.main(["match", "--data-dir", str(tmp_path)]) == 0

    assert "No unmatched artists" in capsys.readouterr().out
    assert fake_client.calls == []
    assert fake_client.closed


def test_match_reports_auto_pending_and_failed_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    _add_artist(storage, "key-radiohead", "Radiohead")
    _add_artist(storage, "key-bush", "Bush")
    _add_artist(storage, "key-broken", "Broken Lookup")
    storage.close()

    fake_client = FakeMBClient(
        {
            "Radiohead": [ArtistCandidate(mbid="mb-radiohead", name="Radiohead", mb_score=100)],
            "Bush": [
                ArtistCandidate(
                    mbid="mb-bush-gb", name="Bush", mb_score=100, disambiguation="British band"
                ),
                ArtistCandidate(
                    mbid="mb-bush-ca", name="Bush", mb_score=92, disambiguation="Canadian band"
                ),
            ],
            "Broken Lookup": MusicBrainzError("MusicBrainz request failed with HTTP 400"),
        }
    )
    monkeypatch.setattr(cli, "MusicBrainzClient", lambda: fake_client)

    assert cli.main(["match", "--data-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "match complete: candidates=3 auto=1 pending=1 failed=1" in output
    assert "1 artist(s) need a decision" in output
    assert sorted(fake_client.calls) == ["Broken Lookup", "Bush", "Radiohead"]
    assert fake_client.closed

    storage = Storage(tmp_path)
    assert storage.get_artist_match("key-radiohead").status == "auto"  # type: ignore[union-attr]
    assert storage.get_artist_match("key-bush").status == "pending"  # type: ignore[union-attr]
    assert storage.get_artist_match("key-broken") is None  # a failure never persists a row
    storage.close()


def test_matches_list_when_queue_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["matches", "list", "--data-dir", str(tmp_path)]) == 0
    assert "Review queue is empty" in capsys.readouterr().out


def test_matches_list_shows_candidates_and_next_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    storage.save_artist_match(
        "key-bush",
        "Bush",
        "pending",
        candidates_json=(
            '[{"mbid": "mb-bush-gb", "name": "Bush", "score": 1.0, "country": "GB", '
            '"disambiguation": "British band"}, '
            '{"mbid": "mb-bush-ca", "name": "Bush", "score": 0.92, "country": "CA", '
            '"disambiguation": "Canadian band"}]'
        ),
    )
    storage.close()

    assert cli.main(["matches", "list", "--data-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "Bush  (artist_key=key-bush)" in output
    assert "mb-bush-gb" in output
    assert "British band" in output
    assert "mb-bush-ca" in output
    assert "encore matches resolve --artist-key key-bush --mbid <mbid>" in output
    assert "encore matches skip --artist-key key-bush" in output


def test_matches_resolve_updates_the_row_and_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    storage.save_artist_match("key-bush", "Bush", "pending")
    storage.close()

    exit_code = cli.main(
        [
            "matches",
            "resolve",
            "--data-dir",
            str(tmp_path),
            "--artist-key",
            "key-bush",
            "--mbid",
            "mb-bush-gb",
        ]
    )

    assert exit_code == 0
    assert "Resolved 'key-bush' to mb-bush-gb" in capsys.readouterr().out
    storage = Storage(tmp_path)
    resolved = storage.get_artist_match("key-bush")
    assert resolved is not None
    assert (resolved.status, resolved.mbid) == ("manual", "mb-bush-gb")
    storage.close()


def test_matches_resolve_unknown_artist_key_reports_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(
        [
            "matches",
            "resolve",
            "--data-dir",
            str(tmp_path),
            "--artist-key",
            "no-such-key",
            "--mbid",
            "mb-x",
        ]
    )

    assert exit_code == 1
    assert "no artist match row" in capsys.readouterr().err


def test_matches_skip_updates_the_row_and_confirms(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    storage.save_artist_match("key-bush", "Bush", "pending")
    storage.close()

    exit_code = cli.main(
        ["matches", "skip", "--data-dir", str(tmp_path), "--artist-key", "key-bush"]
    )

    assert exit_code == 0
    assert "Skipped 'key-bush'" in capsys.readouterr().out
    storage = Storage(tmp_path)
    skipped = storage.get_artist_match("key-bush")
    assert skipped is not None
    assert skipped.status == "skipped"
    storage.close()


def test_matches_skip_unknown_artist_key_reports_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(
        ["matches", "skip", "--data-dir", str(tmp_path), "--artist-key", "no-such-key"]
    )

    assert exit_code == 1
    assert "no artist match row" in capsys.readouterr().err


def test_match_with_only_auto_matches_omits_the_review_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    _add_artist(storage, "key-radiohead", "Radiohead")
    storage.close()
    fake_client = FakeMBClient(
        {"Radiohead": [ArtistCandidate(mbid="mb-radiohead", name="Radiohead", mb_score=100)]}
    )
    monkeypatch.setattr(cli, "MusicBrainzClient", lambda: fake_client)

    assert cli.main(["match", "--data-dir", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "auto=1 pending=0 failed=0" in output
    assert "need a decision" not in output


def test_matches_commands_report_an_unusable_data_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_directory = tmp_path / "regular-file"
    not_a_directory.write_text("occupied", encoding="utf-8")

    exit_codes = [
        cli.main(["match", "--data-dir", str(not_a_directory)]),
        cli.main(["matches", "list", "--data-dir", str(not_a_directory)]),
        cli.main(
            [
                "matches",
                "resolve",
                "--data-dir",
                str(not_a_directory),
                "--artist-key",
                "k",
                "--mbid",
                "m",
            ]
        ),
        cli.main(["matches", "skip", "--data-dir", str(not_a_directory), "--artist-key", "k"]),
    ]

    assert exit_codes == [1, 1, 1, 1]
    assert capsys.readouterr().err.count("cannot create data directory") == 4
