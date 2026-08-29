"""Merge-gate logic, importable so the gates can be tested like any other code.

This package marker exists for two reasons, both about the gates being real code:
``mypy`` type-checks ``scripts/`` to the same standard as ``src/`` (see
``[tool.mypy].files`` in ``pyproject.toml``), and ``tests/test_doc_audit.py`` imports
``scripts.doc_audit`` so the doc-audit gate is exercised by the suite rather than only
by a Makefile target. Without the marker the same file resolves as both ``doc_audit``
and ``scripts.doc_audit`` and mypy refuses to analyse either.

Each script is still runnable directly (``uv run python scripts/validate_slos.py``);
nothing here changes how the Makefile invokes them, and nothing here ships in the wheel.
"""
