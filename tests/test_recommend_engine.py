"""Recommendation engine tests (F7): weighting, aggregation, sticky decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from encore.matching.mb import RateLimiter
from encore.recommend.engine import refresh_recommendations
from encore.recommend.lb import ListenBrainzClient, SimilarArtistInfo, _parse_row
from encore.storage import Storage, StorageError

SEED_A = "aaaa1111-0000-0000-0000-000000000001"
SEED_B = "bbbb2222-0000-0000-0000-000000000002"
CAND_X = "cccc3333-0000-0000-0000-00000000000x"
CAND_Y = "dddd4444-0000-0000-0000-00000000000y"
CAND_Z = "eeee5555-0000-0000-0000-00000000000z"


def _labs_row(mbid: str, name: str, score: int, reference: str) -> dict[str, object]:
    return {
        "artist_mbid": mbid,
        "name": name,
        "comment": None,
        "type": "Group",
        "score": score,
        "reference_mbid": reference,
    }


class FakeLBClient:
    """A labs client that answers from a scripted table of rows per seed.

    Rows go through the client's own parser, so the fake exercises the same
    contract the real HTTP surface does.
    """

    def __init__(self, rows_by_seed: dict[str, list[dict[str, object]]]) -> None:
        self.rows_by_seed = rows_by_seed
        self.calls: list[list[str]] = []
        self.algorithm = "test-algorithm"

    def close(self) -> None:
        pass

    def similar_artists(self, artist_mbids: list[str]) -> list[SimilarArtistInfo]:
        self.calls.append(list(artist_mbids))
        rows: list[SimilarArtistInfo] = []
        for mbid in artist_mbids:
            for raw in self.rows_by_seed.get(mbid, []):
                parsed = _parse_row(raw)
                if parsed is not None:
                    rows.append(parsed)
        return rows


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "data")
    _seed_artist(storage, "key-a", "Low A", SEED_A, play_count=100)
    _seed_artist(storage, "key-b", "Low B", SEED_B, play_count=50)
    return storage


def _seed_artist(
    storage: Storage,
    rating_key: str,
    name: str,
    mbid: str,
    play_count: int = 0,
) -> None:
    from encore.models import Artist

    with storage.session() as session:
        session.add(
            Artist(
                plex_rating_key=rating_key,
                name=name,
                library_key="1",
                play_count=play_count,
            )
        )
        session.commit()
    storage.save_artist_match(rating_key, name, "auto", mbid=mbid)


def test_scores_aggregate_across_seeds_weighted_by_listening(storage: Storage) -> None:
    # X is similar to both seeds; Z only to the heavier one. Weights are
    # 1.0 (A) and 0.5 (B) after F9 normalization.
    client = FakeLBClient(
        {
            SEED_A: [_labs_row(CAND_X, "Candidate X", 1000, SEED_A)],
            SEED_B: [
                _labs_row(CAND_X, "Candidate X", 800, SEED_B),
                _labs_row(CAND_Z, "Candidate Z", 500, SEED_B),
            ],
        }
    )

    report = refresh_recommendations(storage, client)  # type: ignore[arg-type]

    assert report.seeds == 2
    assert report.stored == 2
    recs = {row.mbid: row for row in storage.list_recommendations()}
    assert set(recs) == {CAND_X, CAND_Z}
    # X aggregates two contributions: 1.0x(1000/1000) + 0.5x(800/1000) = 1.4;
    # Z gets only the lighter seed's 0.5x(500/1000) = 0.25 — so X ranks first.
    assert recs[CAND_X].score > recs[CAND_Z].score
    provenance = json.loads(recs[CAND_X].provenance_json or "{}")
    assert {source["mbid"] for source in provenance["sources"]} == {SEED_A, SEED_B}
    assert provenance["algorithm"] == "test-algorithm"


def test_an_empty_play_history_degrades_to_equal_weights(storage: Storage) -> None:
    from sqlmodel import select

    from encore.models import Artist

    # Zero out every play count: seeding must go unweighted (all 1.0), so
    # Z's single light-seed reference scores the same as a heavy one would.
    with storage.session() as session:
        for row in session.exec(select(Artist)).all():
            row.play_count = 0
            session.add(row)
        session.commit()

    weights = storage.watched_seed_weights()
    assert weights == {SEED_A: 1.0, SEED_B: 1.0}

    client = FakeLBClient({SEED_B: [_labs_row(CAND_Z, "Z", 999, SEED_B)]})
    refresh_recommendations(storage, client)  # type: ignore[arg-type]
    recs = storage.list_recommendations()
    assert len(recs) == 1
    assert recs[0].mbid == CAND_Z


def test_owned_artists_and_sticky_decisions_are_excluded(storage: Storage) -> None:
    client = FakeLBClient(
        {
            SEED_A: [
                _labs_row(SEED_B, "Owned Seed B", 900, SEED_A),  # owned → excluded
                _labs_row(CAND_X, "Candidate X", 800, SEED_A),
            ]
        }
    )
    refresh_recommendations(storage, client)  # type: ignore[arg-type]
    storage.set_recommendation_status(CAND_X, "dismissed")

    # A second refresh re-serves X: it must stay dismissed, not resurrect.
    refresh_recommendations(storage, client)  # type: ignore[arg-type]
    statuses = {row.mbid: row.status for row in storage.list_recommendations()}
    assert statuses.get(CAND_X) is None  # not among the "new" candidates


def test_refresh_updates_new_rows_in_place(storage: Storage) -> None:
    first = FakeLBClient({SEED_A: [_labs_row(CAND_X, "Candidate X", 1000, SEED_A)]})
    refresh_recommendations(storage, first)  # type: ignore[arg-type]
    created_at = storage.list_recommendations()[0].created_at

    # A weaker second refresh lowers the score in place rather than duplicating.
    second = FakeLBClient({SEED_A: [_labs_row(CAND_X, "Candidate X", 400, SEED_A)]})
    refresh_recommendations(storage, second)  # type: ignore[arg-type]
    recs = storage.list_recommendations()
    assert len(recs) == 1 and recs[0].mbid == CAND_X
    assert recs[0].updated_at >= created_at


def test_the_candidate_cap_limits_persistence(storage: Storage) -> None:
    rows = [_labs_row(f"ffff{i:04d}-0000", f"Cand {i}", 100 - i, SEED_A) for i in range(10)]
    client = FakeLBClient({SEED_A: rows})

    report = refresh_recommendations(storage, client, limit=3)  # type: ignore[arg-type]

    assert report.candidates == 10
    assert report.stored == 3
    assert len(storage.list_recommendations()) == 3


def test_a_failed_batch_degrades_without_raising(storage: Storage) -> None:
    from encore.recommend.lb import ListenBrainzError

    class FlakyClient(FakeLBClient):
        def __init__(self) -> None:
            super().__init__({})

        def similar_artists(self, artist_mbids: list[str]) -> list[SimilarArtistInfo]:
            raise ListenBrainzError("labs unreachable")

    flaky = FlakyClient()
    report = refresh_recommendations(storage, flaky)  # type: ignore[arg-type]
    assert report.batches_failed == 1
    assert report.stored == 0
    storage.close()


def test_no_watched_artists_is_a_free_no_op(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "empty")
    client = ListenBrainzClient(rate_limiter=RateLimiter(min_interval=0))

    report = refresh_recommendations(storage, client)

    assert report.seeds == 0 and report.stored == 0
    storage.close()


def test_recommendation_status_validation(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "status")
    with pytest.raises(StorageError):
        storage.set_recommendation_status("nope", "dismissed")
    storage.close()
