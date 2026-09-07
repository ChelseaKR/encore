"""`encore settings simulate` — replaying history through a proposed policy (#58).

The anchor is `test_simulating_the_policy_in_force_reproduces_recorded_deliveries`:
the fixture is built through the *real* path — `watch_artist` against a stubbed
MusicBrainz, then `Storage.ensure_deliveries` — and the replay of today's policy
has to land on the delivery rows that path actually created. A simulator that
agrees with itself and not with delivery is a reimplementation, not a model.

The rest of the file is about the reason this reads `release_groups` and not
`events`. **Type filters gate event creation, not delivery**: a group the policy
in force filtered raises no event at all, so a replay of the event log would
report that widening the policy costs nothing — an absence rendered as a
measurement, and the exact question the command exists to answer.
`test_widening_to_singles_finds_the_singles_that_never_became_events` is that
case; it fails outright against an event-log implementation.

Two gaps are asserted to stay visible rather than being smoothed away: an
artist baselined inside the window (silent under every policy, ADR-0011) and
days on which nothing was recorded at all.
"""

from __future__ import annotations

import datetime as dt
import socket
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock
from sqlmodel import select

from encore.artistsettings import SettingsOverride
from encore.channelroute import ChannelRoute
from encore.cli import main
from encore.matching.mb import MB_BASE_URL, MusicBrainzClient, RateLimiter
from encore.models import Artist, Delivery, ReleaseEvent, ReleaseGroup
from encore.simulate import (
    OUTCOME_BASELINE,
    OUTCOME_DELIVERED,
    OUTCOME_FILTERED,
    OUTCOME_MUTED,
    OUTCOME_ROUTED_AWAY,
    diff_reports,
    format_report,
    run_simulation,
)
from encore.storage import Storage
from encore.watch import watch_artist
from tests.mb_fixtures import mb_browse_response, mb_release_group

SENTINEL_URL = "ntfy://example.invalid/topic"
ARTIST = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _client() -> MusicBrainzClient:
    return MusicBrainzClient(rate_limiter=RateLimiter(min_interval=0), sleep=lambda _s: None)


def _browse_url(artist_mbid: str) -> str:
    return f"{MB_BASE_URL}/release-group?artist={artist_mbid}&fmt=json&limit=100&offset=0"


def _watch(storage: Storage, key: str, name: str, mbid: str) -> None:
    with storage.session() as session:
        session.add(Artist(plex_rating_key=key, name=name, library_key="1"))
        session.commit()
    storage.save_artist_match(key, name, "auto", mbid=mbid, confidence=0.99)


def _window(_storage: Storage) -> tuple[dt.datetime, dt.datetime]:
    """Return a window wide enough for what a test just wrote, but not the baseline."""
    now = dt.datetime.now(dt.UTC)
    return now - dt.timedelta(days=30), now + dt.timedelta(minutes=1)


def _poll(
    storage: Storage, httpx_mock: HTTPXMock, mbid: str, groups: list[dict[str, object]]
) -> None:
    httpx_mock.add_response(url=_browse_url(mbid), json=mb_browse_response(*groups))
    watch_artist(storage, _client(), mbid)


def _backdate(storage: Storage, days: int) -> None:
    """Age everything recorded so far by ``days``.

    A real install baselines an artist once, long before the window anyone
    simulates. Tests that poll twice in the same millisecond would otherwise
    land the baseline inside every window, which the replay correctly refuses
    to reconstruct — so the fixture has to look like an install with a past.
    """
    shift = dt.timedelta(days=days)
    with storage.session() as session:
        for group in session.exec(select(ReleaseGroup)).all():
            group.first_seen_at = group.first_seen_at - shift
            group.updated_at = group.updated_at - shift
            session.add(group)
        for event in session.exec(select(ReleaseEvent)).all():
            event.created_at = event.created_at - shift
            session.add(event)
        session.commit()


def _outcomes(storage: Storage, proposed: SettingsOverride | None = None) -> list[str]:
    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end, proposed_defaults=proposed)
    return [outcome.outcome for outcome in report.outcomes]


