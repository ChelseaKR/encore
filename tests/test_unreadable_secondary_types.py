"""A release-group whose secondary types cannot be read is not a typeless one.

`ReleaseGroup.secondary_types_json` was read at four places with three
behaviours. Channel routing (`Storage._routable_event`) and `encore settings
simulate` caught `json.JSONDecodeError` and answered `()`. The
upcoming-releases query and the events view called `json.loads` unguarded, so
they raised on a blob the other two tolerated.

The `()` answer is the one with teeth. `ArtistWatchSettings.passes` checks
`all(tag in allow_secondary for tag in tags[1:])`, and `all(...)` over an empty
sequence is `True` — so a group with no secondary types clears the secondary
half of the filter on its primary type alone. A live album, a compilation or a
remix whose stored types could not be read therefore passed a filter the user
had set to keep exactly that out. A failed read was granting permission.

`artistsettings.group_type_tags` already states the project's posture on a type
it cannot validate:

    Unknown secondary tags survive as opaque slugs — they will never sit in a
    validated allowlist, which is exactly how an unrecognized future type stays
    conservative.

An unreadable column is that situation with less information, so it takes the
same path: `stored_secondary_types` answers with a slug no allowlist can
contain. These tests hold both halves — the unreadable case fails closed, and
the genuinely-empty case is still empty, because turning a real finding into a
suppression would be the same defect pointed the other way.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from encore.artistsettings import (
    DEFAULT_ALLOWED_PRIMARY,
    SECONDARY_TYPE_SLUGS,
    UNREADABLE_SECONDARY_TYPES,
    ArtistWatchSettings,
    SettingsError,
    group_type_tags,
    parse_secondary_types,
    stored_secondary_types,
)
from encore.models import Artist
from encore.storage import Storage

TODAY = dt.date(2026, 8, 1)
ARTIST_MBID = "11111111-2222-3333-4444-555555555555"
ARTIST_NAME = "Sentinel Artist needle-4f1c"
GROUP_MBID = "aaaaaaaa-0000-0000-0000-00000000f00d"


class TestTheReader:
    def test_absent_and_empty_are_no_secondary_types(self) -> None:
        assert stored_secondary_types(None) == ()
        assert stored_secondary_types("") == ()
        assert stored_secondary_types("   ") == ()

    def test_a_stored_empty_list_is_a_finding_not_a_failure(self) -> None:
        # The boundary. `[]` is what a studio album with no secondary types
        # records. Calling it unreadable would suppress every ordinary album.
        assert stored_secondary_types("[]") == ()

    def test_a_readable_list_reads_as_itself(self) -> None:
        assert stored_secondary_types('["Live", "Compilation"]') == ("Live", "Compilation")

    def test_a_corrupt_blob_is_not_an_empty_one(self) -> None:
        assert stored_secondary_types('["Live"') == (UNREADABLE_SECONDARY_TYPES,)

    def test_a_blob_of_the_wrong_shape_is_not_an_empty_one(self) -> None:
        assert stored_secondary_types('{"types": ["Live"]}') == (UNREADABLE_SECONDARY_TYPES,)
        assert stored_secondary_types('"Live"') == (UNREADABLE_SECONDARY_TYPES,)


class TestItFailsClosed:
    def test_the_slug_is_not_a_real_secondary_type(self) -> None:
        assert UNREADABLE_SECONDARY_TYPES not in SECONDARY_TYPE_SLUGS

    def test_the_slug_cannot_be_put_in_an_allowlist(self) -> None:
        # If a user could opt in to it, the guard would be optional. They cannot.
        with pytest.raises(SettingsError):
            parse_secondary_types(UNREADABLE_SECONDARY_TYPES)

    def test_the_slug_survives_tagging_intact(self) -> None:
        tags = group_type_tags("Album", stored_secondary_types('["Live"'))
        assert tags == ("album", UNREADABLE_SECONDARY_TYPES)

    def test_an_unreadable_type_set_never_clears_the_filter(self) -> None:
        policy = ArtistWatchSettings(
            allow_primary=frozenset(DEFAULT_ALLOWED_PRIMARY),
            allow_secondary=frozenset({"live", "compilation", "remix", "soundtrack"}),
        )
        # An ordinary album still passes...
        assert policy.passes("Album", stored_secondary_types("[]")) is True
        # ...and one whose types could not be read does not, however permissive
        # the allowlist is. There is no allowlist that admits it.
        assert policy.passes("Album", stored_secondary_types('["Live"')) is False

    def test_a_permissive_allowlist_still_does_not_admit_it(self) -> None:
        policy = ArtistWatchSettings(
            allow_primary=frozenset(DEFAULT_ALLOWED_PRIMARY),
            allow_secondary=frozenset(SECONDARY_TYPE_SLUGS),
        )
        assert policy.passes("Album", stored_secondary_types('["Live"')) is False


def _seed(storage: Storage, secondary_types: tuple[str, ...] = ()) -> None:
    with storage.session() as session:
        session.add(Artist(plex_rating_key="4242", name=ARTIST_NAME, library_key="1"))
        session.commit()
    storage.save_artist_match("4242", ARTIST_NAME, "auto", mbid=ARTIST_MBID, confidence=0.99)
    storage.add_release_group(
        artist_mbid=ARTIST_MBID,
        mbid=GROUP_MBID,
        title="Sentinel Album needle-9d7a",
        primary_type="Album",
        secondary_types=secondary_types,
        first_release_date="2026-09-15",
    )


def _corrupt_the_column(storage: Storage, blob: str) -> None:
    """Write a blob straight into the column, past the writer that made it."""
    with storage.engine.connect() as connection:
        connection.exec_driver_sql(
            "UPDATE release_groups SET secondary_types_json = ? WHERE mbid = ?",
            (blob, GROUP_MBID),
        )
        connection.commit()


class TestTheStoragePathsAgree:
    def test_the_upcoming_view_no_longer_raises_on_an_unreadable_blob(self, tmp_path: Path) -> None:
        """This query called `json.loads` unguarded.

        `encore feeds` and the iCal calendar are built on it, so a single
        corrupt row took the whole calendar down with a `JSONDecodeError` that
        named no artist and no release.
        """
        storage = Storage(tmp_path)
        try:
            _seed(storage)
            _corrupt_the_column(storage, '["Live"')
            upcoming = storage.list_upcoming_releases(today=TODAY)
        finally:
            storage.close()
        # It does not raise, and the group is withheld rather than announced as
        # if it had no secondary types.
        assert upcoming == []

    def test_a_readable_group_still_reaches_the_upcoming_view(self, tmp_path: Path) -> None:
        # The control for the test above: withholding has to be caused by the
        # unreadable column, not by the fixture failing to produce a release.
        storage = Storage(tmp_path)
        try:
            _seed(storage)
            upcoming = storage.list_upcoming_releases(today=TODAY)
        finally:
            storage.close()
        assert [release.release_group_mbid for release in upcoming] == [GROUP_MBID]

    def test_the_events_view_names_the_unreadable_column(self, tmp_path: Path) -> None:
        """`encore events` also called `json.loads` unguarded.

        Unlike the upcoming view this is a listing, not a filtered feed, so the
        right answer is to show the operator that the column could not be read
        rather than to hide the row.
        """
        storage = Storage(tmp_path)
        try:
            _seed(storage)
            group = next(row for row in storage.all_release_groups() if row.mbid == GROUP_MBID)
            assert group.id is not None
            storage.add_event(group.id, "new")
            _corrupt_the_column(storage, '["Live"')
            views = storage.list_event_views()
        finally:
            storage.close()
        assert len(views) == 1
        assert views[0].secondary_types == (UNREADABLE_SECONDARY_TYPES,)
