"""Per-channel routing rules (#65).

Each of #65's four "Done when" clauses is a test here. Two properties get
their own guards because both are the kind that decay quietly:

- **an unrouted channel behaves exactly as it did before routing existed** —
  the identity predicate, and today's fan-out unchanged;
- **a rule that could never match is refused when it is written**, not obeyed
  forever. A channel routed to a library key no library has would deliver
  nothing and look broken; that is the same shape as F10's #33 regression,
  where an absent policy was read as "no restriction".
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from encore import cli
from encore.artistsettings import SettingsError, SettingsOverride
from encore.channelroute import (
    KNOWN_SOURCES,
    ROUTE_KEYS,
    ChannelRoute,
    RoutableEvent,
    canonical_route_json,
    channel_accepts,
    describe_route,
    parse_route_json,
)
from encore.models import Artist, Delivery, ReleaseEvent, ReleaseGroup
from encore.storage import Storage, StorageError

SENTINEL_URL = "ntfy://example.invalid/topic"


def _event(
    *,
    priority: str = "normal",
    primary_type: str | None = "Album",
    secondary_types: tuple[str, ...] = (),
    library_keys: frozenset[str] = frozenset({"1"}),
    artist_keys: frozenset[str] = frozenset({"a1"}),
    source: str = "plex",
) -> RoutableEvent:
    return RoutableEvent(
        priority=priority,
        primary_type=primary_type,
        secondary_types=secondary_types,
        library_keys=library_keys,
        artist_keys=artist_keys,
        source=source,
    )


# --- The identity predicate: an unrouted channel is unchanged ------------------


def test_an_empty_route_accepts_everything() -> None:
    """`ChannelRoute()` must be the identity, or an upgrade silences channels."""
    empty = ChannelRoute()
    assert empty.is_empty()
    assert canonical_route_json(empty) is None
    for event in (
        _event(),
        _event(priority="instant"),
        _event(priority="digest", primary_type=None),
        _event(source="promoted", library_keys=frozenset(), artist_keys=frozenset()),
        _event(primary_type="EP", secondary_types=("Live", "Compilation")),
    ):
        assert channel_accepts(event, empty)


def test_a_route_naming_one_key_does_not_filter_by_the_others() -> None:
    """Absent keys mean 'all'. A priority route must not also filter by type."""
    route = ChannelRoute(priority=frozenset({"instant"}))
    assert channel_accepts(_event(priority="instant", primary_type="Single"), route)
    assert channel_accepts(_event(priority="instant", source="promoted"), route)
    assert not channel_accepts(_event(priority="normal"), route)


# --- Done when #1: priority routing ------------------------------------------


def test_an_instant_route_takes_instant_events_only() -> None:
    routed = ChannelRoute(priority=frozenset({"instant"}))
    unrouted = ChannelRoute()
    instant_event = _event(priority="instant")
    normal_event = _event(priority="normal")
    assert channel_accepts(instant_event, routed)
    assert channel_accepts(instant_event, unrouted)
    assert not channel_accepts(normal_event, routed)
    assert channel_accepts(normal_event, unrouted)


# --- Done when #2: a library route excludes a promoted (non-Plex) artist ------


def test_a_library_route_excludes_a_promoted_artist() -> None:
    """An F8-promoted artist owns no Plex row, so it is in no library."""
    route = ChannelRoute(library_keys=frozenset({"1"}))
    promoted = _event(source="promoted", library_keys=frozenset(), artist_keys=frozenset())
    assert not channel_accepts(promoted, route)
    assert channel_accepts(_event(library_keys=frozenset({"1"})), route)


def test_the_rendering_says_which_rule_applies() -> None:
    route = ChannelRoute(library_keys=frozenset({"1"}), priority=frozenset({"instant"}))
    rendered = describe_route(route)
    assert "library_keys=1" in rendered
    assert "priority=instant" in rendered
    assert describe_route(ChannelRoute()) == "all"


# --- Done when #4: an unmatchable rule is refused at write time ---------------


def test_an_unknown_library_key_is_refused_when_it_is_written(tmp_path: Path) -> None:
    """Refused by name, not silently matched to nothing."""
    storage = Storage(tmp_path)
    try:
        _seed_artist(storage, "a1", "First", library_key="1")
        storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
        with pytest.raises(StorageError, match="unknown value"):
            storage.set_channel_route("phone", ChannelRoute(library_keys=frozenset({"99"})))
        # And the channel is left unrouted rather than half-written.
        assert storage.get_channel_route("phone").is_empty()
        storage.set_channel_route("phone", ChannelRoute(library_keys=frozenset({"1"})))
        assert storage.get_channel_route("phone").library_keys == frozenset({"1"})
    finally:
        storage.close()


def test_an_unknown_release_type_is_refused() -> None:
    with pytest.raises(SettingsError, match="unknown value"):
        parse_route_json(json.dumps({"primary_types": ["lp"]}))
    with pytest.raises(SettingsError, match="unknown value"):
        parse_route_json(json.dumps({"secondary_types": ["bootleg-ish"]}))


def test_an_unknown_priority_tier_is_refused() -> None:
    with pytest.raises(SettingsError, match="unknown value"):
        parse_route_json(json.dumps({"priority": ["urgent"]}))


def test_a_source_nothing_produces_is_refused() -> None:
    """`imported` is #55's source and nothing writes it yet.

    Accepting it would let an operator route a channel to a source that can
    never match — a channel that silently receives nothing forever.
    """
    assert "imported" not in KNOWN_SOURCES
    with pytest.raises(SettingsError, match="unknown value"):
        parse_route_json(json.dumps({"sources": ["imported"]}))
    assert parse_route_json(json.dumps({"sources": ["promoted"]})).sources == frozenset(
        {"promoted"}
    )


def test_an_unknown_route_key_is_refused() -> None:
    with pytest.raises(SettingsError, match="unknown key"):
        parse_route_json(json.dumps({"librarykeys": ["1"]}))


def test_an_explicitly_empty_list_is_refused() -> None:
    """`[]` reads as 'none of them' — a channel wired to receive nothing."""
    with pytest.raises(SettingsError, match="would route nothing"):
        parse_route_json(json.dumps({"priority": []}))


def test_every_route_key_is_actually_honoured_by_the_predicate() -> None:
    """A key the parser accepts and the predicate ignores is a rule that lies.

    Walks `ROUTE_KEYS`, builds a route naming a value the event does not have,
    and asserts the event is declined. A key added to the schema but never
    wired into `channel_accepts` fails here rather than shipping as a filter
    that quietly does nothing.
    """
    mismatches: dict[str, ChannelRoute] = {
        "priority": ChannelRoute(priority=frozenset({"digest"})),
        "primary_types": ChannelRoute(primary_types=frozenset({"single"})),
        "secondary_types": ChannelRoute(secondary_types=frozenset({"live"})),
        "library_keys": ChannelRoute(library_keys=frozenset({"other"})),
        "artist_keys": ChannelRoute(artist_keys=frozenset({"other"})),
        "sources": ChannelRoute(sources=frozenset({"promoted"})),
    }
    assert set(mismatches) == set(ROUTE_KEYS), "a route key has no case here"
    baseline = _event(priority="normal", primary_type="Album", secondary_types=())
    for key, route in mismatches.items():
        assert not channel_accepts(baseline, route), f"route key {key!r} is not honoured"


# --- Canonicalization ---------------------------------------------------------


def test_two_writes_of_the_same_route_store_the_same_bytes() -> None:
    first = ChannelRoute(priority=frozenset({"instant", "digest"}))
    second = ChannelRoute(priority=frozenset({"digest", "instant"}))
    assert canonical_route_json(first) == canonical_route_json(second)


def test_a_route_round_trips_through_its_stored_form() -> None:
    route = ChannelRoute(
        priority=frozenset({"instant"}),
        primary_types=frozenset({"album", "ep"}),
        sources=frozenset({"plex"}),
    )
    assert parse_route_json(canonical_route_json(route)) == route


# --- Fan-out: a routed-away event is never materialised ------------------------


def _seed_artist(
    storage: Storage, key: str, name: str, *, library_key: str = "1", mbid: str | None = None
) -> None:
    with storage.session() as session:
        session.add(Artist(plex_rating_key=key, name=name, library_key=library_key))
        session.commit()
    if mbid is not None:
        storage.save_artist_match(key, name, "auto", mbid=mbid)


def _seed_event(storage: Storage, artist_mbid: str, *, primary_type: str = "Album") -> int:
    with storage.session() as session:
        group = ReleaseGroup(
            mbid=f"rg-{artist_mbid}",
            artist_mbid=artist_mbid,
            title="A Record",
            primary_type=primary_type,
        )
        session.add(group)
        session.commit()
        session.refresh(group)
        assert group.id is not None
        event = ReleaseEvent(release_group_id=group.id, kind="new")
        session.add(event)
        session.commit()
        session.refresh(event)
        assert event.id is not None
        return event.id


def _deliveries_for(storage: Storage, event_id: int) -> set[str]:
    """Channel names that have a Delivery row for this event."""
    from sqlmodel import select

    from encore.models import NotificationChannel

    with storage.session() as session:
        rows = session.exec(select(Delivery).where(Delivery.event_id == event_id)).all()
        names = {}
        for channel in session.exec(select(NotificationChannel)).all():
            names[channel.id] = channel.name
    return {names[row.channel_id] for row in rows}


def test_a_routed_away_event_is_never_materialised(tmp_path: Path) -> None:
    """#65's fixture clause, end to end through the real fan-out.

    Two channels, one routed to instant priority. An instant-artist event
    creates deliveries for both; a normal-artist event only for the unrouted
    one — and the routed channel gets no row at all, so nothing can later
    retry or count it.
    """
    storage = Storage(tmp_path)
    try:
        _seed_artist(storage, "a1", "Loud", mbid="mbid-loud")
        _seed_artist(storage, "a2", "Quiet", mbid="mbid-quiet")
        storage.set_artist_settings("a1", SettingsOverride(priority="instant"))

        storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
        storage.add_channel(name="inbox", url=SENTINEL_URL + "/2", mode="digest")
        storage.set_channel_route("phone", ChannelRoute(priority=frozenset({"instant"})))

        loud_event = _seed_event(storage, "mbid-loud")
        quiet_event = _seed_event(storage, "mbid-quiet")
        storage.ensure_deliveries(dt.datetime.now(dt.UTC))

        assert _deliveries_for(storage, loud_event) == {"phone", "inbox"}
        assert _deliveries_for(storage, quiet_event) == {"inbox"}
    finally:
        storage.close()


def test_removing_a_route_restores_todays_fan_out(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    try:
        _seed_artist(storage, "a2", "Quiet", mbid="mbid-quiet")
        storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
        storage.set_channel_route("phone", ChannelRoute(priority=frozenset({"instant"})))
        storage.set_channel_route("phone", ChannelRoute())
        assert storage.get_channel_route("phone").is_empty()

        event_id = _seed_event(storage, "mbid-quiet")
        storage.ensure_deliveries(dt.datetime.now(dt.UTC))
        assert _deliveries_for(storage, event_id) == {"phone"}
    finally:
        storage.close()


def test_a_library_routed_channel_receives_nothing_for_a_promoted_artist(
    tmp_path: Path,
) -> None:
    """Route by library, through the real fan-out rather than the predicate.

    A promoted identity has no Plex row, so it is in no library.
    """
    storage = Storage(tmp_path)
    try:
        _seed_artist(storage, "a1", "Owned", library_key="1", mbid="mbid-owned")
        storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
        storage.set_channel_route("phone", ChannelRoute(library_keys=frozenset({"1"})))

        owned_event = _seed_event(storage, "mbid-owned")
        promoted_event = _seed_event(storage, "mbid-promoted-nobody-owns")
        storage.ensure_deliveries(dt.datetime.now(dt.UTC))

        assert _deliveries_for(storage, owned_event) == {"phone"}
        assert _deliveries_for(storage, promoted_event) == set()
    finally:
        storage.close()


def test_an_unreadable_route_delivers_everything_rather_than_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Widen, never silence, when a filter stops parsing.

    A filter is a narrowing rule; turning one into an outage because it became
    unreadable is the wrong direction to fail. It must say so, not fail quietly.
    """
    from sqlmodel import select

    from encore.models import NotificationChannel

    storage = Storage(tmp_path)
    try:
        _seed_artist(storage, "a1", "Owned", mbid="mbid-owned")
        storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
        with storage.session() as session:
            channel = session.exec(select(NotificationChannel)).one()
            channel.route_json = "{not json"
            session.add(channel)
            session.commit()

        event_id = _seed_event(storage, "mbid-owned")
        with caplog.at_level("WARNING"):
            storage.ensure_deliveries(dt.datetime.now(dt.UTC))
        assert _deliveries_for(storage, event_id) == {"phone"}
        assert any("unreadable route" in record.message for record in caplog.records)
    finally:
        storage.close()


