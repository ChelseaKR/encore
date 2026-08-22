"""F10 — per-artist and global watch settings: types, muting, priority.

The noise-control keystone (encore-plans/03 F10): defaults produce a quiet
radar without any tuning — **albums-only** — while EPs, singles, live
recordings, and compilations are one command away, globally or for a single
artist. Three knobs, all resolved here so every consumer reads the same
policy:

- **Release types.** A release-group reaches you only when its MusicBrainz
  type tags are all opted in: its primary type (album/ep/single/broadcast/
  other) is in the allowed set *and* every secondary type (live, remix,
  compilation, …) is too. A live album therefore needs both ``album``-class
  primaries allowed and ``live`` opted in — "albums but not live albums"
  is the default, not a special case.
- **Muting.** ``muted`` (indefinite) or ``mute_until`` (a date), never both.
  Muting suppresses *deliveries only*: events are still recorded, so feeds,
  RSS, and iCal stay truthful, and un-muting never replays history.
- **Priority.** ``instant`` artists bypass digest windows (a heavy-rotation
  favorite breaks through a daily rollup); ``digest`` artists wait for the
  window even on instant channels; ``normal`` follows the channel's mode.

Storage shape: the global default type allowlist lives on the settings
singleton (`AppSettings.watch_defaults_json`); a per-artist override lives
on the Plex artist row (`Artist.settings_json`) as partial JSON — absent
keys inherit the global default. Resolution across the two layers happens
in `encore.storage`; this module owns the vocabulary, parsing, validation,
and the pure pass/mute predicates.

Conservative by construction: a type tag outside the known universe blocks
the group rather than passing it. MusicBrainz adding a brand-new secondary
type can therefore silence affected groups until encore learns the tag —
the failure mode chosen is "miss a niche release," never "spray noise."
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace

__all__ = [
    "DEFAULT_ALLOWED_PRIMARY",
    "DEFAULT_ALLOWED_SECONDARY",
    "PRIMARY_TYPE_SLUGS",
    "PRIORITY_DIGEST",
    "PRIORITY_INSTANT",
    "PRIORITY_NORMAL",
    "PRIORITY_TIERS",
    "SECONDARY_TYPE_SLUGS",
    "ArtistWatchSettings",
    "SettingsError",
    "SettingsOverride",
    "canonical_override_json",
    "group_type_tags",
    "parse_primary_types",
    "parse_secondary_types",
    "parse_settings_json",
    "resolve_effective",
    "slug_for_primary",
    "slug_for_secondary",
]


class SettingsError(ValueError):
    """A watch-settings value is not something encore can act on."""


# MusicBrainz WS/2 primary types (release-group `primary-type`).
PRIMARY_TYPE_SLUGS = frozenset({"album", "ep", "single", "broadcast", "other"})

# MusicBrainz WS/2 secondary types (`secondary-types`), slugified. The
# canonical names arrive as e.g. "Mixtape/Street", "DJ-mix", "Non-music";
# slugs are lowercase with non-alphanumerics collapsed to hyphens so they
# are safe on a CLI flag line.
SECONDARY_TYPE_SLUGS = frozenset(
    {
        "audiobook",
        "compilation",
        "demo",
        "dj-mix",
        "field-recording",
        "interview",
        "live",
        "mixtape-street",
        "non-music",
        "remix",
        "soundtrack",
        "spokenword",
    }
)

DEFAULT_ALLOWED_PRIMARY = ("album",)
DEFAULT_ALLOWED_SECONDARY: tuple[str, ...] = ()

PRIORITY_NORMAL = "normal"
PRIORITY_INSTANT = "instant"
PRIORITY_DIGEST = "digest"
PRIORITY_TIERS = (PRIORITY_NORMAL, PRIORITY_INSTANT, PRIORITY_DIGEST)


def _slugify(tag: str) -> str:
    """Canonicalize one raw type tag the way MB names map to config slugs."""
    cleaned = "".join(char if char.isalnum() else "-" for char in tag.strip().casefold())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def slug_for_primary(raw: str) -> str | None:
    """Map a MusicBrainz primary-type name to its slug (``None`` if unknown)."""
    slug = _slugify(raw)
    return slug if slug in PRIMARY_TYPE_SLUGS else None


def slug_for_secondary(raw: str) -> str | None:
    """Map a MusicBrainz secondary-type name to its slug (``None`` if unknown)."""
    slug = _slugify(raw)
    return slug if slug in SECONDARY_TYPE_SLUGS else None


def group_type_tags(primary_type: str | None, secondary_types: tuple[str, ...]) -> tuple[str, ...]:
    """Return the full set of type tags one release-group must clear.

    A missing primary type counts as ``other`` (MusicBrainz rarely omits it;
    when it does, the group is niche by definition). Unknown secondary tags
    survive as opaque slugs — they will never sit in a validated allowlist,
    which is exactly how an unrecognized future type stays conservative.
    """
    tags: list[str] = []
    if primary_type is None or not primary_type.strip():
        tags.append("other")
    else:
        tags.append(slug_for_primary(primary_type) or _slugify(primary_type))
    for raw in secondary_types:
        tags.append(slug_for_secondary(raw) or _slugify(raw))
    return tuple(dict.fromkeys(tags))


def parse_primary_types(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated primary-type allowlist, validating each slug."""
    return _parse_type_list(raw, PRIMARY_TYPE_SLUGS, "primary")


