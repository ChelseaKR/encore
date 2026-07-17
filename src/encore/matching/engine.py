"""The matching engine: cache-first orchestration of search → score → persist (F2).

Flow per artist: an existing `encore.models.ArtistMatch` row of *any* status
short-circuits (the permanent cache — one MusicBrainz query per artist,
ever, unless a re-match is explicitly forced). On a miss, the engine searches
MusicBrainz, scores candidates (`encore.matching.scoring`), and persists
either an auto-match or a pending review-queue entry with the ranked
candidates serialized for display.

Privacy (no-outing lens): artist names and MBIDs are taste data — engine log
lines carry only opaque keys, statuses, and counts, proven by a
``no_outing``-marked test.
"""

from __future__ import annotations

import json
import logging

from encore.matching.mb import ArtistCandidate, MusicBrainzClient
from encore.matching.scoring import (
    AMBIGUITY_MARGIN,
    AUTO_MATCH_THRESHOLD,
    ArtistHints,
    MatchDecision,
    decide,
)
from encore.models import ArtistMatch
from encore.storage import Storage

__all__ = ["MatchEngine", "candidates_from_json"]

logger = logging.getLogger(__name__)


def _candidates_to_json(decision: MatchDecision) -> str:
    """Serialize the ranked candidates for the review queue to display."""
    return json.dumps(
        [
            {
                "mbid": candidate.mbid,
                "name": candidate.name,
                "score": round(score, 4),
                "type": candidate.artist_type,
                "country": candidate.country,
                "disambiguation": candidate.disambiguation,
            }
            for candidate, score in decision.ranked
        ]
    )


def candidates_from_json(candidates_json: str | None) -> list[dict[str, object]]:
    """Deserialize a row's stored candidate list (empty when none recorded)."""
    if candidates_json is None:
        return []
    raw: object = json.loads(candidates_json)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


class MatchEngine:
    """Matches artists to MusicBrainz identities and manages the review queue."""

    def __init__(
        self,
        storage: Storage,
        client: MusicBrainzClient,
        auto_threshold: float = AUTO_MATCH_THRESHOLD,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
    ) -> None:
        """Wire the engine to a storage layer and a MusicBrainz client."""
        self._storage = storage
        self._client = client
        self._auto_threshold = auto_threshold
        self._ambiguity_margin = ambiguity_margin

    def match_artist(
        self,
        artist_key: str,
        hints: ArtistHints,
        force: bool = False,
    ) -> ArtistMatch:
        """Return the (cached or freshly decided) match for one artist.

        Any existing row is returned untouched unless ``force`` is true —
        ``force`` re-queries MusicBrainz and re-decides, which is the
        "re-run the match" half of manual re-matching (the "pin a specific
        MBID" half is `resolve`).
        """
        if not force:
            cached = self._storage.get_artist_match(artist_key)
            if cached is not None:
                logger.debug(
                    "match cache hit for artist_key=%s (status=%s)", artist_key, cached.status
                )
                return cached
        candidates = self._client.search_artists(hints.name)
        decision = decide(
            hints,
            candidates,
            auto_threshold=self._auto_threshold,
            ambiguity_margin=self._ambiguity_margin,
        )
        row = self._persist_decision(artist_key, hints.name, decision)
        logger.info(
            "match decided for artist_key=%s: status=%s from %d candidate(s)",
            artist_key,
            row.status,
            len(candidates),
        )
        return row

    def _persist_decision(
        self, artist_key: str, artist_name: str, decision: MatchDecision
    ) -> ArtistMatch:
        """Write an auto or pending decision through the storage layer."""
        chosen: ArtistCandidate | None = decision.chosen
        return self._storage.save_artist_match(
            artist_key=artist_key,
            artist_name=artist_name,
            status=decision.status,
            mbid=chosen.mbid if chosen is not None else None,
            confidence=decision.confidence,
            candidates_json=_candidates_to_json(decision),
        )

    def review_queue(self) -> list[ArtistMatch]:
        """Artists awaiting a human decision, oldest first."""
        return self._storage.list_review_queue()

    def resolve(self, artist_key: str, mbid: str) -> ArtistMatch:
        """Manually match to a specific MBID (review resolution or re-match)."""
        row = self._storage.resolve_artist_match(artist_key, mbid)
        logger.info("match resolved manually for artist_key=%s", artist_key)
        return row

    def skip(self, artist_key: str) -> ArtistMatch:
        """Deliberately leave an artist unmatched (no re-query on later syncs)."""
        row = self._storage.skip_artist_match(artist_key)
        logger.info("match skipped for artist_key=%s", artist_key)
        return row
