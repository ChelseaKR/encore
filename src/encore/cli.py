"""Console-script entry point: serve, sync, match, watch, notify, channels, events, feeds.

`encore serve` runs the app under uvicorn; `encore plex configure` stores
Plex credentials (token prompted or piped, never a CLI argument — flags leak
into shell history); `encore sync` is the on-demand library sync (F1);
`encore match` runs one matching pass over synced-but-unmatched artists (F2)
and `encore matches` works the review queue it fills; `encore watch` is the
on-demand release-watch cycle (F3); `encore channels` manages Apprise
notification destinations and `encore notify` runs one delivery cycle (F4);
`encore feeds` mints and rotates the F5 feed URLs. Every on-demand command
has a scheduled twin in `encore.scheduler`, running the same pass on an
interval — `encore match` and the match scheduler share
`run_matching_pass`, so the manual and automatic paths cannot drift.

`encore feeds show` is the one place the feed token is deliberately printed:
the URL *is* the capability, and handing it to the operator is this command's
entire job. It prints to a terminal the operator already trusts with the data
directory — never to a log — and both subcommands say plainly that sharing
the URL shares the taste feed and that `rotate` is the revocation.

`encore events` is F4's **in-app feed** — the always-works fallback for when
every channel is broken. It is a CLI surface rather than an HTTP route on
purpose: the feed is pure taste data, encore has no authentication until the
F6 wizard sets an admin password, and shipping an unauthenticated route on a
container port that people publish would be the exact harm the no-outing lens
exists to prevent (docs/adr/0012). Reading it over the terminal requires the
access the operator already has to the data directory.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable

# Explicit re-export ("as uvicorn"): tests monkeypatch `cli.uvicorn.run` directly
# (see tests/test_cli.py), which needs this name to be a real, typed attribute of
# the module under mypy's strict `no_implicit_reexport` rather than a bare import
# mypy treats as private to this module.
import uvicorn as uvicorn

from encore.matching.engine import candidates_from_json, run_matching_pass
from encore.matching.mb import MusicBrainzClient
from encore.models import CHANNEL_MODES
from encore.notify import DeliveryError, run_delivery_cycle, send_test_notification
from encore.notify.render import render_event
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

    match = subparsers.add_parser(
        "match", help="Run one on-demand MusicBrainz identity-matching pass over new artists (F2)"
    )
    match.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    matches = subparsers.add_parser("matches", help="Work the F2 identity-match review queue")
    matches_sub = matches.add_subparsers(dest="matches_command", required=True)

    matches_list = matches_sub.add_parser(
        "list", help="Show artists awaiting a match decision, with ranked candidates"
    )
    matches_list.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    matches_resolve = matches_sub.add_parser(
        "resolve", help="Confirm an artist's MusicBrainz identity (a review decision or re-match)"
    )
    matches_resolve.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    matches_resolve.add_argument(
        "--artist-key", required=True, help="The artist_key shown by `encore matches list`"
    )
    matches_resolve.add_argument("--mbid", required=True, help="The MusicBrainz artist ID to match")

    matches_skip = matches_sub.add_parser(
        "skip", help="Mark an artist deliberately unmatched (kept; not re-queried)"
    )
    matches_skip.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    matches_skip.add_argument(
        "--artist-key", required=True, help="The artist_key shown by `encore matches list`"
    )

    watch = subparsers.add_parser(
        "watch", help="Run one on-demand MusicBrainz release-watch cycle (F3)"
    )
    watch.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    notify = subparsers.add_parser(
        "notify", help="Run one on-demand notification delivery cycle (F4)"
    )
    notify.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    events = subparsers.add_parser(
        "events", help="Show the in-app release feed — the always-works fallback (F4)"
    )
    events.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    events.add_argument("--limit", type=int, default=20, help="How many events to show")

    feeds = subparsers.add_parser("feeds", help="Standing feed URLs: RSS + iCal (F5)")
    feeds_sub = feeds.add_subparsers(dest="feeds_command", required=True)
    feeds_show = feeds_sub.add_parser(
        "show", help="Print the feed URLs, minting the token on first use"
    )
    feeds_rotate = feeds_sub.add_parser(
        "rotate", help="Replace the feed token — every previously shared feed URL stops working"
    )
    for feeds_command in (feeds_show, feeds_rotate):
        feeds_command.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
        feeds_command.add_argument(
            "--base-url",
            default="http://127.0.0.1:8321",
            help="The URL your encore server is reachable at, as feed readers will see it "
            "(default: http://127.0.0.1:8321)",
        )

    channels = subparsers.add_parser("channels", help="Notification channels (Apprise)")
    channels_sub = channels.add_subparsers(dest="channels_command", required=True)

    channel_add = channels_sub.add_parser(
        "add",
        help="Add a channel; the Apprise URL is prompted or piped on stdin, never passed as a flag",
    )
    channel_add.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    channel_add.add_argument("--name", required=True, help="Your label for this channel")
    channel_add.add_argument("--mode", choices=CHANNEL_MODES, default="instant")
    channel_add.add_argument(
        "--digest-hours",
        type=float,
        default=24.0,
        help="Digest cadence in hours (digest mode only; default: 24)",
    )

    channel_list = channels_sub.add_parser("list", help="List channels and their health")
    channel_list.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    channel_remove = channels_sub.add_parser("remove", help="Delete a channel")
    channel_remove.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    channel_remove.add_argument("--name", required=True)

    channel_enable = channels_sub.add_parser("enable", help="Re-enable a channel")
    channel_enable.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    channel_enable.add_argument("--name", required=True)

    channel_disable = channels_sub.add_parser(
        "disable", help="Stop delivering to a channel without deleting it"
    )
    channel_disable.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    channel_disable.add_argument("--name", required=True)

    channel_test = channels_sub.add_parser("test", help="Fire a test notification at a channel")
    channel_test.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    channel_test.add_argument("--name", required=True)

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


def _read_hidden(prompt: str) -> str:
    """Read one secret: piped stdin if not a TTY, else a hidden prompt.

    Never a CLI flag — argv is visible in `ps` output and shell history.
    """
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass(prompt).strip()


def _read_token() -> str:
    """Read the Plex token (see `_read_hidden`)."""
    return _read_hidden("Plex token (input hidden): ")


def _read_channel_url() -> str:
    """Read an Apprise channel URL — a credential, so hidden like the token."""
    return _read_hidden("Apprise URL (input hidden): ")


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


def _cmd_match(args: argparse.Namespace) -> int:
    """Run one matching pass over synced-but-unmatched artists (F2).

    The same `run_matching_pass` the scheduled match job runs. Skip-don't-
    queue, the same posture `encore watch` uses for MusicBrainz (risk R8):
    one artist's failure is counted and the pass moves on rather than
    wedging on it, and the next run — manual or scheduled — retries exactly
    the failed ones because matched artists are excluded from the backlog.
    No Plex-GUID hint is passed yet — the scoring function accepts one
    (`ArtistHints.guid_mbid`) as a boost, never an auto-accept, but nothing
    in this repo extracts an MBID out of a Plex GUID yet, so a name-only
    match is the honest current behavior rather than a silently-never-
    populated hint.
    """
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    client = MusicBrainzClient()
    try:
        report = run_matching_pass(storage, client)
    finally:
        client.close()
        storage.close()
    if report.candidates == 0:
        print("No unmatched artists. Run `encore sync` first, or everything is matched.")
        return 0
    print(
        f"match complete: candidates={report.candidates} auto={report.auto} "
        f"pending={report.pending} failed={report.failed}"
    )
    if report.pending:
        print(f"{report.pending} artist(s) need a decision — run `encore matches list`.")
    return 0


def _format_candidate(candidate: dict[str, object]) -> str:
    """Render one ranked candidate line for `encore matches list`."""
    name = candidate.get("name", "?")
    mbid = candidate.get("mbid", "?")
    score = candidate.get("score")
    score_text = f"{score:.2f}" if isinstance(score, int | float) else "?"
    extras = ", ".join(
        str(candidate[key]) for key in ("type", "country", "disambiguation") if candidate.get(key)
    )
    suffix = f" ({extras})" if extras else ""
    return f"    {score_text}  {name}{suffix}  mbid={mbid}"


def _cmd_matches_list(args: argparse.Namespace) -> int:
    """Show every artist awaiting a review decision, with its ranked candidates."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        queue = storage.list_review_queue()
    finally:
        storage.close()
    if not queue:
        print("Review queue is empty. Run `encore match` to look for new artists.")
        return 0
    for row in queue:
        print(f"{row.artist_name}  (artist_key={row.artist_key})")
        for candidate in candidates_from_json(row.candidates_json)[:5]:
            print(_format_candidate(candidate))
        print(f"    resolve: encore matches resolve --artist-key {row.artist_key} --mbid <mbid>")
        print(f"    skip:    encore matches skip --artist-key {row.artist_key}")
    return 0


