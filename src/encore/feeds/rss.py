"""The RSS 2.0 feed of release events (F5).

Deliberate shapes:

- **Items reuse the F4 renderer.** An RSS item and an instant notification
  answer the same question ("what happened?"), so the item title and body are
  exactly `encore.notify.render.render_event`'s output — one translated,
  tested rendering instead of two drifting ones. Cover art stays a link in
  the body for the same reason it is not an Apprise attachment: encore's own
  host must not fetch it (docs/adr/0012).
- **`<guid>` is the event id** (``encore:event:<id>``, ``isPermaLink=false``):
  stable across reads, so a reader never re-surfaces an item it has seen, and
  a ``date_changed`` event — a genuinely new fact about a seen release — is a
  new item rather than a silent edit.
- **`<link>` points at MusicBrainz's public release-group page**: a real
  destination that exists for every item and carries no reader-identifying
  or Plex-identifying material.
- **The token appears nowhere in the document.** The reader already holds the
  URL; echoing the capability into the body would only widen where it lands.
- Built with ElementTree, so well-formedness and escaping are the XML
  library's guarantee, not a template's luck.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from email.utils import format_datetime
from urllib.parse import quote
from xml.etree import ElementTree as ET

from encore.i18n import _
from encore.models import EventView
from encore.notify.render import render_event

__all__ = [
    "MUSICBRAINZ_RELEASE_GROUP_URL",
    "RSS_EVENT_LIMIT",
    "render_rss",
]

MUSICBRAINZ_RELEASE_GROUP_URL = "https://musicbrainz.org/release-group"

# The routes serve at most this many of the newest events. A feed is a window,
# not an archive: readers poll, and a multi-year backlog in one document helps
# no one while growing without bound.
RSS_EVENT_LIMIT = 100

# The channel link must be a real page (RSS 2.0 requires one); the project
# home is the only stable, non-taste-bearing page encore has.
_CHANNEL_LINK = "https://github.com/ChelseaKR/encore"


def _rfc822(created_at: dt.datetime) -> str:
    """Format a stored UTC timestamp as an RFC 822 ``pubDate``.

    SQLite hands timestamps back naive; they are UTC by construction
    (`encore.models.utcnow`), so the timezone is restored, never guessed.
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.UTC)
    return format_datetime(created_at, usegmt=True)


def render_rss(
    views: Sequence[EventView],
    machine_identifier: str | None = None,
) -> str:
    """Render release events as an RSS 2.0 document (newest first).

    ``views`` is expected newest-first, as `Storage.list_event_views`
    returns; an empty sequence renders a valid, empty channel.
    """
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = _("encore — release feed")
    ET.SubElement(channel, "link").text = _CHANNEL_LINK
    ET.SubElement(channel, "description").text = _(
        "New and upcoming releases for artists in your Plex music library"
    )
    if views:
        ET.SubElement(channel, "lastBuildDate").text = _rfc822(views[0].created_at)
    for view in views:
        rendered = render_event(view, machine_identifier)
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = rendered.title
        ET.SubElement(
            item, "link"
        ).text = f"{MUSICBRAINZ_RELEASE_GROUP_URL}/{quote(view.release_group_mbid)}"
        ET.SubElement(item, "description").text = rendered.body
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = f"encore:event:{view.event_id}"
        ET.SubElement(item, "pubDate").text = _rfc822(view.created_at)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True)
