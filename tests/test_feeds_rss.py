"""F5 RSS renderer: well-formed RSS 2.0 sharing F4's rendering, stable guids."""

from __future__ import annotations

import dataclasses
import datetime as dt
from xml.etree import ElementTree as ET

from encore.feeds import render_rss
from encore.feeds.rss import MUSICBRAINZ_RELEASE_GROUP_URL
from encore.notify.render import render_event
from tests.notify_fixtures import GROUP_MBID, MACHINE_ID, make_view


def _channel(document: str) -> ET.Element:
    root = ET.fromstring(document)  # noqa: S314 - our own renderer's in-process output, not untrusted input
    assert root.tag == "rss"
    assert root.get("version") == "2.0"
    channel = root.find("channel")
    assert channel is not None
    return channel


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    assert child is not None and child.text is not None
    return child.text


def test_empty_feed_is_a_valid_channel() -> None:
    channel = _channel(render_rss([]))
    assert _text(channel, "title")
    assert _text(channel, "link").startswith("https://")
    assert _text(channel, "description")
    assert channel.findall("item") == []
    assert channel.find("lastBuildDate") is None


def test_item_reuses_the_notification_rendering() -> None:
    view = make_view()
    channel = _channel(render_rss([view], MACHINE_ID))
    (item,) = channel.findall("item")
    rendered = render_event(view, MACHINE_ID)
    assert _text(item, "title") == rendered.title
    assert _text(item, "description") == rendered.body
    assert "app.plex.tv" in _text(item, "description")
    assert _text(item, "link") == f"{MUSICBRAINZ_RELEASE_GROUP_URL}/{GROUP_MBID}"


def test_guid_is_the_stable_event_id_not_a_permalink() -> None:
    channel = _channel(render_rss([make_view(event_id=77)]))
    (item,) = channel.findall("item")
    guid = item.find("guid")
    assert guid is not None
    assert guid.text == "encore:event:77"
    assert guid.get("isPermaLink") == "false"


def test_pubdate_is_rfc822_and_lastbuilddate_tracks_the_newest() -> None:
    newest = make_view(event_id=2)
    older = dataclasses.replace(
        make_view(event_id=1), created_at=dt.datetime(2026, 7, 1, 9, 30, tzinfo=dt.UTC)
    )
    channel = _channel(render_rss([newest, older]))
    assert _text(channel, "lastBuildDate") == "Sat, 01 Aug 2026 12:00:00 GMT"
    items = channel.findall("item")
    assert _text(items[1], "pubDate") == "Wed, 01 Jul 2026 09:30:00 GMT"


def test_naive_timestamps_are_read_as_utc() -> None:
    # SQLite hands timestamps back naive; they are UTC by construction.
    naive = dataclasses.replace(make_view(), created_at=dt.datetime(2026, 8, 1, 12, 0))
    channel = _channel(render_rss([naive]))
    (item,) = channel.findall("item")
    assert _text(item, "pubDate") == "Sat, 01 Aug 2026 12:00:00 GMT"


def test_xml_special_characters_survive_a_parse_roundtrip() -> None:
    view = make_view(
        title='<Best> "Album" & Friends',
        artist_name="Simon & Garfunkel <needle>",
    )
    channel = _channel(render_rss([view]))
    (item,) = channel.findall("item")
    assert '<Best> "Album" & Friends' in _text(item, "title")
    assert "Simon & Garfunkel <needle>" in _text(item, "title")


def test_document_declares_itself_as_xml() -> None:
    assert render_rss([]).startswith("<?xml")