def _cmd_matches_resolve(args: argparse.Namespace) -> int:
    """Manually confirm one artist's MusicBrainz identity."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.resolve_artist_match(args.artist_key, args.mbid)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(f"Resolved {args.artist_key!r} to {args.mbid}. Picked up on the next `encore watch`.")
    return 0


def _cmd_matches_skip(args: argparse.Namespace) -> int:
    """Mark one artist deliberately unmatched."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.skip_artist_match(args.artist_key)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(f"Skipped {args.artist_key!r}. It will not be re-matched or watched.")
    return 0


_MATCHES_COMMANDS = {
    "list": _cmd_matches_list,
    "resolve": _cmd_matches_resolve,
    "skip": _cmd_matches_skip,
}


def _cmd_matches(args: argparse.Namespace) -> int:
    """Dispatch an `encore matches …` subcommand."""
    handler = _MATCHES_COMMANDS.get(args.matches_command)
    if handler is None:  # pragma: no cover - argparse rejects unknown subcommands
        return 1
    return handler(args)


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


def _cmd_notify(args: argparse.Namespace) -> int:
    """Run one on-demand delivery cycle and print the counts."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        report = run_delivery_cycle(storage)
    finally:
        storage.close()
    print(
        f"delivery complete: enqueued={report.enqueued} sent={report.sent} "
        f"digests={report.digests_sent} retried={report.retried} failed={report.failed} "
        f"channels_skipped={report.channels_skipped} settled={report.events_settled}"
    )
    if report.failed or report.channels_skipped:
        print("some channels are unhealthy — run `encore channels list` for the last error.")
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    """Print the in-app feed: the newest release events, rendered."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        views = storage.list_event_views(limit=max(1, args.limit))
        machine_identifier = storage.get_plex_machine_identifier()
    finally:
        storage.close()
    if not views:
        print("No release events yet. Run `encore watch` once artists are matched.")
        return 0
    for view in views:
        rendered = render_event(view, machine_identifier)
        stamp = view.created_at.strftime("%Y-%m-%d %H:%M")
        print(f"[{stamp}] {rendered.title}")
        for line in rendered.body.splitlines():
            print(f"    {line}")
    return 0


