"""CLI tests for `encore feeds`: minting, idempotence, rotation, base URL."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from encore import cli
from encore.storage import Storage

_URL_PATTERN = re.compile(r"(https?://\S+)/feeds/([A-Za-z0-9_-]+)/(releases\.xml|upcoming\.ics)")


def _urls(output: str) -> dict[str, tuple[str, str]]:
    """Extract {document: (base, token)} from the command's output."""
    return {document: (base, token) for base, token, document in _URL_PATTERN.findall(output)}


def test_show_mints_the_token_and_prints_both_urls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["feeds", "show", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    urls = _urls(output)
    assert set(urls) == {"releases.xml", "upcoming.ics"}
    base, token = urls["releases.xml"]
    assert base == "http://127.0.0.1:8321"
    assert urls["upcoming.ics"][1] == token
    # The printed token is the stored one.
    storage = Storage(tmp_path)
    assert storage.get_feed_token() == token
    storage.close()
    # The caution always travels with the URLs.
    assert "Anyone with these URLs" in output
    assert "encore feeds rotate" in output


def test_show_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["feeds", "show", "--data-dir", str(tmp_path)])
    first = _urls(capsys.readouterr().out)
    cli.main(["feeds", "show", "--data-dir", str(tmp_path)])
    second = _urls(capsys.readouterr().out)
    assert first == second


def test_rotate_replaces_the_token_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["feeds", "show", "--data-dir", str(tmp_path)])
    old_token = _urls(capsys.readouterr().out)["releases.xml"][1]
    assert cli.main(["feeds", "rotate", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    new_token = _urls(output)["releases.xml"][1]
    assert new_token != old_token
    assert "no longer work" in output
    storage = Storage(tmp_path)
    assert storage.get_feed_token() == new_token
    storage.close()


def test_base_url_flag_shapes_the_urls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A trailing slash must not double up in the printed URL.
    assert (
        cli.main(
            [
                "feeds",
                "show",
                "--data-dir",
                str(tmp_path),
                "--base-url",
                "https://encore.example/",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert _urls(output)["releases.xml"][0] == "https://encore.example"
    assert "example//feeds" not in output


def test_broken_data_dir_fails_loudly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("this path is occupied by a file")
    assert cli.main(["feeds", "show", "--data-dir", str(blocker)]) == 1
    assert "error:" in capsys.readouterr().err
