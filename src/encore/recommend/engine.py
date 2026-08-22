"""F7 — the recommendation engine: seeds in, provenance-bound candidates out.

One refresh (weekly by scheduler, on demand via ``encore recommend``):

1. **Seed.** Every watched artist MBID, weighted by F9 listening history
   (`Storage.watched_seed_weights`). An all-zero play history degrades to
   equal weights — unweighted seeding, never silent failure.
2. **Ask ListenBrainz labs** for artists similar to each seed, batched
   fifty MBIDs per request under the shared 1 req/s limiter.
3. **Aggregate.** Each candidate's score sums every seed that referenced
   it: ``seed_weight x similarity``, similarities normalized against this
   refresh's maximum raw score. Candidates already owned are excluded;
   dismissed and promoted decisions survive at the storage layer — a user
   decision outlives recomputation.
4. **Persist the top N** (`RECOMMENDATION_LIMIT`) with per-candidate
   provenance: which seeds produced it and what each contributed.

Skip-don't-queue throughout: a failed batch is counted and the refresh
continues; the next weekly cycle recomputes everything anyway, so there is
no queue to drain and no partial state to reconcile.

Privacy (no-outing lens): names and MBIDs are taste data. Log lines carry
counts only; candidates live in the local database like everything else.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from encore.models import Recommendation
from encore.recommend.lb import ListenBrainzClient, ListenBrainzError, SimilarArtistInfo
from encore.storage import Storage

__all__ = [
    "PROVENANCE_LIMIT",
    "RECOMMENDATION_LIMIT",
    "SEED_BATCH_SIZE",
    "RecommendRefreshReport",
    "refresh_recommendations",
]

logger = logging.getLogger(__name__)

# Candidates persisted per refresh — a rec page stays browsable, and the
# cap is the noise budget (F7 acceptance).
RECOMMENDATION_LIMIT = 50
# Seeds per labs request (the client refuses more).
SEED_BATCH_SIZE = 50
# Contributing sources kept per candidate's provenance.
PROVENANCE_LIMIT = 5


@dataclass
class RecommendRefreshReport:
    """Counts from one refresh — all a log line may say."""

    seeds: int = 0
    batches_failed: int = 0
    rows_received: int = 0
    candidates: int = 0
    stored: int = 0

    @property
    def degraded(self) -> bool:
        """Whether at least one batch failed (the result may be incomplete)."""
        return self.batches_failed > 0


def _batches(items: Sequence[str], size: int) -> Iterator[list[str]]:
    """Yield successive fixed-size batches of seed MBIDs."""
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _provenance_json(algorithm: str, sources: Sequence[tuple[str, float]]) -> str:
    """Serialize one candidate's provenance (capped, sorted, canonical)."""
    top = sorted(sources, key=lambda pair: pair[1], reverse=True)[:PROVENANCE_LIMIT]
    return json.dumps(
        {
            "algorithm": algorithm,
            "sources": [
                {"mbid": mbid, "contribution": round(contribution, 6)} for mbid, contribution in top
            ],
        },
        sort_keys=True,
    )


def refresh_recommendations(
    storage: Storage,
    client: ListenBrainzClient,
    limit: int = RECOMMENDATION_LIMIT,
) -> RecommendRefreshReport:
    """Run one full refresh over the watched library; returns counts."""
    report = RecommendRefreshReport()
    seeds = storage.watched_seed_weights()
    report.seeds = len(seeds)
    if not seeds:
        logger.info("recommend refresh skipped: no watched artists to seed from")
        return report

    # Candidate display fields (first row wins) and per-seed raw rows.
    display: dict[str, SimilarArtistInfo] = {}
    rows_by_reference: dict[str, list[SimilarArtistInfo]] = {}
    max_raw_score = 0.0
    for batch in _batches(sorted(seeds), SEED_BATCH_SIZE):
        try:
            fetched = client.similar_artists(batch)
        except ListenBrainzError:
            # Counts only — no MBIDs (no-outing lens). The next weekly cycle
            # retries everything; there is no backlog queue.
            report.batches_failed += 1
            continue
        report.rows_received += len(fetched)
        for row in fetched:
            if row.mbid in seeds:
                continue  # an owned artist can never be a candidate
            rows_by_reference.setdefault(row.reference_mbid, []).append(row)
            max_raw_score = max(max_raw_score, row.score)
            display.setdefault(row.mbid, row)

    if not rows_by_reference or max_raw_score <= 0:
        logger.info(
            "recommend refresh: seeds=%d rows=%d candidates=0 stored=0 failed_batches=%d",
            report.seeds,
            report.rows_received,
            report.batches_failed,
        )
        return report

    # One O(rows) pass: each raw row contributes weight x normalized
    # similarity to its candidate's total and provenance.
    totals: dict[str, float] = {}
    contributions: dict[str, dict[str, float]] = {}
    for reference_mbid, rows in rows_by_reference.items():
        weight = seeds[reference_mbid]
        for row in rows:
            contribution = weight * (row.score / max_raw_score)
            totals[row.mbid] = totals.get(row.mbid, 0.0) + contribution
            per_seed = contributions.setdefault(row.mbid, {})
            per_seed[reference_mbid] = per_seed.get(reference_mbid, 0.0) + contribution

    candidates = [
        Recommendation(
            mbid=mbid,
            name=display[mbid].name,
            comment=display[mbid].comment,
            score=total,
            provenance_json=_provenance_json(client.algorithm, sorted(contributions[mbid].items())),
            status="new",
        )
        for mbid, total in totals.items()
    ]
    candidates.sort(key=lambda row: row.score, reverse=True)
    report.candidates = len(candidates)
    report.stored = storage.upsert_recommendations(candidates[:limit])
    logger.info(
        "recommend refresh: seeds=%d rows=%d candidates=%d stored=%d failed_batches=%d",
        report.seeds,
        report.rows_received,
        report.candidates,
        report.stored,
        report.batches_failed,
    )
    return report