def _print_feed_urls(base_url: str, token: str) -> None:
    """Print the two feed URLs and the sharing caution they always travel with."""
    base = base_url.rstrip("/")
    print(f"RSS (release events):    {base}/feeds/{token}/releases.xml")
    print(f"iCal (upcoming dates):   {base}/feeds/{token}/upcoming.ics")
    print()
    print("Anyone with these URLs can read your release feed — they reveal the")
    print("artists in your library. Share them only where you'd share that, and")
    print("run `encore feeds rotate` to revoke every previously shared URL.")


def _cmd_feeds(args: argparse.Namespace) -> int:
    """Dispatch an `encore feeds …` subcommand (show or rotate)."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if args.feeds_command == "rotate":
            token = storage.rotate_feed_token()
            print("Feed token rotated: the previous feed URLs no longer work.")
        else:
            token = storage.ensure_feed_token()
    except SecretDecryptionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    _print_feed_urls(args.base_url, token)
    return 0


def _cmd_channels_add(args: argparse.Namespace) -> int:
    """Add a notification channel (URL prompted or piped, encrypted at rest)."""
    url = _read_channel_url()
    if not url:
        print("error: empty Apprise URL", file=sys.stderr)
        return 2
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.add_channel(args.name, url, mode=args.mode, digest_interval_hours=args.digest_hours)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    cadence = f", every {args.digest_hours}h" if args.mode == "digest" else ""
    print(f"Added channel {args.name!r} ({args.mode}{cadence}).")
    print("The URL is encrypted at rest and is never printed or logged.")
    print(f"Verify it now with: encore channels test --name {args.name}")
    return 0


def _cmd_channels_list(args: argparse.Namespace) -> int:
    """List channels with their health — never their URLs."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        channels = storage.list_channels()
    finally:
        storage.close()
    if not channels:
        print("No notification channels configured. Add one with `encore channels add`.")
        return 0
    for channel in channels:
        state = "enabled" if channel.enabled else "disabled"
        cadence = f" every {channel.digest_interval_hours}h" if channel.mode == "digest" else ""
        print(f"{channel.name}  [{channel.mode}{cadence}, {state}]")
        if channel.last_success_at is not None:
            print(f"    last delivered: {channel.last_success_at:%Y-%m-%d %H:%M}")
        if channel.consecutive_failures:
            print(f"    failing: {channel.consecutive_failures} consecutive attempt(s)")
            print(f"    last error: {channel.last_error}")
    return 0


