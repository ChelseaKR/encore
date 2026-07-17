"""MusicBrainz identity matching + review queue (F2, M1 — docs/adr/0003).

The correctness keystone: every Plex artist is matched to a MusicBrainz
artist MBID with an explicit confidence score. At or above the (provisional,
see `encore.matching.scoring`) auto-match threshold the match is recorded
automatically; below it the artist enters a review queue instead of guessing.
Decisions are cached permanently and are always manually overridable.
"""

from encore.matching.engine import MatchEngine
from encore.matching.mb import (
    MB_RATE_LIMITER,
    ArtistCandidate,
    MusicBrainzClient,
    MusicBrainzError,
    RateLimiter,
)
from encore.matching.scoring import (
    AUTO_MATCH_THRESHOLD,
    ArtistHints,
    MatchDecision,
    decide,
    score_candidate,
)

__all__ = [
    "AUTO_MATCH_THRESHOLD",
    "MB_RATE_LIMITER",
    "ArtistCandidate",
    "ArtistHints",
    "MatchDecision",
    "MatchEngine",
    "MusicBrainzClient",
    "MusicBrainzError",
    "RateLimiter",
    "decide",
    "score_candidate",
]