# --- CLI ----------------------------------------------------------------------


def test_cli_route_and_unroute(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    storage = Storage(tmp_path)
    _seed_artist(storage, "a1", "Owned", library_key="1")
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()

    assert (
        cli.main(
            [
                "channels",
                "route",
                "--data-dir",
                str(tmp_path),
                "--name",
                "phone",
                "--priority",
                "instant",
            ]
        )
        == 0
    )
    assert "priority=instant" in capsys.readouterr().out

    assert cli.main(["channels", "list", "--data-dir", str(tmp_path)]) == 0
    assert "route: priority=instant" in capsys.readouterr().out

    assert cli.main(["channels", "unroute", "--data-dir", str(tmp_path), "--name", "phone"]) == 0
    assert "unrouted" in capsys.readouterr().out

    assert cli.main(["channels", "list", "--data-dir", str(tmp_path)]) == 0
    assert "route: all" in capsys.readouterr().out


def test_cli_route_with_no_filters_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A route with no filters is today's fan-out; say that with `unroute`."""
    storage = Storage(tmp_path)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()
    assert cli.main(["channels", "route", "--data-dir", str(tmp_path), "--name", "phone"]) == 2
    assert "no filters given" in capsys.readouterr().err


def test_cli_route_reports_an_unknown_library_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    _seed_artist(storage, "a1", "Owned", library_key="1")
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()
    assert (
        cli.main(
            ["channels", "route", "--data-dir", str(tmp_path), "--name", "phone", "--library", "99"]
        )
        == 1
    )
    assert "unknown value" in capsys.readouterr().err


@pytest.mark.no_secrets_in_logs
def test_cli_route_prints_no_channel_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = Storage(tmp_path)
    storage.add_channel(
        name="phone", url="ntfy://user:sentinel-pass@example.invalid/t", mode="instant"
    )
    storage.close()
    cli.main(
        [
            "channels",
            "route",
            "--data-dir",
            str(tmp_path),
            "--name",
            "phone",
            "--priority",
            "instant",
        ]
    )
    cli.main(["channels", "list", "--data-dir", str(tmp_path)])
    captured = capsys.readouterr()
    assert "sentinel-pass" not in captured.out + captured.err


def test_channels_list_still_lists_when_one_route_is_unreadable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The diagnostic must not fail on the very fault it exists to show.

    `channels list` is where an operator looks to find out what is wrong with
    their channels. Aborting the whole listing because one stored route stopped
    parsing would break it exactly when it is needed — and it would hide the
    healthy channels too. The unreadable route is named, and described in the
    terms the fan-out actually acts on, so the listing never implies a filter
    is in force when none is.
    """
    from sqlmodel import select

    from encore.models import NotificationChannel

    storage = Storage(tmp_path)
    storage.add_channel(name="broken", url=SENTINEL_URL, mode="instant")
    storage.add_channel(name="healthy", url=SENTINEL_URL + "/2", mode="instant")
    storage.set_channel_route("healthy", ChannelRoute(priority=frozenset({"instant"})))
    with storage.session() as session:
        channel = session.exec(
            select(NotificationChannel).where(NotificationChannel.name == "broken")
        ).one()
        channel.route_json = "{not json"
        session.add(channel)
        session.commit()
    storage.close()

    assert cli.main(["channels", "list", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "broken" in out
    assert "UNREADABLE" in out
    assert "delivering everything" in out
    # The healthy channel is still listed with its real route.
    assert "healthy" in out
    assert "route: priority=instant" in out
