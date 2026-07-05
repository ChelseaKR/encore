"""Console-script entry point: `encore serve` runs the app under uvicorn."""

from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="encore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the encore server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8321)
    serve.add_argument("--data-dir", default="./instance")

    args = parser.parse_args(argv)

    if args.command == "serve":
        uvicorn.run("encore.app:app", host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
