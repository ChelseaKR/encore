"""encore — a self-hosted, notification-first companion for Plex music libraries."""

from importlib.metadata import version

# Single-sourced from the installed distribution's metadata (`pyproject.toml`
# [project].version, REL-02) — never hand-copy a version string a second time.
__version__ = version("encore")

__all__ = ["__version__"]
