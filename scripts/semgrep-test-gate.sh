#!/usr/bin/env bash
# Run the custom Semgrep rules' unit tests AND prove they actually ran.
#
# `semgrep test <dir>` exits 0 when it finds no test files at all: it prints
# "No unit tests found" as a WARNING and succeeds. So deleting a rule's test
# file — or adding a second rule and forgetting to write one — silently turns
# the rule self-test into a check that cannot fail, while `make security`
# stays green. Measured with semgrep 1.166.0:
#
#   rule regex weakened, test file present  -> exit 1  (gate works)
#   test file deleted                       -> exit 0  ("No unit tests found")
#
# This wrapper closes the second case: every rule file must have a sibling
# test file, and the run must not report that it found no tests.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

rules_dir=".semgrep-rules"
semgrep_pin="semgrep==1.166.0"

shopt -s nullglob
rule_files=("$rules_dir"/*.yml "$rules_dir"/*.yaml)
if [ ${#rule_files[@]} -eq 0 ]; then
  echo "semgrep-test-gate: FAIL — no rule files under $rules_dir/." >&2
  echo "  The custom-rule gate is only meaningful if there are rules to test." >&2
  exit 1
fi

missing=0
for rule in "${rule_files[@]}"; do
  stem="${rule%.*}"
  found=0
  for candidate in "$stem".*; do
    case "$candidate" in
      *.yml | *.yaml) continue ;;
    esac
    [ -f "$candidate" ] && found=1
  done
  if [ "$found" -eq 0 ]; then
    echo "semgrep-test-gate: FAIL — $rule has no sibling test file." >&2
    echo "  Add $stem.<ext> with '# ruleid:' / '# ok:' annotations." >&2
    missing=1
  fi
done
[ "$missing" -eq 0 ] || exit 1

if ! out="$(uvx --from "$semgrep_pin" semgrep test "$rules_dir" 2>&1)"; then
  printf '%s\n' "$out"
  echo "semgrep-test-gate: FAIL — the custom rules' unit tests did not pass." >&2
  exit 1
fi
printf '%s\n' "$out"

if printf '%s' "$out" | grep -qi "no unit tests found"; then
  echo "semgrep-test-gate: FAIL — semgrep ran no unit tests (it exits 0 in that case)." >&2
  exit 1
fi

echo "semgrep-test-gate: clean — every custom rule has tests and they pass."
