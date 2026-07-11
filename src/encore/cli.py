"""Console-script entry point: `encore serve` runs the app under uvicorn."""

from __future__ import annotations

import argparse
import os

# Explicit re-export ("as uvicorn"): tests monkeypatch `cli.uvicorn.run` directly
# (see tests/test_cli.py), which needs this name to be a real, typed attribute of
# the module under mypy's strict `no_implicit_reexport` rather than a bare import
# mypy treats as private to this module.
import uvicorn as uvicorn

from encore.storage import DATA_DIR_ENV, resolve_data_dir


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested subcommand."""
    parser = argparse.ArgumentParser(prog="encore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the encore server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8321)
    serve.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Directory holding the SQLite database and its Fernet key file "
            f"(default: ${DATA_DIR_ENV} if set, else ./data)"
        ),
    )

    args = parser.parse_args(argv)

    if args.command == "serve":
        # uvicorn imports "encore.app:app" by string, so the flag travels via the
        # environment; the app factory resolves it at startup with the same
        # precedence --data-dir's help text documents. This is the real wiring
        # the M0 dead flag lacked (docs/adr/0005) — the storage layer now exists
        # for it to point at.
        os.environ[DATA_DIR_ENV] = str(resolve_data_dir(args.data_dir))
        uvicorn.run("encore.app:app", host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
