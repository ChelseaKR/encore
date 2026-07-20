"""The read-only Plex client wrapper (docs/adr/0007).

Two mechanical guarantees, both tested in `tests/test_plex_client.py`:

1. **Transport-level read-only.** Every HTTP request plexapi makes funnels
   through `ReadOnlySession.request`, which raises on any method other than
   GET/HEAD/OPTIONS. A bug (ours or plexapi's) that tries to write to Plex
   fails loudly before a byte leaves the process — the guarantee does not
   depend on this facade staying narrow.
2. **Facade-level narrowness.** `PlexMusicClient` exposes only the read
   operations F1 needs (list music libraries, list artists). It is the only
   module allowed to import ``plexapi``.

The Plex token is held by the underlying plexapi object; it never appears in
this module's ``repr`` output or log lines (OBS-11 — see
`tests/test_scheduler.py`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from plexapi.server import PlexServer

__all__ = [
    "READ_METHODS",
    "PlexArtist",
    "PlexLibrary",
    "PlexMusicClient",
    "PlexWriteAttemptError",
    "ReadOnlySession",
]

logger = logging.getLogger(__name__)

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Plex's numeric type for artist items in a music library, and the section
# type string plexapi reports for music libraries.
_MUSIC_SECTION_TYPE = "artist"


class PlexWriteAttemptError(RuntimeError):
    """A code path attempted a mutating HTTP request against the Plex server.

    Encore never has a legitimate reason to write to Plex (docs/adr/0007;
    README non-goals) — this firing means a bug, and failing loudly here is
    the point of the transport guard.
    """


class ReadOnlySession(requests.Session):
    """A ``requests.Session`` that refuses every non-read HTTP method.

    All of requests' verb helpers (``get``/``post``/``put``/...) funnel
    through `request`, so overriding it guards the whole session, including
    any call plexapi makes internally.
    """

    def request(
        self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any
    ) -> requests.Response:
        """Send the request iff the method is GET/HEAD/OPTIONS; raise otherwise."""
        method_name = method.decode() if isinstance(method, bytes) else str(method)
        if method_name.upper() not in READ_METHODS:
            raise PlexWriteAttemptError(
                f"blocked a {method_name.upper()} request to the Plex server: encore's "
                "Plex access is read-only by design (docs/adr/0007). This is a bug — "
                "please report it."
            )
        return super().request(method, url, *args, **kwargs)


@dataclass(frozen=True)
class PlexLibrary:
    """A music library section on the Plex server."""

    key: str
    title: str


@dataclass(frozen=True)
class PlexArtist:
    """One artist entry as inventoried from a Plex music library."""

    rating_key: str
    name: str
    guid: str | None
    library_key: str


class PlexMusicClient:
    """Read-only facade over python-plexapi, scoped to music-library reads.

    The public surface deliberately contains no mutating verb — asserted by
    a test, not just convention (`tests/test_plex_client.py`).
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        session: ReadOnlySession | None = None,
    ) -> None:
        """Connect to the Plex server at ``base_url`` with ``token``.

        ``session`` exists so contract tests can substitute a fixture-serving
        transport; it must still be a `ReadOnlySession` — there is no
        constructor path that yields a writable session.

        Raises:
            TypeError: ``session`` is not a `ReadOnlySession`.
        """
        if session is None:
            session = ReadOnlySession()
        if not isinstance(session, ReadOnlySession):
            raise TypeError(
                "PlexMusicClient only accepts a ReadOnlySession — the read-only "
                "guarantee (docs/adr/0007) is transport-level, not optional."
            )
        self._base_url = base_url
        # plexapi does not publish a typed constructor. Keep that untyped edge
        # explicit here; the facade converts every value into Encore's typed
        # dataclasses before it can reach the rest of the application.
        server_factory: Any = PlexServer
        self._server = server_factory(base_url, token=token, session=session)

    def __repr__(self) -> str:
        """Identify the client by server URL only — never the token (OBS-11)."""
        return f"PlexMusicClient(base_url={self._base_url!r})"

    @property
    def base_url(self) -> str:
        """The Plex server base URL this client talks to."""
        return self._base_url

    def music_libraries(self) -> list[PlexLibrary]:
        """Return the server's music (artist-typed) library sections."""
        sections = self._server.library.sections()
        return [
            PlexLibrary(key=str(section.key), title=str(section.title))
            for section in sections
            if str(section.type) == _MUSIC_SECTION_TYPE
        ]

    def artists(self, library_key: str) -> list[PlexArtist]:
        """Inventory every artist in the music library ``library_key``.

        plexapi pages through the section under the hood (100 items per
        container request), so a 1,000-artist library is one call here and
        ~10 HTTP GETs on the wire.

        Raises:
            ValueError: ``library_key`` is not a music library on this server.
        """
        section = self._server.library.sectionByID(int(library_key))
        if str(section.type) != _MUSIC_SECTION_TYPE:
            raise ValueError(
                f"Plex library {library_key} ({section.title!r}) is a "
                f"{section.type!r} library, not a music library"
            )
        items = section.all(libtype=_MUSIC_SECTION_TYPE)
        logger.debug("plex: library %s returned %d artist entries", library_key, len(items))
        artists: list[PlexArtist] = []
        for item in items:
            # plexapi auto-reloads a partially populated object when a normal
            # attribute lookup returns None. A missing GUID is valid, so read
            # the already parsed value directly and avoid an unnecessary
            # metadata request (or an offline-sync failure).
            guid = vars(item).get("guid")
            artists.append(
                PlexArtist(
                    rating_key=str(item.ratingKey),
                    name=str(item.title),
                    guid=str(guid) if guid else None,
                    library_key=library_key,
                )
            )
        return artists