def _cmd_channels_remove(args: argparse.Namespace) -> int:
    """Delete a channel and its delivery rows."""
    return _channel_mutation(args, "remove")


def _cmd_channels_enable(args: argparse.Namespace) -> int:
    """Re-enable a disabled channel."""
    return _channel_mutation(args, "enable")


def _cmd_channels_disable(args: argparse.Namespace) -> int:
    """Stop delivering to a channel without deleting its history."""
    return _channel_mutation(args, "disable")


def _channel_mutation(args: argparse.Namespace, action: str) -> int:
    """Apply remove/enable/disable to one channel by name."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        if action == "remove":
            storage.remove_channel(args.name)
        else:
            storage.set_channel_enabled(args.name, action == "enable")
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(f"Channel {args.name!r} {action}d.")
    return 0


def _cmd_channels_test(args: argparse.Namespace) -> int:
    """Fire the test notification at one channel and report the outcome."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        send_test_notification(storage, args.name)
    except (StorageError, SecretDecryptionError, DeliveryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(f"Test notification sent to {args.name!r}.")
    return 0


_CHANNEL_COMMANDS = {
    "add": _cmd_channels_add,
    "list": _cmd_channels_list,
    "remove": _cmd_channels_remove,
    "enable": _cmd_channels_enable,
    "disable": _cmd_channels_disable,
    "test": _cmd_channels_test,
}


def _cmd_channels(args: argparse.Namespace) -> int:
    """Dispatch an `encore channels …` subcommand."""
    handler = _CHANNEL_COMMANDS.get(args.channels_command)
    if handler is None:  # pragma: no cover - argparse rejects unknown subcommands
        return 1
    return handler(args)


def _cmd_plex(args: argparse.Namespace) -> int:
    """Dispatch an `encore plex …` subcommand."""
    if args.plex_command == "configure":
        return _cmd_plex_configure(args)
    return 1  # pragma: no cover - argparse rejects unknown subcommands


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the HTTP server under uvicorn."""
    # uvicorn imports "encore.app:app" by string, so the flag travels via the
    # environment; the app factory resolves it at startup with the same
    # precedence --data-dir's help text documents. This is the real wiring
    # the M0 dead flag lacked (docs/adr/0005) — the storage layer now exists
    # for it to point at.
    os.environ[DATA_DIR_ENV] = str(resolve_data_dir(args.data_dir))
    # No access log (OBS-11). The F5 feed capability token travels in the URL
    # path, and uvicorn's access log writes the whole request line to stdout —
    # which for the shipped container is `docker logs`, where it stays forever
    # and outlives any rotation. A feed poll every fifteen minutes would print
    # the token ninety-six times a day. Startup/error logging is untouched, so
    # the operator still sees the server come up and still sees failures; if
    # request-level observability ever lands it has to redact the path first.
    uvicorn.run("encore.app:app", host=args.host, port=args.port, access_log=False)
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "serve": _cmd_serve,
    "sync": _cmd_sync,
    "match": _cmd_match,
    "matches": _cmd_matches,
    "watch": _cmd_watch,
    "notify": _cmd_notify,
    "events": _cmd_events,
    "channels": _cmd_channels,
    "feeds": _cmd_feeds,
    "plex": _cmd_plex,
}


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested subcommand."""
    args = _build_parser().parse_args(argv)
    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        return 1
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