def parse_secondary_types(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated secondary-type allowlist, validating each slug."""
    return _parse_type_list(raw, SECONDARY_TYPE_SLUGS, "secondary")


def _parse_type_list(raw: str, universe: frozenset[str], kind: str) -> tuple[str, ...]:
    parts = [part.strip() for part in raw.split(",")]
    parsed: list[str] = []
    for part in parts:
        if not part:
            continue
        slug = _slugify(part)
        if slug not in universe:
            raise SettingsError(
                f"unknown {kind} release type {part!r}; "
                f"known {kind} types: {', '.join(sorted(universe))}"
            )
        if slug not in parsed:
            parsed.append(slug)
    if not parsed:
        raise SettingsError(f"empty {kind} release-type list")
    return tuple(parsed)


@dataclass(frozen=True)
class SettingsOverride:
    """One layer of watch settings as stored — absent keys mean inherit.

    Tri-state fields distinguish "not configured" (``None``) from an
    explicit value, which is what makes per-artist JSON a *partial*
    override over the global defaults instead of a replacement. ``muted``
    and ``mute_until`` are mutually exclusive by validation.
    """

    allow_primary: tuple[str, ...] | None = None
    allow_secondary: tuple[str, ...] | None = None
    muted: bool | None = None
    mute_until: dt.date | None = None
    priority: str | None = None

    def is_empty(self) -> bool:
        """Whether nothing at all is configured at this layer."""
        return self == SettingsOverride()


def _parse_mute_fields(payload: dict[str, object]) -> tuple[bool | None, dt.date | None]:
    """Extract and validate the (muted, mute_until) pair from stored JSON."""
    muted_raw = payload.get("muted")
    until_raw = payload.get("mute_until")
    muted: bool | None = None
    if muted_raw is not None:
        if not isinstance(muted_raw, bool):
            raise SettingsError("muted must be true or false")
        muted = muted_raw
    mute_until: dt.date | None = None
    if until_raw is not None:
        try:
            mute_until = dt.date.fromisoformat(str(until_raw))
        except ValueError as exc:
            raise SettingsError(f"mute_until must be an ISO date: {exc}") from exc
    if muted and mute_until is not None:
        raise SettingsError("muted and mute_until are mutually exclusive")
    return muted, mute_until


def parse_settings_json(raw: str | None) -> SettingsOverride:
    """Parse one stored settings JSON blob into an override layer.

    Accepts ``None``/empty (nothing configured) and tolerates unknown keys
    (forward compatibility) but validates every value it knows: slugs must
    exist, dates must be ISO, priority must be a known tier, and the mute
    fields must not contradict each other.
    """
    if raw is None or not raw.strip():
        return SettingsOverride()
    try:
        payload: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsError(f"stored watch settings are not valid JSON: {exc}") from exc
    if payload is None:
        return SettingsOverride()
    if not isinstance(payload, dict):
        raise SettingsError("stored watch settings must be a JSON object")
    allow_primary_raw = payload.get("allow_primary")
    allow_secondary_raw = payload.get("allow_secondary")
    priority_raw = payload.get("priority")
    muted, mute_until = _parse_mute_fields(payload)
    priority: str | None = None
    if priority_raw is not None:
        priority = str(priority_raw)
        if priority not in PRIORITY_TIERS:
            raise SettingsError(
                f"invalid priority {priority!r}; expected {', '.join(PRIORITY_TIERS)}"
            )
    return SettingsOverride(
        allow_primary=_parse_stored_types(allow_primary_raw, PRIMARY_TYPE_SLUGS, "primary"),
        allow_secondary=_parse_stored_types(allow_secondary_raw, SECONDARY_TYPE_SLUGS, "secondary"),
        muted=muted,
        mute_until=mute_until,
        priority=priority,
    )


def _parse_stored_types(raw: object, universe: frozenset[str], kind: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise SettingsError(f"{kind}_types must be a list of slugs")
    parsed = tuple(dict.fromkeys(_slugify(item) for item in raw))
    unknown = [slug for slug in parsed if slug not in universe]
    if unknown:
        raise SettingsError(
            f"unknown {kind} release type(s) {', '.join(unknown)}; "
            f"known {kind} types: {', '.join(sorted(universe))}"
        )
    return parsed


def canonical_override_json(override: SettingsOverride) -> str | None:
    """Serialize an override to the canonical stored form (``None`` when empty).

    Canonicalization matters because two writes expressing the same policy
    should store the same bytes: slugs sorted, explicit ``None`` fields
    dropped, empty layers erased entirely so a fully-defaulted artist does
    not carry dead JSON forever.
    """
    payload: dict[str, object] = {}
    if override.allow_primary is not None:
        payload["allow_primary"] = sorted(set(override.allow_primary))
    if override.allow_secondary is not None:
        payload["allow_secondary"] = sorted(set(override.allow_secondary))
    if override.muted is not None:
        payload["muted"] = override.muted
    if override.mute_until is not None:
        payload["mute_until"] = override.mute_until.isoformat()
    if override.priority is not None:
        payload["priority"] = override.priority
    if not payload:
        return None
    return json.dumps(payload, sort_keys=True)


@dataclass(frozen=True)
class ArtistWatchSettings:
    """The effective watch settings for one artist identity — no inheritance left.

    Every field is concrete here; the global/per-artist layering has been
    collapsed by `resolve_effective`. These objects are taste-data-adjacent
    configuration (not library content) but carry no identifiers either way.
    """

    allow_primary: frozenset[str] = frozenset(DEFAULT_ALLOWED_PRIMARY)
    allow_secondary: frozenset[str] = frozenset()
    muted: bool = False
    mute_until: dt.date | None = None
    priority: str = PRIORITY_NORMAL

    def is_muted_on(self, today: dt.date) -> bool:
        """Whether muting is active on ``today`` (an expired date unmutes)."""
        if self.muted:
            return True
        return self.mute_until is not None and today <= self.mute_until

    def passes(self, primary_type: str | None, secondary_types: tuple[str, ...]) -> bool:
        """Whether a release-group's type tags are all opted in."""
        tags = group_type_tags(primary_type, secondary_types)
        primary_slug = tags[0]
        if primary_slug not in self.allow_primary:
            return False
        return all(tag in self.allow_secondary for tag in tags[1:])


def _apply_type_layer(
    effective: ArtistWatchSettings,
    primary: tuple[str, ...] | None,
    secondary: tuple[str, ...] | None,
) -> ArtistWatchSettings:
    """Overlay one layer's type allowlists onto the effective policy."""
    if primary is not None:
        effective = replace(effective, allow_primary=frozenset(primary))
    if secondary is not None:
        effective = replace(effective, allow_secondary=frozenset(secondary))
    return effective


def resolve_effective(
    defaults: SettingsOverride, override: SettingsOverride | None
) -> ArtistWatchSettings:
    """Collapse the global defaults plus one artist's override (or ``None``).

    Layering rules, deliberately simple to predict: an artist that sets any
    type allowlist replaces the corresponding global list outright (mixing
    "global allows live" with "this artist allows ep" would make per-artist
    state impossible to reason about); mute and priority exist per artist
    only, so the global layer cannot set them and `SettingsError` says so.
    Muting is *not* evaluated here — callers ask `is_muted_on` with their
    own clock, keeping this function pure and time-free.
    """
    if defaults.muted is not None or defaults.mute_until is not None:
        raise SettingsError("muting is per-artist; it cannot be a global default")
    if defaults.priority is not None:
        raise SettingsError("priority is per-artist; it cannot be a global default")
    effective = _apply_type_layer(
        ArtistWatchSettings(), defaults.allow_primary, defaults.allow_secondary
    )
    if override is None or override.is_empty():
        return effective
    effective = _apply_type_layer(effective, override.allow_primary, override.allow_secondary)
    if override.muted is not None:
        effective = replace(effective, muted=override.muted, mute_until=None)
    if override.mute_until is not None:
        effective = replace(effective, mute_until=override.mute_until, muted=False)
    if override.priority is not None:
        effective = replace(effective, priority=override.priority)
    return effective
