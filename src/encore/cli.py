"""Console-script entry point: `encore serve` runs the app under uvicorn."""

from __future__ import annotations

import argparse

# Explicit re-export ("as uvicorn"): tests monkeypatch `cli.uvicorn.run` directly
# (see tests/test_cli.py), which needs this name to be a real, typed attribute of
# the module under mypy's strict `no_implicit_reexport` rather than a bare import
# mypy treats as private to this module.
import uvicorn as uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="encore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the encore server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8321)
    # No --data-dir yet: there is no SQLite file or Fernet key to locate until
    # M1's storage layer exists (docs/adr/0005). Adding the flag now, parsed but
    # unused, was a latent dead-code bug (the Dockerfile CMD passed it for
    # nothing) — re-add it deliberately, wired to something real, when M1 lands.

    args = parser.parse_args(argv)

    if args.command == "serve":
        uvicorn.run("encore.app:app", host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
