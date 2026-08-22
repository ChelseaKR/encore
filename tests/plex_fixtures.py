"""Recorded-shape Plex API fixtures for contract tests (risk R3).

`FixtureSession` serves XML in the exact shape the Plex HTTP API returns
(as recorded from plexapi 4.18 traffic against a real server layout),
including container-header pagination — so if a plexapi upgrade changes
which endpoints it requests or how it pages, these tests fail in CI instead
of in a user's install.

It subclasses `ReadOnlySession`, so the transport-level read-only guard
stays active in every test that uses it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import quoteattr

import requests
from requests.models import PreparedRequest, Response

from encore.plex import ReadOnlySession

DEFAULT_BASE_URL = "http://plex.test:32400"


@dataclass(frozen=True)
class FakeArtist:
    """One artist entry to serve from a fixture library."""

    rating_key: str
    name: str
    guid: str | None = "plex://artist/000000000000000000000000"
    # Lifetime plays (F9): rendered as the server's viewCount attribute.
    play_count: int = 0


@dataclass
class FakeLibrary:
    """One library section to serve from the fixture server."""

    key: str
    title: str
    type: str = "artist"
    artists: list[FakeArtist] = field(default_factory=list)


def _root_xml() -> str:
    return (
        '<MediaContainer size="1" friendlyName="encore-test-plex" '
        'machineIdentifier="fixture-machine-id" version="1.41.0.8992" '
        'platform="Linux" myPlex="0"></MediaContainer>'
    )


def _library_xml() -> str:
    return '<MediaContainer size="3" allowSync="0" title1="Plex Library"></MediaContainer>'


def _sections_xml(libraries: list[FakeLibrary]) -> str:
    directories = []
    for library in libraries:
        directories.append(
            f'<Directory allowSync="1" filters="1" refreshing="0" key={quoteattr(library.key)} '
            f"type={quoteattr(library.type)} title={quoteattr(library.title)} "
            f'agent="tv.plex.agents.music" scanner="Plex Music" language="en-US" '
            f'uuid="uuid-{library.key}" updatedAt="1" createdAt="1">'
            f'<Location id="{library.key}" path="/media/{library.key}"/></Directory>'
        )
    return f'<MediaContainer size="{len(libraries)}">' + "".join(directories) + "</MediaContainer>"


def _artists_page_xml(library: FakeLibrary, start: int, size: int) -> str:
    window = library.artists[start : start + size]
    items = []
    for artist in window:
        guid_attr = f" guid={quoteattr(artist.guid)}" if artist.guid else ""
        plays_attr = f' viewCount="{artist.play_count}" ' if artist.play_count else ""
        items.append(
            f"<Directory ratingKey={quoteattr(artist.rating_key)} "
            f'key="/library/metadata/{artist.rating_key}/children"{guid_attr} '
            f'type="artist" title={quoteattr(artist.name)} index="1" '
            f'{plays_attr}addedAt="1" updatedAt="1"></Directory>'
        )
    return (
        f'<MediaContainer size="{len(window)}" totalSize="{len(library.artists)}" '
        f'offset="{start}">' + "".join(items) + "</MediaContainer>"
    )


class FixtureSession(ReadOnlySession):
    """Serves recorded-shape Plex XML; records every request it sees."""

    def __init__(self, libraries: list[FakeLibrary]) -> None:
        super().__init__()
        self.libraries = {library.key: library for library in libraries}
        self.calls: list[tuple[str, str]] = []

    def _body_for(self, path: str, headers: dict[str, str]) -> str | None:
        if path == "/":
            return _root_xml()
        if path == "/library":
            return _library_xml()
        if path == "/library/sections":
            return _sections_xml(list(self.libraries.values()))
        for key, library in self.libraries.items():
            if path == f"/library/sections/{key}":
                return _sections_xml([library])
            if path == f"/library/sections/{key}/all":
                start = int(headers.get("X-Plex-Container-Start", 0))
                size = int(headers.get("X-Plex-Container-Size", 100))
                return _artists_page_xml(library, start, size)
        return None

    def send(self, request: PreparedRequest, **kwargs: Any) -> Response:
        """Answer from fixtures instead of the network."""
        url = str(request.url)
        self.calls.append((str(request.method), url))
        path = (
            "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        )
        path = path.split("?")[0].rstrip("/") or "/"
        body = self._body_for(path, dict(request.headers))
        response = Response()
        response.headers["Content-Type"] = "application/xml"
        if body is None:
            response.status_code = 404
            body = '<MediaContainer size="0"></MediaContainer>'
        else:
            response.status_code = 200
        response._content = body.encode()  # requests offers no public setter for the body
        response.url = url
        response.request = request
        return response


def make_client_session(
    libraries: list[FakeLibrary],
) -> tuple[FixtureSession, str]:
    """Return a fixture session plus the base URL it pretends to serve."""
    return FixtureSession(libraries), DEFAULT_BASE_URL


def make_plain_writable_session() -> requests.Session:
    """Return a vanilla writable session for the constructor-rejection test."""
    return requests.Session()
