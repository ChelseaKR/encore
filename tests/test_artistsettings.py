"""F10 watch-settings logic: vocabulary, validation, layering, pass rules.

These are the pure halves of per-artist settings — no database involved.
Storage round-trips, delivery suppression, and engine filtering each have
their own integration coverage; this file pins the policy semantics that
everything else leans on.
"""

from __future__ import annotations

import datetime as dt

import pytest

from encore.artistsettings import (
    DEFAULT_ALLOWED_PRIMARY,
    PRIORITY_DIGEST,
    PRIORITY_INSTANT,
    SettingsError,
    SettingsOverride,
    canonical_override_json,
    group_type_tags,
    parse_primary_types,
    parse_secondary_types,
    parse_settings_json,
    resolve_effective,
)

TODAY = dt.date(2026, 8, 22)


def test_default_policy_is_albums_only() -> None:
    settings = resolve_effective(SettingsOverride(), None)
    assert settings.allow_primary == frozenset(DEFAULT_ALLOWED_PRIMARY) == {"album"}
    assert settings.allow_secondary == frozenset()
    assert settings.passes("Album", ())
    assert not settings.passes("EP", ())
    assert not settings.passes("Single", ())


def test_a_live_album_needs_both_the_primary_and_the_secondary_opted_in() -> None:
    # "Albums but not live albums" is the default posture, expressed by the
    # all-tags rule rather than a special case.
    albums_only = resolve_effective(SettingsOverride(), None)
    assert not albums_only.passes("Album", ("Live",))
    opted_in = resolve_effective(
        SettingsOverride(allow_primary=("album",), allow_secondary=("live",)), None
    )
    assert opted_in.passes("Album", ("Live",))


def test_every_tag_must_clear_the_allowlist() -> None:
    settings = resolve_effective(
        SettingsOverride(allow_primary=("album", "ep"), allow_secondary=("live",)), None
    )
    assert not settings.passes("EP", ("Live", "Remix"))  # remix not opted in
    assert settings.passes("EP", ("Live",))


def test_an_unknown_type_tag_blocks_conservatively() -> None:
    # A future MusicBrainz secondary type must fail closed: silence over
    # noise is this feature's chosen error mode.
    settings = resolve_effective(SettingsOverride(), None)
    assert not settings.passes("Album", ("Brand-New-Type",))


def test_group_type_tags_maps_mb_names_and_treats_missing_primary_as_other() -> None:
    assert group_type_tags("Album", ()) == ("album",)
    assert group_type_tags("Single", ("Mixtape/Street",)) == ("single", "mixtape-street")
    assert group_type_tags(None, ("DJ-mix",)) == ("other", "dj-mix")


def test_parse_rejects_unknown_slugs_and_empty_lists() -> None:
    assert parse_primary_types("album, EP") == ("album", "ep")
    assert parse_secondary_types("Live, live") == ("live",)  # duplicates collapse
    with pytest.raises(SettingsError):
        parse_primary_types("album,longplayer")
    with pytest.raises(SettingsError):
        parse_secondary_types(" ")


def test_parse_settings_json_validates_values_and_mutual_exclusion() -> None:
    override = parse_settings_json(
        '{"allow_primary": ["ep"], "muted": false, "priority": "instant"}'
    )
    assert override == SettingsOverride(allow_primary=("ep",), muted=False, priority="instant")
    with pytest.raises(SettingsError):
        parse_settings_json('{"priority": "urgent"}')
    with pytest.raises(SettingsError):
        parse_settings_json('{"mute_until": "soon"}')
    with pytest.raises(SettingsError):
        parse_settings_json('{"muted": true, "mute_until": "2026-09-01"}')
    with pytest.raises(SettingsError):
        parse_settings_json('{"allow_secondary": ["vinyl"]}')
    # Unknown keys and empty input are tolerated (forward compatibility).
    assert parse_settings_json(None) == SettingsOverride()
    assert parse_settings_json("{}") == SettingsOverride()


def test_canonical_json_round_trips_and_erases_the_empty_layer() -> None:
    override = SettingsOverride(allow_secondary=("remix",), priority=PRIORITY_DIGEST)
    raw = canonical_override_json(override)
    assert raw is not None
    assert '"allow_secondary"' in raw and '"priority"' in raw
    assert parse_settings_json(raw) == override
    assert canonical_override_json(SettingsOverride()) is None


def test_resolution_layers_override_over_defaults_without_mixing_lists() -> None:
    defaults = SettingsOverride(allow_primary=("album",), allow_secondary=("compilation",))
    override = SettingsOverride(allow_primary=("album", "ep"))
    effective = resolve_effective(defaults, override)
    # An artist that sets a primary list replaces it outright; the global
    # secondary default still applies because the artist left it unset.
    assert effective.allow_primary == {"album", "ep"}
    assert effective.allow_secondary == {"compilation"}


def test_mute_and_priority_cannot_be_global_defaults() -> None:
    with pytest.raises(SettingsError):
        resolve_effective(SettingsOverride(muted=True), None)
    with pytest.raises(SettingsError):
        resolve_effective(SettingsOverride(priority=PRIORITY_INSTANT), None)


def test_muting_semantics_including_expired_dates() -> None:
    indefinite = resolve_effective(SettingsOverride(), SettingsOverride(muted=True))
    assert indefinite.is_muted_on(TODAY)
    until = resolve_effective(SettingsOverride(), SettingsOverride(mute_until=dt.date(2026, 8, 23)))
    assert until.is_muted_on(TODAY)
    assert not until.is_muted_on(dt.date(2026, 8, 24))
    lifted = resolve_effective(SettingsOverride(), SettingsOverride(muted=False))
    assert not lifted.is_muted_on(TODAY)
