#!/usr/bin/env bash
# I18N gate G2-lite (docs/I18N.md): the committed extraction template must be
# current — every user-facing string routed through `encore.i18n` is in
# `src/encore/locales/encore.pot`, and nothing stale is left behind.
#
# The template is extracted with --omit-header --sort-output --no-location so
# the comparison is over the *set of translatable strings*, not over line
# numbers or a generation timestamp: moving a function must not fail this gate,
# adding or deleting a user-facing string must.
#
# G7/G6/G5/G3 (msgfmt --check, EN/ES key parity, completeness, tag validity)
# stay declared-and-deferred in docs/I18N.md until a second catalog exists —
# there is nothing to compile or compare against yet.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

template="src/encore/locales/encore.pot"
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

uv run pybabel extract \
  --mapping-file babel.cfg \
  --keyword _ \
  --keyword _n:1,2 \
  --omit-header \
  --sort-output \
  --no-location \
  --output-file "$tmpdir/encore.pot" \
  src

if ! diff -u "$template" "$tmpdir/encore.pot"; then
  echo "i18n-check: FAIL — $template is stale." >&2
  echo "Regenerate it with: make i18n-extract" >&2
  exit 1
fi

echo "i18n-check: clean — the extraction template matches the source strings."
