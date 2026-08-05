"""Contract tests for the read-only Plex client (F1, risk R3).

These run against recorded-shape fixtures (`tests/plex_fixtures.py`), so a
plexapi upgrade that changes endpoints, pagination, or attribute names fails
here instead of in a user's install.
"""

from __future__ import annotations

from typing import cast

import pytest

from encore.plex import (
    PlexArtist,
    PlexMusicClient,
    PlexWriteAttemptError,
    ReadOnlySession,
)
from tests.plex_fixtures import (
    FakeArtist,
    FakeLibrary,
    make_client_session,
    make_plain_writable_session,
)

pytestmark = pytest.mark.read_only_plex


def _music_and_movies() -> list[FakeLibrary]:
    return [
        FakeLibrary(
            key="1",
            title="Music",
            artists=[
                FakeArtist(rating_key="101", name="Boards of Canada"),
                FakeArtist(rating_key="102", name="Kate Bush", guid=None),
            ],
        ),
        FakeLibrary(key="2", title="Movies", type="movie"),
        FakeLibrary(key="3", title="Vinyl Rips", artists=[FakeArtist("301", "Autechre")]),
    ]


def test_music_libraries_filters_to_artist_sections() -> None:
    session, base_url = make_client_session(_music_and_movies())
    client = PlexMusicClient(base_url, "fixture-token", session=session)
    libraries = client.music_libraries()
    assert [(lib.key, lib.title) for lib in libraries] == [("1", "Music"), ("3", "Vinyl Rips")]


def test_artists_parses_fixture_attributes() -> None:
    session, base_url = make_client_session(_music_and_movies())
    client = PlexMusicClient(base_url, "fixture-token", session=session)
    artists = client.artists("1")
    assert artists == [
        PlexArtist(
            rating_key="101",
            name="Boards of Canada",
            guid="plex://artist/000000000000000000000000",
            library_key="1",
        ),
        PlexArtist(rating_key="102", name="Kate Bush", guid=None, library_key="1"),
    ]
    assert not any("/library/metadata/102" in url for _, url in session.calls)


def test_artists_rejects_non_music_library() -> None:
    session, base_url = make_client_session(_music_and_movies())
    client = PlexMusicClient(base_url, "fixture-token", session=session)
    with pytest.raises(ValueError, match="not a music library"):
        client.artists("2")


def test_thousand_artist_library_inventoried_in_one_call() -> None:
    # Roadmap F1 acceptance: a 1,000-artist library is inventoried in one run.
    # plexapi pages via X-Plex-Container-* headers under the hood.
    big = FakeLibrary(
        key="1",
        title="Music",
        artists=[FakeArtist(rating_key=str(1000 + i), name=f"Artist {i:04d}") for i in range(1000)],
    )
    session, base_url = make_client_session([big])
    client = PlexMusicClient(base_url, "fixture-token", session=session)
    artists = client.artists("1")
    assert len(artists) == 1000
    assert len({a.rating_key for a in artists}) == 1000
    page_calls = [url for _, url in session.calls if "/library/sections/1/all" in url]
    assert len(page_calls) == 10  # 100-item containers — the recorded plexapi behavior


def test_repr_and_base_url_never_expose_the_token() -> None:
    session, base_url = make_client_session(_music_and_movies())
    client = PlexMusicClient(base_url, "fixture-token-needle-77", session=session)
    assert "fixture-token-needle-77" not in repr(client)
    assert client.base_url == base_url


def test_transport_blocks_mutating_methods_before_network_io() -> None:
    session = ReadOnlySession()
    with pytest.raises(PlexWriteAttemptError, match="blocked a POST request"):
        session.post("http://plex.invalid/library/metadata/1")


def test_client_rejects_a_writable_session() -> None:
    with pytest.raises(TypeError, match="only accepts a ReadOnlySession"):
        PlexMusicClient(
            "http://plex.invalid:32400",
            "fixture-token",
            session=cast(ReadOnlySession, make_plain_writable_session()),
        )


def test_client_facade_exposes_no_mutating_operation() -> None:
    public_names = {name for name in dir(PlexMusicClient) if not name.startswith("_")}
    # machine_identifier joined the facade with F4 (Plex deep links in
    # notifications). It is a read of a value plexapi already holds — the
    # facade still exposes no verb that could write to Plex.
    assert public_names == {"artists", "base_url", "machine_identifier", "music_libraries"}
