"""F5 iCal renderer: RFC 5545 shape — escaping, folding, CRLF, stable UIDs."""

from __future__ import annotations

import datetime as dt

from encore.feeds import render_ical
from encore.feeds.ical import MAX_LINE_OCTETS
from encore.models import UpcomingReleaseView
from encore.notify.render import release_type_label

NOW = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.UTC)
GROUP_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def make_release(
    title: str = "Sentinel Album needle-9d7a",
    artist_name: str = "Sentinel Artist needle-6b2e",
    first_release_date: str = "2026-09-15",
    primary_type: str | None = "Album",
    secondary_types: tuple[str, ...] = (),
    group_mbid: str = GROUP_MBID,
) -> UpcomingReleaseView:
    return UpcomingReleaseView(
        release_group_mbid=group_mbid,
        title=title,
        primary_type=primary_type,
        secondary_types=secondary_types,
        first_release_date=first_release_date,
        artist_mbid="11111111-2222-3333-4444-555555555555",
        artist_name=artist_name,
    )


def _unfold(document: str) -> list[str]:
    """Undo RFC 5545 folding: a CRLF followed by one space continues the line."""
    assert document.endswith("\r\n")
    return document.replace("\r\n ", "").rstrip("\r\n").split("\r\n")


def test_empty_calendar_is_valid() -> None:
    lines = _unfold(render_ical([], now=NOW))
    assert lines[0] == "BEGIN:VCALENDAR"
    assert lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert "PRODID:-//encore//release calendar//EN" in lines
    assert "METHOD:PUBLISH" in lines
    assert not any(line.startswith("BEGIN:VEVENT") for line in lines)


def test_event_is_an_all_day_entry_with_a_stable_uid() -> None:
    lines = _unfold(render_ical([make_release()], now=NOW))
    assert "BEGIN:VEVENT" in lines
    assert f"UID:{GROUP_MBID}@encore" in lines
    assert "DTSTAMP:20260801T120000Z" in lines
    assert "DTSTART;VALUE=DATE:20260915" in lines
    assert "TRANSP:TRANSPARENT" in lines
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert "Sentinel Artist needle-6b2e" in summary
    assert "Sentinel Album needle-9d7a" in summary
    assert "(Album)" in summary


def test_a_date_change_moves_the_entry_not_duplicates_it() -> None:
    # Same release-group before and after a date change: the UID is identical,
    # so a subscribed calendar replaces the entry instead of stacking two.
    before = render_ical([make_release(first_release_date="2026-09-15")], now=NOW)
    after = render_ical([make_release(first_release_date="2026-10-01")], now=NOW)
    uid = f"UID:{GROUP_MBID}@encore"
    assert uid in _unfold(before)
    assert uid in _unfold(after)
    assert "DTSTART;VALUE=DATE:20261001" in _unfold(after)


def test_text_values_are_escaped() -> None:
    release = make_release(title="Live; at Home, Vol. 1\\", artist_name="A, B; C")
    lines = _unfold(render_ical([release], now=NOW))
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert "Live\\; at Home\\, Vol. 1\\\\" in summary
    assert "A\\, B\\; C" in summary


def test_physical_lines_fold_at_75_octets() -> None:
    release = make_release(title="An Extremely Long Album Title " * 8)
    document = render_ical([release], now=NOW)
    for line in document.rstrip("\r\n").split("\r\n"):
        assert len(line.encode("utf-8")) <= MAX_LINE_OCTETS
    summary = next(line for line in _unfold(document) if line.startswith("SUMMARY:"))
    assert "An Extremely Long Album Title" in summary


def test_folding_never_splits_a_multibyte_character() -> None:
    release = make_release(title="é" * 120, artist_name="ü" * 40)
    document = render_ical([release], now=NOW)
    for line in document.rstrip("\r\n").split("\r\n"):
        assert len(line.encode("utf-8")) <= MAX_LINE_OCTETS
    # Unfolding must reconstruct the original characters intact.
    summary = next(line for line in _unfold(document) if line.startswith("SUMMARY:"))
    assert "é" * 120 in summary
    assert "ü" * 40 in summary


def test_output_is_deterministic_for_a_fixed_now() -> None:
    releases = [make_release()]
    assert render_ical(releases, now=NOW) == render_ical(releases, now=NOW)


def test_missing_type_renders_a_placeholder_not_a_blank() -> None:
    lines = _unfold(render_ical([make_release(primary_type=None)], now=NOW))
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert "(Unknown type)" in summary


def test_secondary_types_reach_the_calendar_like_they_reach_the_feed() -> None:
    # A live record or an EP must not read as a plain studio album on a
    # calendar while the RSS item beside it says "Album (Live)".
    release = make_release(secondary_types=("Live",))
    lines = _unfold(render_ical([release], now=NOW))
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert "(Album (Live))" in summary
    assert summary.endswith(release_type_label(release.primary_type, release.secondary_types) + ")")
