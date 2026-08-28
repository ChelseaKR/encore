#!/usr/bin/env python3
"""Validate SLO declarations against the Observability Standard §4 schema (OBS-14, B6).

Usage: uv run python scripts/validate_slos.py slos/

Checks every ``*.yaml``/``*.yml`` under the given directory for the required
schema fields and sane value ranges. This closes the "declared but never
schema-validated" half of OBS-14; the *other* half (wiring ``sli_query`` to a
real metric instead of the documented placeholder) lands with the F3 poller at
M2 and is tracked in the roadmap, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

REQUIRED_FIELDS: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "sli_query": str,
    "target_percentage": (int, float),
    "window_days": int,
    "error_budget_policy": str,
}


def _field_errors(path: Path, doc: dict[str, object]) -> list[str]:
    """Check required-field presence and types."""
    errors: list[str] = []
    for field, expected in REQUIRED_FIELDS.items():
        if field not in doc:
            errors.append(f"{path}: missing required field '{field}'")
            continue
        value = doc[field]
        if isinstance(value, bool) or not isinstance(value, expected):
            errors.append(f"{path}: field '{field}' must be {expected}, got {type(value).__name__}")
    return errors


def _value_errors(path: Path, doc: dict[str, object]) -> list[str]:
    """Check value ranges and non-emptiness on fields that type-checked."""
    errors: list[str] = []
    target = doc.get("target_percentage")
    if isinstance(target, (int, float)) and not isinstance(target, bool) and not 0 < target <= 100:
        errors.append(f"{path}: target_percentage must be in (0, 100], got {target}")

    window = doc.get("window_days")
    if isinstance(window, int) and not isinstance(window, bool) and window <= 0:
        errors.append(f"{path}: window_days must be a positive integer, got {window}")

    for field in ("name", "sli_query", "error_budget_policy"):
        value = doc.get(field)
        if isinstance(value, str) and not value.strip():
            errors.append(f"{path}: field '{field}' must not be empty")
    return errors


def validate_slo_file(path: Path) -> list[str]:
    """Return a list of human-readable schema violations for one SLO file."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{path}: not parseable as YAML: {exc}"]

    if not isinstance(doc, dict):
        return [f"{path}: top level must be a mapping, got {type(doc).__name__}"]

    return _field_errors(path, doc) + _value_errors(path, doc)


def main(argv: list[str]) -> int:
    """Validate every SLO file under the directory given as argv[1]."""
    if len(argv) != 2:
        print("usage: validate_slos.py <slo-directory>", file=sys.stderr)
        return 2

    slo_dir = Path(argv[1])
    if not slo_dir.is_dir():
        print(f"validate_slos: {slo_dir} is not a directory", file=sys.stderr)
        return 2

    files = sorted(p for ext in ("*.yaml", "*.yml") for p in slo_dir.rglob(ext))
    if not files:
        print(f"validate_slos: no SLO files found under {slo_dir}", file=sys.stderr)
        return 1

    all_errors: list[str] = []
    for path in files:
        all_errors.extend(validate_slo_file(path))

    if all_errors:
        for error in all_errors:
            print(f"validate_slos: FAIL — {error}", file=sys.stderr)
        return 1

    print(f"validate_slos: OK — {len(files)} SLO file(s) conform to the OBS-14 schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
