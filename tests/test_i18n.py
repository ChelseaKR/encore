"""I18N seam tests (docs/I18N.md): the pseudolocale proves strings really route.

Encore ships no second catalog, so "the seam exists" is otherwise unfalsifiable
— an English-returning `_()` looks identical whether it consults a catalog or
returns its argument. These tests compile a pseudolocale catalog at runtime and
assert the notification renderer picks it up.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo

from encore import i18n
from encore.notify.render import render_event, render_test
from tests.notify_fixtures import make_view

PSEUDO_LOCALE = "xx"


@pytest.fixture(name="pseudolocale")
def pseudolocale_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Compile a pseudolocale catalog and point the seam at it."""
    catalog = Catalog(locale="en")  # plural rules are irrelevant to these strings
    catalog.add(
        "New release: %(artist)s — %(title)s",
        "⟦New release: %(artist)s — %(title)s⟧",
    )
    catalog.add("Type: %(type)s", "⟦Type: %(type)s⟧")
    catalog.add("encore test notification", "⟦encore test notification⟧")

    mo_dir = tmp_path / PSEUDO_LOCALE / "LC_MESSAGES"
    mo_dir.mkdir(parents=True)
    with (mo_dir / f"{i18n.DOMAIN}.mo").open("wb") as handle:
        write_mo(handle, catalog)

    monkeypatch.setenv(i18n.LOCALE_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(i18n.LOCALE_ENV, PSEUDO_LOCALE)
    i18n.reset_translations()
    yield tmp_path
    i18n.reset_translations()


def test_notification_strings_route_through_the_catalog(pseudolocale: Path) -> None:
    rendered = render_event(make_view())
    assert rendered.title.startswith("⟦New release:")
    assert "⟦Type: Album⟧" in rendered.body
    assert render_test().title == "⟦encore test notification⟧"


def test_placeholders_survive_translation(pseudolocale: Path) -> None:
    # A translator must be able to reorder named placeholders; the interpolation
    # happens after translation, so the values still land.
    rendered = render_event(make_view(artist_name="Boards of Canada", title="Geogaddi"))
    assert "Boards of Canada" in rendered.title
    assert "Geogaddi" in rendered.title


def test_an_untranslated_string_falls_back_to_english(pseudolocale: Path) -> None:
    # "Release date: …" is deliberately absent from the pseudo catalog.
    assert "Release date: 2026-08-14" in render_event(make_view()).body


def test_without_a_catalog_the_seam_is_a_transparent_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(i18n.LOCALE_DIR_ENV, str(tmp_path / "empty"))
    monkeypatch.delenv(i18n.LOCALE_ENV, raising=False)
    i18n.reset_translations()
    try:
        assert i18n.gettext("Type: %(type)s") == "Type: %(type)s"
        assert i18n.ngettext("one", "many", 2) == "many"
    finally:
        i18n.reset_translations()
