#!/usr/bin/env bash
# Fail on a TODO/FIXME/HACK marker that names neither a GitHub issue (#NN) nor a
# milestone (M0-M4). CQ-34: an untracked TODO is a defect, not a style nit — the
# portfolio's own conformance audit caught exactly one of these (release.yml, the
# GHCR-publish stub) with no reference at all. CQ-35 (bare `noqa`/`type: ignore`
# suppressions) is covered separately for Python files by ruff's PGH rules
# (pyproject.toml); this script covers every other file (workflows, docs,
# Dockerfile, scripts) that ruff never lints.
#
# A marker only counts once it's followed by a colon (`TODO:`, `TODO(name):`,
# `FIXME (context):`) — this deliberately excludes prose that merely *mentions*
# the words, e.g. this file's own comments, or a make target named `todo-gate`.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

marker_re='(TODO|FIXME|HACK)[[:space:]]*(\([^)]*\))?[[:space:]]*:'
ref_re='#[0-9]+|M[0-4]'

fail=0

while IFS= read -r -d '' f; do
  # Skip this script (it necessarily discusses the markers it looks for) and
  # lockfiles (dependency names can coincidentally match the marker regex).
  case "$f" in
    scripts/todo-gate.sh|uv.lock|*.lock) continue ;;
  esac
  [ -f "$f" ] || continue

  while IFS=: read -r lineno line; do
    [ -z "$lineno" ] && continue
    if ! [[ "$line" =~ $ref_re ]]; then
      echo "todo-gate: $f:$lineno: marker has no #issue or M0-M4 milestone reference:"
      echo "  $line"
      fail=1
    fi
  done < <(grep -nE "$marker_re" "$f" 2>/dev/null || true)
done < <(git ls-files -z)

if [ "$fail" -ne 0 ]; then
  echo "todo-gate: FAIL — add '#<issue-number>' or 'M0'..'M4' to each marker above." >&2
  exit 1
fi
echo "todo-gate: clean — every TODO/FIXME/HACK names an issue or a milestone."