# --- The anchor: the replay has to agree with the real delivery path ----------


def test_simulating_the_policy_in_force_reproduces_recorded_deliveries(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """#58's first acceptance criterion, against rows the real path wrote.

    Built through `watch_artist` + `ensure_deliveries`, not by hand: if the
    simulator and the delivery engine ever disagree about muting, routing,
    channel age or type filters, this is where it shows.
    """
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.add_channel(name="inbox", url=SENTINEL_URL + "/2", mode="digest")
    # Baseline poll first, so the window's observations are post-baseline.
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old Record", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [
            mb_release_group("rg-0", "Old Record", "Album"),
            mb_release_group("rg-1", "New Record", "Album"),
            mb_release_group("rg-2", "A Single", "Single"),
        ],
    )
    storage.ensure_deliveries()
    with storage.session() as session:
        recorded = list(session.exec(select(Delivery)).all())
    actual: dict[str, int] = {}
    for channel in storage.list_channels(enabled_only=True):
        actual[channel.name] = sum(1 for d in recorded if d.channel_id == channel.id)

    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)

    assert report.deliveries_by_channel() == actual
    assert report.deliveries == len(recorded)
    # And the shape, not only the totals: the album delivered, the single was
    # filtered by the albums-only default and never became an event.
    counts = report.counts_by_outcome()
    assert counts[OUTCOME_DELIVERED] == 1
    assert counts[OUTCOME_FILTERED] == 1


# --- Why the corpus is release-groups and not the event log ------------------


def test_widening_to_singles_finds_the_singles_that_never_became_events(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """The whole point. An event-log replay reports +0 here and is wrong.

    Under the albums-only default the single raised no event, so it is absent
    from `events` entirely. It is present in `release_groups`, because the diff
    engine records a filtered group on purpose — "so a later opt-in starts from
    truth".
    """
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old Record", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [
            mb_release_group("rg-0", "Old Record", "Album"),
            mb_release_group("rg-1", "A Single", "Single"),
            mb_release_group("rg-2", "Another Single", "Single"),
        ],
    )
    assert len(storage.list_events()) == 0, "singles must not have raised events"

    in_force = _outcomes(storage)
    assert in_force.count(OUTCOME_FILTERED) == 2
    assert in_force.count(OUTCOME_DELIVERED) == 0

    widened = _outcomes(
        storage, SettingsOverride(allow_primary=("album", "single"), allow_secondary=())
    )
    # Exactly the singles present, and nothing invented.
    assert widened.count(OUTCOME_DELIVERED) == 2
    assert widened.count(OUTCOME_FILTERED) == 0


def test_diff_lists_only_the_observations_whose_outcome_moves(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [
            mb_release_group("rg-0", "Old", "Album"),
            mb_release_group("rg-1", "New Album", "Album"),
            mb_release_group("rg-2", "A Single", "Single"),
        ],
    )
    start, end = _window(storage)
    in_force = run_simulation(storage, window_start=start, window_end=end)
    widened = run_simulation(
        storage,
        window_start=start,
        window_end=end,
        proposed_defaults=SettingsOverride(allow_primary=("album", "single")),
    )
    changes = diff_reports(in_force, widened)
    assert [c.observation.title for c in changes] == ["A Single"]
    assert changes[0].before == OUTCOME_FILTERED
    assert changes[0].after == OUTCOME_DELIVERED


def test_narrowing_is_reported_as_well_as_widening(storage: Storage, httpx_mock: HTTPXMock) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    narrowed = _outcomes(storage, SettingsOverride(allow_primary=("ep",)))
    assert narrowed.count(OUTCOME_FILTERED) == 1
    assert narrowed.count(OUTCOME_DELIVERED) == 0


# --- The gaps stay visible ---------------------------------------------------


