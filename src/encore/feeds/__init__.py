"""F5 standing feeds: RSS for release events, iCal for upcoming release dates.

Both renderers are pure functions from read models to text — no network, no
database, no clock reads beyond the arguments — so every property the feature
promises (well-formed XML, RFC 5545 folding, no invented dates) is provable
offline. Access control lives in `encore.app`: the feed routes are gated by
the capability token `encore.storage.Storage` mints and stores encrypted
(docs/adr/0008), because a feed URL *is* the taste feed (dpia.md §4).
"""

from encore.feeds.ical import render_ical
from encore.feeds.rss import RSS_EVENT_LIMIT, render_rss

__all__ = [
    "RSS_EVENT_LIMIT",
    "render_ical",
    "render_rss",
]
