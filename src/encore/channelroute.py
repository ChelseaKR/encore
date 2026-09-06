"""Per-channel routing rules — the channel-side half of F10 (issue #65).

F4's fan-out is all-or-nothing: every enabled channel gets every deliverable
event, so a user with a loud phone channel and a quiet email digest cannot
have both without muting artists globally. F10 gave *artists* priority tiers
precisely so heavy-rotation favourites could break through; this is the same
idea at the other end of the wire.

A route is evaluated **after** F10 has decided an event is deliverable at all,
and **before** fan-out creates `Delivery` rows, so a routed-away event is
simply never materialised for that channel. Feeds stay complete: like muting,
routing suppresses deliveries only (docs/adr/0012).

Two rules make the difference between a filter and a trap.

**An absent key means "all".** A channel with no route behaves exactly as it
did before this existed, and a route naming only ``priority`` does not
silently start filtering by type. `ChannelRoute()` is the identity predicate.

**A rule that cannot ever match is rejected when it is written, not silently
obeyed forever.** A route naming a library key no library has, an unknown
release type, or a source nothing in this build produces would quietly deliver
nothing and look like a broken channel. :func:`parse_route_json` refuses all
three by name. That is the same reasoning as F10's own #33 regression, where
an absent policy was read as "no restriction": a rule whose meaning cannot be
established must not be given a convenient default meaning.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from encore.artistsettings import (
    PRIMARY_TYPE_SLUGS,
    PRIORITY_TIERS,
    SECONDARY_TYPE_SLUGS,
    SettingsError,
    group_type_tags,
)

__all__ = [
    "KNOWN_SOURCES",
    "ROUTE_KEYS",
    "ChannelRoute",
    "RoutableEvent",
    "canonical_route_json",
    "channel_accepts",
    "describe_route",
    "parse_route_json",
]

# The artist sources this build can actually produce.
#
# "imported" is deliberately absent. It is issue #55's watchlist-import source
# and nothing writes it yet, so accepting it here would let an operator route a
# channel to a source that can never match — a channel that silently receives
# nothing, which is the exact failure this module refuses elsewhere. When #55
# lands it adds the value here and the rule starts meaning something.
KNOWN_SOURCES = ("plex", "promoted")

ROUTE_KEYS = (
    "priority",
    "primary_types",
    "secondary_types",
    "library_keys",
    "artist_keys",
    "sources",
)


@dataclass(frozen=True)
class ChannelRoute:
    """One channel's subscription. Every ``None`` field means "all"."""

    priority: frozenset[str] | None = None
    primary_types: frozenset[str] | None = None
    secondary_types: frozenset[str] | None = None
    library_keys: frozenset[str] | None = None
    artist_keys: frozenset[str] | None = None
    sources: frozenset[str] | None = None

    def is_empty(self) -> bool:
        """Whether this route constrains nothing — i.e. today's fan-out."""
        return self == ChannelRoute()


@dataclass(frozen=True)
class RoutableEvent:
    """Everything a route may ask about one deliverable event.

    Built by storage from rows it already loads for the muting check, so
    routing adds no extra query per event.
    """

    priority: str
    primary_type: str | None
    secondary_types: tuple[str, ...]
    library_keys: frozenset[str]
    artist_keys: frozenset[str]
    source: str


def _parse_set(
    payload: dict[str, Any], key: str, *, allowed: tuple[str, ...] | None
) -> frozenset[str] | None:
    """Read one list-valued key, or ``None`` when it is absent.

    An explicitly empty list is refused rather than stored. ``[]`` reads
    naturally as "none of them", which would be a channel wired to receive
    nothing at all — `channels disable` says that, and says it reversibly.
    """
    if key not in payload:
        return None
    raw = payload[key]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SettingsError(f"route key {key!r} must be a list of strings")
    values = {item.strip() for item in raw if item.strip()}
    if not values:
        raise SettingsError(
            f"route key {key!r} is empty, which would route nothing to this channel; "
            "omit the key to mean 'all', or use `encore channels disable`"
        )
    if allowed is not None:
        unknown = sorted(values - set(allowed))
        if unknown:
            raise SettingsError(
                f"route key {key!r} names unknown value(s) {', '.join(unknown)}; "
                f"known: {', '.join(sorted(allowed))}"
            )
    return frozenset(values)