def test_an_artist_baselined_inside_the_window_is_excluded_and_named(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """A back catalogue is silent on the first poll under every policy.

    Counting it as "would not have been delivered" would be true and useless;
    counting it as deliverable under a wider policy would be false. It is
    excluded, and the report says how many and why.
    """
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [
            mb_release_group("rg-0", "Album One", "Album"),
            mb_release_group("rg-1", "Album Two", "Album"),
        ],
    )
    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)
    assert report.counts_by_outcome() == {OUTCOME_BASELINE: 2}
    assert report.artists_baselined_in_window == (ARTIST,)
    assert report.replayable == 0
    text = format_report(report)
    assert "were first polled inside this window" in text
    assert "ADR-0011" in text


def test_an_artist_baselined_before_the_window_replays_normally(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    start, end = _window(storage)
    # The baseline is 40 days back; the window reaches 30.
    report = run_simulation(storage, window_start=start, window_end=end)
    assert report.artists_baselined_in_window == ()
    assert report.counts_by_outcome().get(OUTCOME_BASELINE) is None


def test_quiet_days_are_counted_because_a_missed_run_is_not_a_quiet_day(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    now = dt.datetime.now(dt.UTC)
    report = run_simulation(storage, window_start=now - dt.timedelta(days=9), window_end=now)
    # Ten calendar days in the window, observations on exactly one of them.
    assert report.days_without_observations == 9
    assert "day(s) in the window recorded nothing at all" in format_report(report)


def test_an_empty_window_says_nothing_to_simulate_rather_than_a_table_of_zeros(
    storage: Storage,
) -> None:
    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)
    text = format_report(report)
    assert "Nothing to simulate." in text
    assert "Deliveries" not in text
    assert "Noisiest" not in text


# --- Sharing the delivery path's own predicates -------------------------------


def test_a_muted_artist_produces_no_deliveries_but_the_event_still_exists(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    storage.set_artist_settings("rk-1", SettingsOverride(muted=True))
    outcomes = _outcomes(storage)
    assert outcomes.count(OUTCOME_MUTED) == 1
    assert outcomes.count(OUTCOME_DELIVERED) == 0


def test_a_route_that_declines_the_event_is_reported_as_routed_away(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """Not delivered, and not filtered either — the event exists, unsent."""
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.set_channel_route("phone", ChannelRoute(primary_types=frozenset({"ep"})))
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    outcomes = _outcomes(storage)
    assert outcomes.count(OUTCOME_ROUTED_AWAY) == 1


def test_an_install_with_no_channel_reports_zero_deliveries_not_an_error(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)
    assert report.deliveries_by_channel() == {}
    assert report.deliveries == 0
    assert "no enabled channel" in format_report(report)


def test_a_channel_that_would_receive_nothing_reads_as_zero_not_as_absent(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """A channel missing from the table looks like a channel that is not there."""
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.add_channel(name="inbox", url=SENTINEL_URL + "/2", mode="digest")
    storage.set_channel_route("inbox", ChannelRoute(primary_types=frozenset({"ep"})))
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    start, end = _window(storage)
    per_channel = run_simulation(
        storage, window_start=start, window_end=end
    ).deliveries_by_channel()
    assert per_channel == {"phone": 1, "inbox": 0}


def test_several_watched_artists_are_ranked_by_how_loud_they_are(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    _watch(storage, "rk-2", "Nadja", OTHER)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    for mbid in (ARTIST, OTHER):
        _poll(storage, httpx_mock, mbid, [mb_release_group(f"rg-{mbid}-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [
            mb_release_group(f"rg-{ARTIST}-0", "Old", "Album"),
            mb_release_group("rg-a1", "One", "Album"),
            mb_release_group("rg-a2", "Two", "Album"),
        ],
    )
    _poll(
        storage,
        httpx_mock,
        OTHER,
        [
            mb_release_group(f"rg-{OTHER}-0", "Old", "Album"),
            mb_release_group("rg-b1", "Only", "Album"),
        ],
    )
    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)
    assert report.noisiest() == [("Mogwai", 2), ("Nadja", 1)]


# --- The command ------------------------------------------------------------


def test_the_command_opens_no_socket(
    storage: Storage, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#58 asks for this in terms, and it is the whole premise of a replay."""
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    storage.close()

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("settings simulate opened a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(socket, "create_connection", _no_sockets)
    assert (
        main(
            [
                "settings",
                "simulate",
                "--data-dir",
                str(tmp_path / "data"),
                "--allow-primary",
                "album,single",
            ]
        )
        == 0
    )


def test_the_command_rejects_a_window_it_cannot_parse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["settings", "simulate", "--data-dir", str(tmp_path / "d"), "--since", "last week"])
        == 1
    )
    assert "must look like 30d or 6w" in capsys.readouterr().err


def test_an_omitted_flag_inherits_rather_than_resetting_the_other_half(
    storage: Storage, httpx_mock: HTTPXMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--allow-secondary live` must not silently narrow the primaries.

    Proposing a policy the operator did not state would answer a different
    question from the one they asked.
    """
    storage.set_watch_default_types(allow_primary=["album", "ep"])
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "An EP", "EP")],
    )
    storage.close()
    assert (
        main(
            [
                "settings",
                "simulate",
                "--data-dir",
                str(tmp_path / "data"),
                # Shorter than the fixture's 40-day-old baseline poll, so the
                # window contains the EP and not the back catalogue.
                "--since",
                "30d",
                "--allow-secondary",
                "live",
            ]
        )
        == 0
    )
    # The EP still delivers: the stored `ep` primary survived the proposal.
    assert "would be delivered" in capsys.readouterr().out


def test_json_output_carries_the_gaps_as_fields(
    storage: Storage, httpx_mock: HTTPXMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    _watch(storage, "rk-1", "Mogwai", ARTIST)
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    storage.close()
    assert main(["settings", "simulate", "--data-dir", str(tmp_path / "data"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artists_baselined_in_window"] == [ARTIST]
    assert payload["days_without_observations"] >= 0
    assert payload["replayable"] == 0


def test_a_type_filtered_release_reads_as_filtered_even_when_the_artist_is_muted(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """Order matters, and it is the delivery path's order, not a convenient one.

    Type filters act at event creation (`watch/engine.py`); muting acts at
    fan-out (`ensure_deliveries`). A release the policy excludes never becomes
    an event, so muting never gets a say about it. Reporting it as "suppressed
    — artist muted" would tell the operator that un-muting would bring it back,
    which is false: only widening the type policy would.
    """
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=40)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "A Single", "Single")],
    )
    storage.set_artist_settings("rk-1", SettingsOverride(muted=True))
    outcomes = _outcomes(storage)
    assert outcomes == [OUTCOME_FILTERED]
    assert OUTCOME_MUTED not in outcomes


def test_a_channel_added_after_an_observation_receives_nothing_from_it(
    storage: Storage, httpx_mock: HTTPXMock
) -> None:
    """Adding a channel must not replay history — the same rule fan-out applies.

    `Storage.ensure_deliveries` skips any event older than the channel itself,
    so a replay that ignored channel age would promise a brand-new channel a
    backlog it will never actually receive.
    """
    _watch(storage, "rk-1", "Mogwai", ARTIST)
    _poll(storage, httpx_mock, ARTIST, [mb_release_group("rg-0", "Old", "Album")])
    _backdate(storage, days=35)
    _poll(
        storage,
        httpx_mock,
        ARTIST,
        [mb_release_group("rg-0", "Old", "Album"), mb_release_group("rg-1", "New", "Album")],
    )
    # The release is five days old; the channel is created now.
    _backdate(storage, days=5)
    storage.add_channel(name="latecomer", url=SENTINEL_URL, mode="instant")

    start, end = _window(storage)
    report = run_simulation(storage, window_start=start, window_end=end)
    assert [o.outcome for o in report.outcomes] == [OUTCOME_ROUTED_AWAY]
    assert report.deliveries_by_channel() == {"latecomer": 0}

    # And the real path agrees: no delivery row is created for it either.
    created, _muted = storage.ensure_deliveries()
    assert created == 0
