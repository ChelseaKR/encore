"""encore — a self-hosted, notification-first companion for Plex music libraries."""

from importlib.metadata import version

# Single-sourced from the installed distribution's metadata (`pyproject.toml`
# [project].version, REL-02) — never hand-copy a version string a second time.
#
# The argument is the *distribution* name, `encore-plex`, not this package's import
# name, `encore`. The two differ because PyPI's `encore` belongs to Enthought, Inc.
# (see pyproject.toml [project].name). This lookup is the reason that matters beyond
# publishing: `version("encore")` resolves against whatever distribution owns that
# metadata in the environment, so in any environment that also has Enthought's
# `encore` installed it would have silently reported 0.8.0 as this project's version.
__version__ = version("encore-plex")

__all__ = ["__version__"]
