"""The iCal feed of upcoming release dates (F5) — hand-rolled RFC 5545.

Why hand-rolled: the calendar is a flat list of all-day events, and the three
RFC 5545 rules that matter (text escaping, 75-octet line folding, CRLF) are a
dozen lines that a test can pin exactly — a calendar library would be a new
dependency whose output we would still have to verify against the same rules.

Deliberate shapes:

- **All-day events, day-precision only.** ``DTSTART;VALUE=DATE`` with no
  ``DTEND`` is RFC 5545's one-day event. Which releases qualify is decided in
  `encore.storage.Storage.list_upcoming_releases` (partial dates never become
  calendar entries — no invented precision), not here.
- **``UID`` is the release-group MBID** (``<mbid>@encore``): stable across
  reads and across date changes, so a moved release date *moves* the entry in
  the subscriber's calendar instead of leaving a stale duplicate behind.
- **``TRANSP:TRANSPARENT``**: a release date is information, not an
  appointment — it must never make the user look busy.
- **``DTSTAMP`` is injectable** (``now``): the field is mandatory, but a
  deterministic document is what makes byte-exact tests possible.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence

from encore.i18n import _
from encore.models import UpcomingReleaseView
from encore.notify.render import release_type_label

__all__ = [
    "ICAL_EVENT_LIMIT",
    "MAX_LINE_OCTETS",
    "render_ical",
]

# RFC 5545 §3.1: content lines SHOULD NOT be longer than 75 octets, excluding
# the line break; longer lines are folded with CRLF + one space.
MAX_LINE_OCTETS = 75

# The route serves at most this many entries. Unlike RSS's window of 100, this
# is a ceiling on a pathological library rather than the shape of the feature:
# a calendar that silently forgot next month's release would be a bug, and the
# query is already bounded by reality (only day-precision, future-dated
# announcements for artists still in the Plex library qualify). Entries arrive
# soonest-first, so a truncation drops the most distant announcements — the
# ones most likely to move before they matter — and never the imminent ones.
ICAL_EVENT_LIMIT = 500

# RFC 5545 §3.3.11: the TEXT production admits WSP (space and HTAB) but no
# other control character — CR and LF exist in a value only as the "\n"
# escape, and DEL sits outside every TSAFE-CHAR range. Anything still in this
# set after escaping is dropped rather than passed through.
_FORBIDDEN_CONTROLS = str.maketrans(
    dict.fromkeys([chr(code) for code in range(0x20) if code != 0x09] + ["\x7f"], None)
)


def _escape(text: str) -> str:
    r"""Escape a TEXT property value per RFC 5545 §3.3.11 (backslash first).

    Line breaks are normalized before escaping: a CRLF is one break, and so is
    a lone CR — Python's own `str.splitlines`, and every parser that agrees
    with it, would otherwise read a raw CR from an upstream MusicBrainz title
    as the end of the content line and the start of an injected one
    (``Evil\r\nSUMMARY:Injected`` is exactly that attack). Every remaining
    disallowed control character is then removed, so no value this function
    returns can carry a byte the TEXT production forbids.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    escaped = (
        normalized.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )
    return escaped.translate(_FORBIDDEN_CONTROLS)


def _fold(line: str) -> Iterator[str]:
    """Fold one logical line into physical lines of at most 75 octets.

    Splits are measured (and made) in UTF-8 octets but never inside a
    multi-byte character; continuation lines start with one space, which
    itself counts toward the 75.
    """
    encoded = line.encode("utf-8")
    prefix = b""
    while len(prefix) + len(encoded) > MAX_LINE_OCTETS:
        split = MAX_LINE_OCTETS - len(prefix)
        # Do not split inside a UTF-8 sequence: back off past continuation
        # bytes (0b10xxxxxx) to the previous character boundary.
        while (encoded[split] & 0xC0) == 0x80:
            split -= 1
        yield (prefix + encoded[:split]).decode("utf-8")
        encoded = encoded[split:]
        prefix = b" "
    yield (prefix + encoded).decode("utf-8")


def _summary(release: UpcomingReleaseView) -> str:
    """One line for the calendar entry: artist, title, and the release type.

    The type label is F4's, secondary types included: a live record or an EP
    reads the same on the calendar as it does in the feed beside it.
    """
    fields = {
        "artist": release.artist_name or _("Unknown artist"),
        "title": release.title,
        "type": release_type_label(release.primary_type, release.secondary_types),
    }
    return _("%(artist)s — %(title)s (%(type)s)") % fields


def render_ical(
    releases: Sequence[UpcomingReleaseView],
    now: dt.datetime | None = None,
) -> str:
    """Render upcoming releases as a VCALENDAR document (CRLF line endings).

    ``releases`` must carry day-precision ``first_release_date`` values (the
    storage query guarantees it); an empty sequence renders a valid, empty
    calendar. ``now`` stamps ``DTSTAMP`` and defaults to the real clock.
    """
    if now is None:
        now = dt.datetime.now(dt.UTC)
    dtstamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    logical_lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//encore//release calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + _escape(_("encore — upcoming releases")),
    ]
    for release in releases:
        day = dt.date.fromisoformat(release.first_release_date)
        logical_lines += [
            "BEGIN:VEVENT",
            # UID is a TEXT value too. A MusicBrainz release-group id is a
            # UUID, so escaping is a no-op on every real one — which is the
            # point: no stored string reaches a content line unescaped, so the
            # invariant holds without depending on an upstream id's shape.
            "UID:" + _escape(release.release_group_mbid) + "@encore",
            f"DTSTAMP:{dtstamp}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            "SUMMARY:" + _escape(_summary(release)),
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    logical_lines.append("END:VCALENDAR")
    physical_lines: list[str] = []
    for line in logical_lines:
        physical_lines.extend(_fold(line))
    return "\r\n".join(physical_lines) + "\r\n"
