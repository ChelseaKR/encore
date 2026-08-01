"""The gettext seam — one place every user-facing string routes through.

Encore ships English-only (docs/I18N.md), but the seam exists from the *first*
user-facing string so a later translation is additive rather than a retrofit.
That first string is F4's notification text: the subject and body a user
actually reads in ntfy/Discord/email. Health-endpoint JSON keys, log lines,
and CLI diagnostics are deliberately **not** routed here — they are operator
and machine surfaces, not display text.

Usage::

    from encore.i18n import _

    _("New release: %(artist)s — %(title)s") % {"artist": ..., "title": ...}

Named ``%(...)s`` placeholders, never positional ``%s``: a translator must be
able to reorder them. Catalogs live in ``src/encore/locales/<locale>/
LC_MESSAGES/encore.mo``; none are committed yet (there is no second language),
so `translations` falls back to ``NullTranslations`` and returns the source
string unchanged. ``$ENCORE_LOCALE`` and ``$ENCORE_LOCALE_DIR`` override the
locale and the search path — the latter is what the pseudolocale test uses to
prove the seam really routes rather than merely existing.
"""

from __future__ import annotations

import gettext as gettext_module
import os
from functools import lru_cache
from pathlib import Path

__all__ = [
    "DEFAULT_LOCALE",
    "DOMAIN",
    "LOCALE_DIR_ENV",
    "LOCALE_ENV",
    "gettext",
    "ngettext",
    "reset_translations",
    "translations",
]

DOMAIN = "encore"
LOCALE_ENV = "ENCORE_LOCALE"
LOCALE_DIR_ENV = "ENCORE_LOCALE_DIR"
DEFAULT_LOCALE = "en"


def _locale_dir() -> Path:
    """Return the catalog search path: ``$ENCORE_LOCALE_DIR`` or packaged ``locales/``."""
    override = os.environ.get(LOCALE_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).parent / "locales"


def _locale() -> str:
    """Return the active locale: ``$ENCORE_LOCALE``, or English."""
    return os.environ.get(LOCALE_ENV) or DEFAULT_LOCALE


@lru_cache(maxsize=8)
def _load(locale: str, locale_dir: str) -> gettext_module.NullTranslations:
    """Load (and memoize) one catalog; a missing catalog is not an error."""
    return gettext_module.translation(
        DOMAIN,
        localedir=locale_dir,
        languages=[locale],
        fallback=True,
    )


def translations() -> gettext_module.NullTranslations:
    """Return the catalog for the active locale (``NullTranslations`` if absent)."""
    return _load(_locale(), str(_locale_dir()))


def reset_translations() -> None:
    """Drop the memoized catalogs (after changing locale or locale dir)."""
    _load.cache_clear()


def gettext(message: str) -> str:
    """Translate one message through the active catalog."""
    return translations().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Translate a message with plural forms through the active catalog."""
    return translations().ngettext(singular, plural, n)


# The conventional short aliases. `_` and `_n` are what the extractor's keyword
# list looks for and what call sites read most clearly.
_ = gettext
_n = ngettext
