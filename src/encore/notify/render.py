"""F4 rendering: an `EventView` becomes the text a human actually reads.

What a notification must carry (roadmap F4 acceptance): artist, title, type,
release date, cover art, and a deep link to the artist in Plex. Each has a
deliberate shape here:

- **Type** joins MusicBrainz's primary and secondary types ("Album (Live)")
  so an EP or a live record is never mistaken for a new studio album.
- **Release date** is MusicBrainz's partial date verbatim (``2027``,
  ``2027-03``, ``2027-03-14``). Padding a bare year to January 1st in *display*
  text would invent precision MusicBrainz did not publish; the padding that
  does happen is confined to the F3 diff's is-this-future test.
- **Cover art** is the Cover Art Archive's deterministic release-group URL.
  It is emitted as a link in the body, **not** as an Apprise attachment: an
  attachment makes *encore's own host* fetch the image, which would add an
  undisclosed outbound flow (docs/adr/0012, docs/audits/dpia.md §3). As a link,
  the fetch — if any — is the notification service's or the reader's. The URL
  404s when the archive has no art for that group; verifying it in advance
  would cost a request per event and is deliberately not done.
- **Plex deep link** needs the server's machine identifier, learned read-only
  during a sync. Before a sync has run (or for an event whose artist has been
  re-matched away from its Plex row) the line is simply omitted — an
  ``app.plex.tv`` URL built from a guessed identifier goes nowhere.

Every user-facing string routes through `encore.i18n` with **named**
placeholders (docs/I18N.md). These are the project's first display strings, so
this module is where the gettext seam earns its keep.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from encore.i18n import _, _n
from encore.models import EventView

__all__ = [
    "COVER_ART_BASE_URL",
    "DIGEST_ITEM_LIMIT",
    "PLEX_APP_BASE_URL",
    "RenderedNotification",
    "cover_art_url",
    "plex_artist_url",
    "release_type_label",
    "render_digest",
    "render_event",
    "render_test",
]

COVER_ART_BASE_URL = "https://coverartarchive.org"
PLEX_APP_BASE_URL = "https://app.plex.tv/desktop"

# A digest names at most this many releases and then says how many more there
# are. A 1,000-artist library that has been offline for a week must produce a
# readable message, not a wall the user scrolls past (F4's signal-to-noise
# obligation; the per-artist filters that shrink the input are F10 at M3).
DIGEST_ITEM_LIMIT = 25


@dataclass(frozen=True)
class RenderedNotification:
    """The service-agnostic message an Apprise channel is asked to deliver."""

    title: str
    body: str


def cover_art_url(release_group_mbid: str) -> str:
    """Build the Cover Art Archive front-cover URL for a release-group MBID."""
    return f"{COVER_ART_BASE_URL}/release-group/{quote(release_group_mbid)}/front"


def plex_artist_url(machine_identifier: str | None, plex_rating_key: str | None) -> str | None:
    """Build the ``app.plex.tv`` deep link for an artist, or ``None`` if unbuildable."""
    if not machine_identifier or not plex_rating_key:
        return None
    key = quote(f"/library/metadata/{plex_rating_key}", safe="")
    return f"{PLEX_APP_BASE_URL}/#!/server/{quote(machine_identifier)}/details?key={key}"


def release_type_label(primary_type: str | None, secondary_types: Sequence[str]) -> str:
    """Render MusicBrainz's type pair: ``Album``, ``Album (Live)``, or a placeholder.

    Public because F5's calendar renders the same label from an
    `UpcomingReleaseView`: an EP or a live record must not read as a plain
    studio album on a calendar when it reads as "Album (Live)" in the feed
    next to it.
    """
    primary = primary_type or _("Unknown type")
    if secondary_types:
        return f"{primary} ({', '.join(secondary_types)})"
    return primary


def _release_type(view: EventView) -> str:
    """Render an event's release type."""
    return release_type_label(view.primary_type, view.secondary_types)


def _release_date(view: EventView) -> str:
    """MusicBrainz's partial date verbatim, or a translated "not announced"."""
    return view.first_release_date or _("date not announced")


def _artist_name(view: EventView) -> str:
    """Return the artist's display name, or a translated placeholder."""
    return view.artist_name or _("Unknown artist")


def _title_for(view: EventView) -> str:
    """Build the one-line subject, chosen by event kind."""
    fields = {"artist": _artist_name(view), "title": view.title}
    if view.kind == "upcoming":
        return _("Upcoming release: %(artist)s — %(title)s") % fields
    if view.kind == "date_changed":
        return _("Release date changed: %(artist)s — %(title)s") % fields
    return _("New release: %(artist)s — %(title)s") % fields


def render_event(
    view: EventView,
    machine_identifier: str | None = None,
    include_cover_art: bool = True,
) -> RenderedNotification:
    """Render one event as a standalone (instant-mode) notification."""
    lines = [
        _("Type: %(type)s") % {"type": _release_type(view)},
        _("Release date: %(date)s") % {"date": _release_date(view)},
    ]
    plex_url = plex_artist_url(machine_identifier, view.plex_rating_key)
    if plex_url is not None:
        lines.append(_("Open in Plex: %(url)s") % {"url": plex_url})
    if include_cover_art:
        lines.append(_("Cover art: %(url)s") % {"url": cover_art_url(view.release_group_mbid)})
    return RenderedNotification(title=_title_for(view), body="\n".join(lines))


def render_digest(
    views: list[EventView],
    machine_identifier: str | None = None,
    include_cover_art: bool = True,
) -> RenderedNotification:
    """Roll several events into one digest message.

    A one-item digest renders exactly like an instant notification: a rollup
    of one is a notification, and dressing it up as a digest would be noise.
    """
    if len(views) == 1:
        return render_event(views[0], machine_identifier, include_cover_art)
    title = _n(
        "%(count)d new release for your library",
        "%(count)d new releases for your library",
        len(views),
    ) % {"count": len(views)}
    lines: list[str] = []
    for view in views[:DIGEST_ITEM_LIMIT]:
        lines.append(
            _("%(artist)s — %(title)s (%(type)s, %(date)s)")
            % {
                "artist": _artist_name(view),
                "title": view.title,
                "type": _release_type(view),
                "date": _release_date(view),
            }
        )
        plex_url = plex_artist_url(machine_identifier, view.plex_rating_key)
        if plex_url is not None:
            lines.append(f"  {plex_url}")
        if include_cover_art:
            lines.append(f"  {cover_art_url(view.release_group_mbid)}")
    remaining = len(views) - DIGEST_ITEM_LIMIT
    if remaining > 0:
        lines.append(
            _n("…and %(count)d more.", "…and %(count)d more.", remaining) % {"count": remaining}
        )
    return RenderedNotification(title=title, body="\n".join(lines))


def render_test() -> RenderedNotification:
    """Build the message `encore channels test` (and the F6 wizard) fires."""
    return RenderedNotification(
        title=_("encore test notification"),
        body=_(
            "If you are reading this, encore can reach this channel. "
            "Release alerts will arrive here."
        ),
    )
