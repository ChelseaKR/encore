"""Fixture library of known-nasty artist-matching cases (F2 acceptance).

Each case pairs a Plex-side artist (name + optional hints) with a synthetic
MusicBrainz-search-shaped JSON payload and the expected terminal state:
``auto`` with a specific MBID, or ``pending`` (review queue). The nasty
shapes the roadmap names are all here: homonyms, unicode/diacritics,
one-album artists, aliases, typos, tribute-band traps, empty results.

**Honesty note:** these payloads are constructed to the WS/2 response shape,
not recorded from live MusicBrainz. They gate the ≥95% fixture-precision
half of F2's acceptance criterion; the ≥90% field-rate half requires the
validation spike on a real library (roadmap U8) and is NOT covered here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from encore.matching.scoring import ArtistHints


def mb_artist(
    mbid: str,
    name: str,
    score: int = 100,
    artist_type: str | None = None,
    country: str | None = None,
    disambiguation: str | None = None,
    aliases: tuple[str, ...] = (),
) -> dict[str, object]:
    """One artist object in MusicBrainz WS/2 search-response shape."""
    return {
        "id": mbid,
        "name": name,
        "sort-name": name,
        "score": score,
        "type": artist_type,
        "country": country,
        "disambiguation": disambiguation,
        "aliases": [{"name": alias} for alias in aliases],
    }


def mb_search_response(*artists: dict[str, object]) -> dict[str, object]:
    """Build a full WS/2 search payload wrapping the given artist objects."""
    return {
        "created": "2026-07-17T00:00:00Z",
        "count": len(artists),
        "offset": 0,
        "artists": list(artists),
    }


@dataclass(frozen=True)
class MatchCase:
    """One fixture-library case: input hints, MB payload, expected outcome."""

    case_id: str
    hints: ArtistHints
    response: dict[str, object] = field(default_factory=lambda: mb_search_response())
    expected_status: str = "auto"
    expected_mbid: str | None = None


MATCH_CASES: tuple[MatchCase, ...] = (
    MatchCase(
        case_id="exact-single",
        hints=ArtistHints(name="Radiohead"),
        response=mb_search_response(mb_artist("mb-radiohead", "Radiohead", 100, "Group", "GB")),
        expected_mbid="mb-radiohead",
    ),
    MatchCase(
        case_id="diacritics-exact",
        hints=ArtistHints(name="Björk"),
        response=mb_search_response(mb_artist("mb-bjork", "Björk", 100, "Person", "IS")),
        expected_mbid="mb-bjork",
    ),
    MatchCase(
        case_id="plex-missing-diacritics",
        hints=ArtistHints(name="Motorhead"),
        response=mb_search_response(mb_artist("mb-motorhead", "Motörhead", 100, "Group", "GB")),
        expected_mbid="mb-motorhead",
    ),
    MatchCase(
        case_id="homonym-no-hints-goes-to-review",
        hints=ArtistHints(name="Bush"),
        response=mb_search_response(
            mb_artist("mb-bush-gb", "Bush", 100, "Group", "GB", "British grunge band"),
            mb_artist("mb-bush-ca", "Bush", 92, "Group", "CA", "Canadian rock band"),
        ),
        expected_status="pending",
    ),
    MatchCase(
        case_id="homonym-country-hint-disambiguates",
        hints=ArtistHints(name="Bush", country_hint="GB"),
        response=mb_search_response(
            mb_artist("mb-bush-gb", "Bush", 100, "Group", "GB", "British grunge band"),
            mb_artist("mb-bush-ca", "Bush", 92, "Group", "CA", "Canadian rock band"),
        ),
        expected_mbid="mb-bush-gb",
    ),
    MatchCase(
        case_id="homonym-guid-boost-disambiguates",
        hints=ArtistHints(name="Nirvana", guid_mbid="mb-nirvana-us"),
        response=mb_search_response(
            mb_artist("mb-nirvana-us", "Nirvana", 100, "Group", "US", "90s US grunge"),
            mb_artist("mb-nirvana-gb", "Nirvana", 95, "Group", "GB", "60s UK psych"),
        ),
        expected_mbid="mb-nirvana-us",
    ),
    MatchCase(
        case_id="homonym-without-guid-goes-to-review",
        hints=ArtistHints(name="Nirvana"),
        response=mb_search_response(
            mb_artist("mb-nirvana-us", "Nirvana", 100, "Group", "US", "90s US grunge"),
            mb_artist("mb-nirvana-gb", "Nirvana", 95, "Group", "GB", "60s UK psych"),
        ),
        expected_status="pending",
    ),
    MatchCase(
        case_id="guid-cannot-rescue-name-mismatch",
        hints=ArtistHints(name="Radiohead", guid_mbid="mb-coldplay"),
        response=mb_search_response(mb_artist("mb-coldplay", "Coldplay", 100, "Group", "GB")),
        expected_status="pending",
    ),
    MatchCase(
        case_id="alias-exact",
        hints=ArtistHints(name="Florence + The Machine"),
        response=mb_search_response(
            mb_artist(
                "mb-florence",
                "Florence and the Machine",
                100,
                "Group",
                "GB",
                aliases=("Florence + the Machine",),
            )
        ),
        expected_mbid="mb-florence",
    ),
    MatchCase(
        case_id="one-album-artist-low-mb-score",
        hints=ArtistHints(name="Cindytalk"),
        response=mb_search_response(mb_artist("mb-cindytalk", "Cindytalk", 42, "Group", "GB")),
        expected_mbid="mb-cindytalk",
    ),
    MatchCase(
        case_id="no-results-goes-to-review",
        hints=ArtistHints(name="Chelsea Garage Demos 2019"),
        response=mb_search_response(),
        expected_status="pending",
    ),
    MatchCase(
        case_id="case-insensitive",
        hints=ArtistHints(name="the beatles"),
        response=mb_search_response(mb_artist("mb-beatles", "The Beatles", 100, "Group", "GB")),
        expected_mbid="mb-beatles",
    ),
    MatchCase(
        case_id="punctuation-slash",
        hints=ArtistHints(name="AC/DC"),
        response=mb_search_response(mb_artist("mb-acdc", "AC/DC", 100, "Group", "AU")),
        expected_mbid="mb-acdc",
    ),
    MatchCase(
        case_id="ampersand-vs-and",
        hints=ArtistHints(name="Simon & Garfunkel"),
        response=mb_search_response(
            mb_artist("mb-simon", "Simon and Garfunkel", 100, "Group", "US")
        ),
        expected_mbid="mb-simon",
    ),
    MatchCase(
        case_id="non-latin-script",
        hints=ArtistHints(name="Мумий Тролль"),
        response=mb_search_response(mb_artist("mb-mumiy", "Мумий Тролль", 100, "Group", "RU")),
        expected_mbid="mb-mumiy",
    ),
    MatchCase(
        case_id="typo-goes-to-review",
        hints=ArtistHints(name="Radiohed"),
        response=mb_search_response(mb_artist("mb-radiohead", "Radiohead", 100, "Group", "GB")),
        expected_status="pending",
    ),
    MatchCase(
        case_id="tribute-band-trap",
        hints=ArtistHints(name="Oasis"),
        response=mb_search_response(
            mb_artist("mb-oasis", "Oasis", 100, "Group", "GB"),
            mb_artist("mb-oasis-tribute", "Oasis Tribute", 45, "Group", "GB", "tribute band"),
        ),
        expected_mbid="mb-oasis",
    ),
    MatchCase(
        case_id="diacritics-exact-latin-small",
        hints=ArtistHints(name="Sigur Rós"),
        response=mb_search_response(mb_artist("mb-sigurros", "Sigur Rós", 100, "Group", "IS")),
        expected_mbid="mb-sigurros",
    ),
    MatchCase(
        case_id="contradicting-hint-strong-name-still-wins",
        hints=ArtistHints(name="Adele", country_hint="US"),
        response=mb_search_response(mb_artist("mb-adele", "Adele", 100, "Person", "GB")),
        expected_mbid="mb-adele",
    ),
    MatchCase(
        case_id="type-hint-disambiguates",
        hints=ArtistHints(name="Boston", type_hint="Group"),
        response=mb_search_response(
            mb_artist("mb-boston-band", "Boston", 100, "Group", "US"),
            mb_artist("mb-boston-person", "Boston", 90, "Person", "US"),
        ),
        expected_mbid="mb-boston-band",
    ),
    MatchCase(
        case_id="near-tie-both-plausible-goes-to-review",
        hints=ArtistHints(name="John Williams"),
        response=mb_search_response(
            mb_artist("mb-jw-composer", "John Williams", 100, "Person", "US", "film composer"),
            mb_artist(
                "mb-jw-guitarist", "John Williams", 97, "Person", "AU", "classical guitarist"
            ),
        ),
        expected_status="pending",
    ),
    MatchCase(
        case_id="stylized-alias",
        hints=ArtistHints(name="P!nk"),
        response=mb_search_response(
            mb_artist("mb-pink", "Pink", 100, "Person", "US", aliases=("P!nk",))
        ),
        expected_mbid="mb-pink",
    ),
)
