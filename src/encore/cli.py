"""Console-script entry point: serve, sync, watch, and Plex configuration.

`encore serve` runs the app under uvicorn; `encore plex configure` stores
Plex credentials (token prompted or piped, never a CLI argument — flags leak
into shell history); `encore sync` is the on-demand library sync (F1);
`encore watch` is the on-demand release-watch cycle (F3). The scheduled
paths live in `encore.scheduler`.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

# Explicit re-export ("as uvicorn"): tests monkeypatch `cli.uvicorn.run` directly
# (see tests/test_cli.py), which needs this name to be a real, typed attribute of
# the module under mypy's strict `no_implicit_reexport` rather than a bare import
# mypy treats as private to this module.
import uvicorn as uvicorn

from encore.matching.mb import MusicBrainzClient
from encore.plex import PlexMusicClient, PlexWriteAttemptError
from encore.secretstore import SecretDecryptionError
from encore.storage import DATA_DIR_ENV, Storage, StorageError, resolve_data_dir
from encore.sync import SyncError, sync_artists
from encore.watch import watch_all_artists

_DATA_DIR_HELP = (
    "Directory holding the SQLite database and its Fernet key file "
    f"(default: ${DATA_DIR_ENV} if set, else ./data)"
)


def _build_parser() -> argparse.ArgumentParser:
    """Declare the CLI surface."""
    parser = argparse.ArgumentParser(prog="encore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the encore server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8321)
    serve.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    sync = subparsers.add_parser("sync", help="Run one on-demand Plex library sync (F1)")
    sync.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    sync.add_argument(
        "--library",
        action="append",
        default=None,
        metavar="KEY",
        help="Music library key to sync (repeatable; default: stored selection, else all)",
    )

    watch = subparsers.add_parser(
        "watch", help="Run one on-demand MusicBrainz release-watch cycle (F3)"
    )
    watch.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    plex = subparsers.add_parser("plex", help="Plex connection settings")
    plex_sub = plex.add_subparsers(dest="plex_command", required=True)
    configure = plex_sub.add_parser(
        "configure",
        help="Store the Plex base URL, token (prompted or piped on stdin), "
        "and optional library selection",
    )
    configure.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    configure.add_argument(
        "--base-url", required=True, help="Plex server URL, e.g. http://plex.local:32400"
    )
    configure.add_argument(
        "--library",
        action="append",
        default=None,
        metavar="KEY",
        help="Music library key to watch (repeatable; omit to sync all music libraries)",
    )
    return parser


def _read_token() -> str:
    """Read the Plex token: piped stdin if not a TTY, else a hidden prompt.

    Never a CLI flag — argv is visible in `ps` output and shell history.
    """
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass("Plex token (input hidden): ").strip()


def _cmd_plex_configure(args: argparse.Namespace) -> int:
    """Store Plex credentials (encrypted at rest) and the library selection."""
    token = _read_token()
    if not token:
        print("error: empty Plex token", file=sys.stderr)
        return 2
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.set_plex_credentials(args.base_url, token)
        storage.set_plex_libraries(args.library)
    finally:
        storage.close()
    libraries = "all music libraries" if args.library is None else ", ".join(args.library)
    print(f"Stored Plex credentials for {args.base_url} (libraries: {libraries}).")
    print("The token is encrypted at rest; back up the data directory as a whole")
    print("(database and key file together — docs/adr/0008).")
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    """Run one on-demand sync and print the report."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        credentials = storage.get_plex_credentials()
        if credentials is None:
            print(
                "error: no Plex credentials configured — run `encore plex configure` first",
                file=sys.stderr,
            )
            return 2
        client = PlexMusicClient(*credentials)
        report = sync_artists(storage, client, args.library)
    except (SyncError, SecretDecryptionError, PlexWriteAttemptError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(
        f"sync complete: libraries={','.join(report.library_keys)} "
        f"seen={report.seen} added={report.added} updated={report.updated} "
        f"resurrected={report.resurrected} tombstoned={report.tombstoned} "
        f"skipped_compilations={report.skipped_compilations}"
    )
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Run one on-demand release-watch cycle and print the report (counts only)."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # No MusicBrainzError handling: watch_all_artists skips failed artists
    # (counts them in the report) rather than raising — skip-don't-queue.
    client = MusicBrainzClient()
    try:
        report = watch_all_artists(storage, client)
    finally:
        client.close()
        storage.close()
    print(
        f"watch complete: polled={report.artists_polled} failed={report.artists_failed} "
        f"baselined={report.artists_baselined} groups={report.groups_seen} "
        f"new={report.events_new} upcoming={report.events_upcoming} "
        f"date_changed={report.events_date_changed}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested subcommand."""
    args = _build_parser().parse_args(argv)

    if args.command == "serve":
        # uvicorn imports "encore.app:app" by string, so the flag travels via the
        # environment; the app factory resolves it at startup with the same
        # precedence --data-dir's help text documents. This is the real wiring
        # the M0 dead flag lacked (docs/adr/0005) — the storage layer now exists
        # for it to point at.
        os.environ[DATA_DIR_ENV] = str(resolve_data_dir(args.data_dir))
        uvicorn.run("encore.app:app", host=args.host, port=args.port)
        return 0
    if args.command == "sync":
        return _cmd_sync(args)
    if args.command == "watch":
        return _cmd_watch(args)
    if args.command == "plex" and args.plex_command == "configure":
        return _cmd_plex_configure(args)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