def parse_route_json(
    raw: str | None, *, known_library_keys: frozenset[str] | None = None
) -> ChannelRoute:
    """Parse a stored route blob into a :class:`ChannelRoute`.

    ``known_library_keys`` is supplied at *write* time so an unmatchable
    library key is refused when the operator types it. It is deliberately not
    supplied on read: a library that disappears from Plex later must not make
    an existing channel's stored route unparseable, which would take the
    channel down rather than merely narrow it.

    Raises:
        SettingsError: the blob is not an object, names an unknown key, or
            names a value that could never match.
    """
    if raw is None or not raw.strip():
        return ChannelRoute()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"route is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SettingsError("route must be a JSON object")
    unknown_keys = sorted(set(payload) - set(ROUTE_KEYS))
    if unknown_keys:
        raise SettingsError(
            f"route names unknown key(s) {', '.join(unknown_keys)}; known: {', '.join(ROUTE_KEYS)}"
        )
    route = ChannelRoute(
        priority=_parse_set(payload, "priority", allowed=PRIORITY_TIERS),
        primary_types=_parse_set(
            payload, "primary_types", allowed=tuple(sorted(PRIMARY_TYPE_SLUGS))
        ),
        secondary_types=_parse_set(
            payload, "secondary_types", allowed=tuple(sorted(SECONDARY_TYPE_SLUGS))
        ),
        library_keys=_parse_set(
            payload,
            "library_keys",
            allowed=tuple(sorted(known_library_keys)) if known_library_keys is not None else None,
        ),
        artist_keys=_parse_set(payload, "artist_keys", allowed=None),
        sources=_parse_set(payload, "sources", allowed=KNOWN_SOURCES),
    )
    return route


def canonical_route_json(route: ChannelRoute) -> str | None:
    """Serialize a route to its stored form (``None`` when it constrains nothing).

    Sorted members and sorted keys, so two writes expressing the same
    subscription store the same bytes and an unrouted channel carries no dead
    JSON.
    """
    payload: dict[str, list[str]] = {}
    for key in ROUTE_KEYS:
        value = getattr(route, key)
        if value is not None:
            payload[key] = sorted(value)
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def channel_accepts(event: RoutableEvent, route: ChannelRoute) -> bool:
    """Whether ``route`` subscribes to ``event``.

    Absent keys mean "all", so :func:`channel_accepts` against an empty route
    is unconditionally true and an unrouted channel behaves exactly as it did
    before routing existed.
    """
    if route.priority is not None and event.priority not in route.priority:
        return False
    if route.sources is not None and event.source not in route.sources:
        return False
    if route.library_keys is not None and not (event.library_keys & route.library_keys):
        return False
    if route.artist_keys is not None and not (event.artist_keys & route.artist_keys):
        return False
    if route.primary_types is not None or route.secondary_types is not None:
        tags = group_type_tags(event.primary_type, event.secondary_types)
        primary_slug, secondary_slugs = tags[0], tags[1:]
        if route.primary_types is not None and primary_slug not in route.primary_types:
            return False
        if route.secondary_types is not None and not (set(secondary_slugs) & route.secondary_types):
            return False
    return True


def describe_route(route: ChannelRoute) -> str:
    """Return a one-line rendering for `encore channels list`."""
    if route.is_empty():
        return "all"
    parts = [
        f"{key}={','.join(sorted(getattr(route, key)))}"
        for key in ROUTE_KEYS
        if getattr(route, key) is not None
    ]
    return " ".join(parts)
