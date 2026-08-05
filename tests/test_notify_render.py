"""F4 rendering tests: every field the acceptance criteria name, and the digest."""

from __future__ import annotations

from encore.notify.render import (
    DIGEST_ITEM_LIMIT,
    cover_art_url,
    plex_artist_url,
    render_digest,
    render_event,
    render_test,
)
from tests.notify_fixtures import ARTIST_NAME, GROUP_MBID, MACHINE_ID, RELEASE_TITLE, make_view


def test_instant_notification_carries_every_acceptance_field() -> None:
    # Roadmap F4 acceptance: artist, title, type, release date, cover art,
    # and a deep link to the artist in Plex.
    rendered = render_event(make_view(), MACHINE_ID)

    assert ARTIST_NAME in rendered.title
    assert RELEASE_TITLE in rendered.title
    assert "Album" in rendered.body
    assert "2026-08-14" in rendered.body
    assert cover_art_url(GROUP_MBID) in rendered.body
    plex_url = plex_artist_url(MACHINE_ID, "4242")
    assert plex_url is not None
    assert plex_url in rendered.body


def test_secondary_types_are_shown_so_a_live_record_is_not_mistaken_for_an_album() -> None:
    rendered = render_event(make_view(primary_type="Album", secondary_types=("Live",)))
    assert "Album (Live)" in rendered.body


def test_partial_release_dates_are_not_padded_in_display_text() -> None:
    # The F3 diff pads a bare year to decide "is this the future?"; display text
    # must not, or we would invent precision MusicBrainz never published.
    assert "2027" in render_event(make_view(first_release_date="2027")).body
    assert "2027-01-01" not in render_event(make_view(first_release_date="2027")).body


def test_missing_metadata_degrades_to_translated_placeholders() -> None:
    rendered = render_event(
        make_view(artist_name="", primary_type=None, first_release_date=""),
    )
    assert "Unknown artist" in rendered.title
    assert "Unknown type" in rendered.body
    assert "date not announced" in rendered.body


def test_kind_chooses_the_subject_line() -> None:
    assert render_event(make_view(kind="new")).title.startswith("New release")
    assert render_event(make_view(kind="upcoming")).title.startswith("Upcoming release")
    assert render_event(make_view(kind="date_changed")).title.startswith("Release date changed")


def test_plex_link_is_omitted_rather_than_guessed() -> None:
    # No machine identifier yet (no sync has run) or no Plex row for the artist:
    # a deep link built from a guess goes nowhere, so there is no line at all.
    assert plex_artist_url(None, "4242") is None
    assert plex_artist_url(MACHINE_ID, None) is None
    assert "app.plex.tv" not in render_event(make_view(), machine_identifier=None).body


def test_plex_link_percent_encodes_the_metadata_key() -> None:
    url = plex_artist_url(MACHINE_ID, "4242")
    assert url is not None
    assert "key=%2Flibrary%2Fmetadata%2F4242" in url


def test_cover_art_is_a_link_not_a_fetch() -> None:
    # docs/adr/0012: the URL is deterministic, so encore emits it without ever
    # contacting the Cover Art Archive itself.
    assert (
        cover_art_url(GROUP_MBID) == f"https://coverartarchive.org/release-group/{GROUP_MBID}/front"
    )


def test_cover_art_can_be_left_out() -> None:
    assert "coverartarchive" not in render_event(make_view(), include_cover_art=False).body


def test_digest_rolls_events_into_one_message() -> None:
    views = [make_view(event_id=i, title=f"Album {i}") for i in range(3)]
    rendered = render_digest(views, MACHINE_ID)

    assert "3 new releases" in rendered.title
    for view in views:
        assert view.title in rendered.body


def test_digest_of_one_renders_as_a_plain_notification() -> None:
    view = make_view()
    assert render_digest([view], MACHINE_ID) == render_event(view, MACHINE_ID)


def test_digest_caps_its_length_and_says_how_many_it_dropped() -> None:
    views = [make_view(event_id=i, title=f"Album {i}") for i in range(DIGEST_ITEM_LIMIT + 4)]
    rendered = render_digest(views)

    assert f"Album {DIGEST_ITEM_LIMIT - 1}" in rendered.body
    assert f"Album {DIGEST_ITEM_LIMIT}" not in rendered.body
    assert "…and 4 more." in rendered.body


def test_test_notification_says_what_it_is() -> None:
    rendered = render_test()
    assert "test notification" in rendered.title
