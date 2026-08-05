"""Shared F4 fixtures: seeding release events, and a sender that never leaves the box."""

from __future__ import annotations

import datetime as dt

from encore.models import Artist, EventView
from encore.notify.render import RenderedNotification
from encore.notify.sender import DeliveryError
from encore.storage import Storage

# Unmistakable needles: nothing else in the schema or fixtures contains them,
# so a privacy test that greps for them cannot pass by accident.
CHANNEL_URL = "ntfy://user:APPRISE-URL-needle-8c41f7@ntfy.example/encore"
ARTIST_NAME = "Sentinel Artist needle-6b2e"
RELEASE_TITLE = "Sentinel Album needle-9d7a"
ARTIST_MBID = "11111111-2222-3333-4444-555555555555"
GROUP_MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
RATING_KEY = "4242"
MACHINE_ID = "machine-id-needle-1a2b"


class RecordingSender:
    """A `NotificationSender` that records calls instead of sending anything."""

    def __init__(self, fail_with: str | None = None, raise_unexpected: bool = False) -> None:
        self.calls: list[tuple[str, RenderedNotification]] = []
        self.fail_with = fail_with
        self.raise_unexpected = raise_unexpected

    def send(self, url: str, notification: RenderedNotification) -> None:
        """Record the call, then fail in whichever way this instance is configured."""
        self.calls.append((url, notification))
        if self.raise_unexpected:
            raise RuntimeError("a plugin exploded in an unexpected way")
        if self.fail_with is not None:
            raise DeliveryError(self.fail_with)


def seed_event(
    storage: Storage,
    kind: str = "new",
    title: str = RELEASE_TITLE,
    artist_name: str = ARTIST_NAME,
    artist_mbid: str = ARTIST_MBID,
    group_mbid: str = GROUP_MBID,
    rating_key: str = RATING_KEY,
    primary_type: str | None = "Album",
    secondary_types: tuple[str, ...] = (),
    first_release_date: str = "2026-08-14",
) -> int:
    """Create an artist, a match, a release-group, and one event; return the event id."""
    with storage.session() as session:
        session.add(Artist(plex_rating_key=rating_key, name=artist_name, library_key="1"))
        session.commit()
    storage.save_artist_match(rating_key, artist_name, "auto", mbid=artist_mbid, confidence=0.99)
    group = storage.add_release_group(
        artist_mbid=artist_mbid,
        mbid=group_mbid,
        title=title,
        primary_type=primary_type,
        secondary_types=secondary_types,
        first_release_date=first_release_date,
    )
    assert group.id is not None
    event = storage.add_event(group.id, kind)
    assert event.id is not None
    return event.id


def make_view(
    kind: str = "new",
    title: str = RELEASE_TITLE,
    artist_name: str = ARTIST_NAME,
    primary_type: str | None = "Album",
    secondary_types: tuple[str, ...] = (),
    first_release_date: str = "2026-08-14",
    plex_rating_key: str | None = RATING_KEY,
    event_id: int = 1,
    group_mbid: str = GROUP_MBID,
) -> EventView:
    """Build an `EventView` directly, for renderer tests that need no database."""
    return EventView(
        event_id=event_id,
        kind=kind,
        created_at=dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC),
        release_group_mbid=group_mbid,
        title=title,
        primary_type=primary_type,
        secondary_types=secondary_types,
        first_release_date=first_release_date,
        artist_mbid=ARTIST_MBID,
        artist_name=artist_name,
        plex_rating_key=plex_rating_key,
    )
