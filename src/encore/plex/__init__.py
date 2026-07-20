"""Read-only Plex adapter package (docs/adr/0007).

This package is the only place in the codebase permitted to import
``plexapi`` — enforced by `tests/test_plex_client.py`, not just stated.
"""

from encore.plex.client import (
    PlexArtist,
    PlexLibrary,
    PlexMusicClient,
    PlexWriteAttemptError,
    ReadOnlySession,
)

__all__ = [
    "PlexArtist",
    "PlexLibrary",
    "PlexMusicClient",
    "PlexWriteAttemptError",
    "ReadOnlySession",
]
