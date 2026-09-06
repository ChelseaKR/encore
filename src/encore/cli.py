"""Console-script entry point: serve, sync, match, watch, notify, channels, events, feeds.

`encore serve` runs the app under uvicorn; `encore plex configure` stores
Plex credentials (token prompted or piped, never a CLI argument — flags leak
into shell history); `encore sync` is the on-demand library sync (F1);
`encore match` runs one matching pass over synced-but-unmatched artists (F2)
and `encore matches` works the review queue it fills; `encore watch` is the
on-demand release-watch cycle (F3); `encore channels` manages Apprise
notification destinations and `encore notify` runs one delivery cycle (F4);
`encore feeds` mints and rotates the F5 feed URLs; `encore artists` and
`encore settings` manage F10 watch policy (types, muting, priority);
`encore recommend` refreshes F7 recommendations and `recommendations`
works them (list / dismiss / promote). Every on-demand command
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
import json
import os
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path

# Explicit re-export ("as uvicorn"): tests monkeypatch `cli.uvicorn.run` directly
# (see tests/test_cli.py), which needs this name to be a real, typed attribute of
# the module under mypy's strict `no_implicit_reexport` rather than a bare import
# mypy treats as private to this module.
import uvicorn as uvicorn

from encore.artistsettings import (
    DEFAULT_ALLOWED_PRIMARY,
    PRIMARY_TYPE_SLUGS,
    PRIORITY_TIERS,
    SettingsError,
    SettingsOverride,
    canonical_override_json,
    parse_primary_types,
    parse_secondary_types,
    parse_settings_json,
)
from encore.backup import BackupError, create_backup, restore_backup
from encore.doctor import exit_code as doctor_exit_code
from encore.doctor import render_json as doctor_render_json
from encore.doctor import render_text as doctor_render_text
from encore.doctor import run_checks as doctor_run_checks
from encore.matching.engine import candidates_from_json, run_matching_pass
from encore.matching.explain import audit_record, explain_match
from encore.matching.explain import render_json as explain_render_json
from encore.matching.explain import render_text as explain_render_text
from encore.matching.mb import MusicBrainzClient
from encore.models import CHANNEL_MODES, utcnow
from encore.notify import DeliveryError, run_delivery_cycle, send_test_notification
from encore.notify.render import render_event
from encore.plex import PlexMusicClient, PlexWriteAttemptError
from encore.portable import (
    ImportStrategy,
    PortableError,
    export_state,
    import_state,
    read_document,
    write_document,
)
from encore.recommend.engine import PROVENANCE_LIMIT, refresh_recommendations
from encore.recommend.lb import ListenBrainzClient
from encore.secretstore import SecretDecryptionError
from encore.storage import (
    DATA_DIR_ENV,
    DB_FILENAME,
    KEY_FILENAME,
    Storage,
    StorageError,
    resolve_data_dir,
)
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

    doctor = subparsers.add_parser(
        "doctor",
        help="Run the offline diagnostic checklist (exit 0 pass / 1 warn / 2 fail)",
    )
    doctor.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    doctor.add_argument(
        "--check-upstream",
        action="store_true",
        help="Also probe MusicBrainz, ListenBrainz labs and the Cover Art Archive. "
        "Without this flag the command opens no socket at all.",
    )
    doctor.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit the report as JSON."
    )

    backup = subparsers.add_parser(
        "backup",
        help="Write one consistent, verified snapshot of the data directory to a tar archive",
    )
    backup.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    backup.add_argument(
        "--out",
        required=True,
        metavar="PATH",
        help="Archive to write. It contains the Fernet key: treat it as a secret "
        "and store it where the live key would be safe.",
    )

    restore = subparsers.add_parser(
        "restore",
        help="Rebuild a data directory from a backup archive, verifying it first",
    )
    restore.add_argument("archive", metavar="ARCHIVE", help="The tar archive written by `backup`")
    restore.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    restore.add_argument(
        "--force",
        action="store_true",
        help="Replace the contents of a non-empty data directory (default: refuse)",
    )

    export = subparsers.add_parser(
        "export",
        help="Write a portable, secret-free description of what this install watches",
    )
    export.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    export.add_argument(
        "--out", required=True, metavar="PATH", help="Write the JSON document to PATH"
    )

    imp = subparsers.add_parser(
        "import",
        help="Merge a watch-state document written by `export` into this install",
    )
    imp.add_argument("document", metavar="FILE", help="The JSON document written by `export`")
    imp.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    imp.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the merge plan and change nothing",
    )
    imp.add_argument(
        "--strategy",
        choices=[strategy.value for strategy in ImportStrategy],
        default=ImportStrategy.KEEP_LOCAL.value,
        help=(
            "What to do where this install and the file disagree: keep-local "
            "(default, list the conflict and change nothing) or prefer-file"
        ),
    )

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

    matches_explain = matches_sub.add_parser(
        "explain", help="Show the evidence behind one artist's match decision (offline)"
    )
    matches_explain.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    matches_explain.add_argument(
        "--artist-key", required=True, help="The artist key to explain (see `matches list`)"
    )
    matches_explain.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit the explanation as JSON."
    )

    matches_audit = matches_sub.add_parser(
        "audit",
        help="Dump every current decision with its evidence as JSONL, for review sampling",
    )
    matches_audit.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    matches_audit.add_argument(
        "--out", required=True, metavar="FILE", help="Write one JSON object per line to FILE"
    )

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

    artists = subparsers.add_parser("artists", help="Your library artists and their settings (F10)")
    artists_sub = artists.add_subparsers(dest="artists_command", required=True)
    artists_list = artists_sub.add_parser(
        "list", help="List library artists with match status and per-artist settings"
    )
    artists_list.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)

    artists_settings = artists_sub.add_parser(
        "settings", help="Set one artist's release types, muting, or priority tier"
    )
    artists_settings.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    artists_settings.add_argument(
        "--artist-key", required=True, help="The artist_key shown by `encore artists list`"
    )
    artists_settings.add_argument(
        "--allow-primary",
        default=None,
        metavar="TYPES",
        help="Comma-separated primary release types to allow for this artist "
        f"(known: {','.join(sorted(PRIMARY_TYPE_SLUGS))}); replaces any previous list",
    )
    artists_settings.add_argument(
        "--allow-secondary",
        default=None,
        metavar="TYPES",
        help="Comma-separated secondary release types to allow (live, remix, ...)",
    )
    artists_settings.add_argument(
        "--reset-types", action="store_true", help="Clear this artist's type overrides"
    )
    artists_settings.add_argument(
        "--mute", action="store_true", help="Mute deliveries from this artist until unmuted"
    )
    artists_settings.add_argument(
        "--mute-until",
        default=None,
        metavar="YYYY-MM-DD",
        help="Mute deliveries from this artist through a date",
    )
    artists_settings.add_argument("--unmute", action="store_true", help="Lift any mute")
    artists_settings.add_argument(
        "--priority",
        default=None,
        choices=PRIORITY_TIERS,
        help="Delivery tier: instant breaks digest windows; digest waits even on "
        "instant channels (default: normal — follow each channel's mode)",
    )

    settings = subparsers.add_parser("settings", help="Global defaults (F10)")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)
    settings_show = settings_sub.add_parser("show", help="Show the global watch defaults")
    settings_show.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    settings_types = settings_sub.add_parser(
        "default-types", help="Set the global default release-type allowlist"
    )
    settings_types.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    settings_types.add_argument(
        "--primary",
        default=None,
        metavar="TYPES",
        help="Comma-separated primary types every artist allows by default "
        "(omit to keep the current list)",
    )
    settings_types.add_argument(
        "--secondary",
        default=None,
        metavar="TYPES",
        help="Comma-separated secondary types allowed by default "
        "('' clears the list; omit to keep the current list)",
    )

    recommend = subparsers.add_parser(
        "recommend", help="Run one recommendation refresh over the watched library (F7)"
    )
    recommend.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    recommend.add_argument(
        "--limit", type=int, default=50, help="How many candidates to persist (default: 50)"
    )

    recs = subparsers.add_parser("recommendations", help="Recommended artists (F7)")
    recs_sub = recs.add_subparsers(dest="recs_command", required=True)
    recs_list = recs_sub.add_parser("list", help="Show recommended artists, best first")
    recs_list.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    recs_list.add_argument("--limit", type=int, default=20, help="How many to show")
    recs_dismiss = recs_sub.add_parser(
        "dismiss", help="Dismiss a candidate — it never comes back in a refresh"
    )
    recs_dismiss.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    recs_dismiss.add_argument(
        "--mbid", required=True, help="The MBID shown by `recommendations list`"
    )
    recs_promote = recs_sub.add_parser(
        "promote", help="Promote a candidate into your watched library (F8)"
    )
    recs_promote.add_argument("--data-dir", default=None, help=_DATA_DIR_HELP)
    recs_promote.add_argument(
        "--mbid", required=True, help="The MBID shown by `recommendations list`"
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


def _cmd_matches_explain(args: argparse.Namespace) -> int:
    """Print the evidence behind one artist's match decision. Never queries MB."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        row = storage.get_artist_match(args.artist_key)
    finally:
        storage.close()
    if row is None:
        # Not an empty explanation: this artist has no decision at all, which
        # is a different fact from a decision with no candidates.
        print(
            f"error: no match record for artist key {args.artist_key!r}. "
            "Run `encore matches list` to see the keys that have one.",
            file=sys.stderr,
        )
        return 1
    explanation = explain_match(row)
    if args.as_json:
        print(json.dumps(explain_render_json(explanation), indent=2, sort_keys=True))
    else:
        print(explain_render_text(explanation))
    return 0


def _cmd_matches_audit(args: argparse.Namespace) -> int:
    """Write every stored decision with its evidence, one JSON object per line.

    The sample sheet the U8 validation spike (issue #46) needs: each line
    carries a `correct` field left null for a human to fill in, so precision
    can be computed from labels rather than asserted.
    """
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        rows = storage.list_artist_matches()
    finally:
        storage.close()
    destination = Path(args.out)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(audit_record(explain_match(row)), sort_keys=True) + "\n")
    print(f"Wrote {len(rows)} decision(s) to {destination}.")
    print("Fill in each line's `correct` field to compute field precision (issue #46).")
    return 0


_MATCHES_COMMANDS = {
    "list": _cmd_matches_list,
    "explain": _cmd_matches_explain,
    "audit": _cmd_matches_audit,
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
        f"date_changed={report.events_date_changed} filtered={report.events_filtered}"
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


def _describe_override(override: SettingsOverride, defaults: SettingsOverride) -> str:
    """One-line human summary of an artist's settings against the global layer."""
    parts: list[str] = []
    if override.allow_primary is not None or override.allow_secondary is not None:
        primary = ",".join(sorted(override.allow_primary or ()))
        secondary = ",".join(sorted(override.allow_secondary or ()))
        parts.append(f"types={primary or 'none'}+{secondary or 'none'} (override)")
    if override.muted:
        parts.append("muted")
    if override.mute_until is not None:
        parts.append(f"muted until {override.mute_until.isoformat()}")
    if override.priority is not None:
        parts.append(f"tier={override.priority}")
    effective = parse_settings_json(canonical_override_json(defaults) or "")
    eff_primary = override.allow_primary or effective.allow_primary or ("album",)
    eff_secondary = override.allow_secondary or effective.allow_secondary or ()
    suffix = f"; effective types: {','.join(sorted(eff_primary))}+{','.join(sorted(eff_secondary))}"
    if not parts:
        return "default" + suffix
    return "; ".join(parts) + suffix


def _cmd_artists_list(args: argparse.Namespace) -> int:
    """List the library directory with match status and settings summaries."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        directory = storage.list_artist_directory()
        defaults = storage.get_watch_defaults()
        weights = storage.listening_weights()
        overrides = {
            row.plex_rating_key: parse_settings_json(row.settings_json)
            for row, _status in directory
            if row.settings_json
        }
    except (StorageError, SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    if not directory:
        print("No artists yet. Run `encore plex configure` and `encore sync` first.")
        return 0
    for row, status in directory:
        state = status or "unmatched"
        tombstone = " [removed from Plex]" if row.removed_at is not None else ""
        override = overrides.get(row.plex_rating_key, SettingsOverride())
        print(f"{row.name}  key={row.plex_rating_key} [{state}]{tombstone}")
        # F9: plays are shown, never hidden — weighting must be explainable.
        weight = weights.get(row.plex_rating_key, 0.0)
        print(f"    plays={row.play_count} listening-weight={weight:.2f}")
        print(f"    {_describe_override(override, defaults)}")
    return 0


def _cmd_artists_settings(args: argparse.Namespace) -> int:
    """Read-modify-write one artist's settings override."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        current = storage.get_artist_settings(args.artist_key)
        allow_primary = current.allow_primary
        allow_secondary = current.allow_secondary
        muted = current.muted
        mute_until = current.mute_until
        priority = current.priority
        if args.reset_types:
            allow_primary = None
            allow_secondary = None
        else:
            if args.allow_primary is not None:
                allow_primary = parse_primary_types(args.allow_primary)
            if args.allow_secondary is not None:
                allow_secondary = (
                    ()
                    if not args.allow_secondary.strip()
                    else parse_secondary_types(args.allow_secondary)
                )
        if args.unmute:
            muted = False
            mute_until = None
        if args.mute:
            muted = True
            mute_until = None
        if args.mute_until is not None:
            mute_until = date.fromisoformat(args.mute_until)
            muted = None
        if args.priority is not None:
            priority = args.priority
        override = SettingsOverride(
            allow_primary=allow_primary,
            allow_secondary=allow_secondary,
            muted=muted,
            mute_until=mute_until,
            priority=priority,
        )
        stored = storage.set_artist_settings(args.artist_key, override)
        defaults = storage.get_watch_defaults()
    except (StorageError, SettingsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(f"Settings for key {args.artist_key!r}: {_describe_override(stored, defaults)}")
    return 0


def _cmd_artists(args: argparse.Namespace) -> int:
    """Dispatch an `encore artists …` subcommand."""
    handler = _ARTISTS_COMMANDS.get(args.artists_command)
    if handler is None:  # pragma: no cover - argparse rejects unknown subcommands
        return 1
    return handler(args)


def _cmd_settings_show(args: argparse.Namespace) -> int:
    """Print the global watch defaults in force for every artist."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        defaults = storage.get_watch_defaults()
    except (StorageError, SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    primary = ",".join(sorted(defaults.allow_primary or DEFAULT_ALLOWED_PRIMARY))
    secondary = ",".join(sorted(defaults.allow_secondary or ()))
    print(f"Default allowed primary types:   {primary}")
    print(f"Default allowed secondary types: {secondary or '(none)'}")
    print("Per-artist overrides: encore artists settings --artist-key <key> ...")
    return 0


def _cmd_settings_default_types(args: argparse.Namespace) -> int:
    """Update the global default release-type allowlist."""
    try:
        primary = parse_primary_types(args.primary) if args.primary else None
        secondary: tuple[str, ...] | None = None
        if args.secondary is not None:
            secondary = () if not args.secondary.strip() else parse_secondary_types(args.secondary)
        storage = Storage(args.data_dir)
    except (StorageError, SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.set_watch_default_types(allow_primary=primary, allow_secondary=secondary)
        defaults = storage.get_watch_defaults()
    except (StorageError, SettingsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    shown_primary = ",".join(sorted(defaults.allow_primary or DEFAULT_ALLOWED_PRIMARY))
    shown_secondary = ",".join(sorted(defaults.allow_secondary or ()))
    print(f"Default allowed primary types:   {shown_primary}")
    print(f"Default allowed secondary types: {shown_secondary or '(none)'}")
    return 0


def _cmd_settings(args: argparse.Namespace) -> int:
    """Dispatch an `encore settings …` subcommand."""
    handler = _SETTINGS_COMMANDS.get(args.settings_command)
    if handler is None:  # pragma: no cover - argparse rejects unknown subcommands
        return 1
    return handler(args)


def _cmd_recommend(args: argparse.Namespace) -> int:
    """Run one recommendation refresh and print the counts."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    client = ListenBrainzClient()
    try:
        report = refresh_recommendations(storage, client, limit=max(1, args.limit))
    finally:
        client.close()
        storage.close()
    print(
        f"recommend complete: seeds={report.seeds} rows={report.rows_received} "
        f"candidates={report.candidates} stored={report.stored} "
        f"failed_batches={report.batches_failed}"
    )
    if report.degraded:
        print("some batches failed — results may be incomplete until the next refresh.")
    if not report.seeds:
        print("No watched artists to seed from — run `encore sync` and `encore match` first.")
    return 0


def _render_provenance(storage: Storage, provenance_json: str | None) -> str:
    """Render a candidate's provenance as 'similar to X, Y' (names resolved locally)."""
    if not provenance_json:
        return ""
    try:
        payload: object = json.loads(provenance_json)
    except ValueError:
        return ""
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    mbids: list[str] = []
    for entry in sources[:PROVENANCE_LIMIT]:
        if isinstance(entry, dict) and isinstance(entry.get("mbid"), str):
            mbids.append(entry["mbid"])
    names = storage.match_names_by_mbids(mbids)
    ordered = [names[mbid] for mbid in mbids if mbid in names]
    if not ordered:
        return ""
    return "similar to " + ", ".join(ordered)


def _cmd_recs_list(args: argparse.Namespace) -> int:
    """Show recommended artists with their scores and provenance."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        recs = storage.list_recommendations(limit=max(1, args.limit))
    finally:
        storage.close()
    if not recs:
        print("No recommendations yet. Run `encore recommend` once artists are matched.")
        return 0
    # Provenance needs the storage layer, so render before closing it.
    try:
        lines: list[str] = []
        for rec in recs:
            comment = f" ({rec.comment})" if rec.comment else ""
            lines.append(f"{rec.name}{comment}  score={rec.score:.3f}  mbid={rec.mbid}")
            provenance = _render_provenance(storage, rec.provenance_json)
            if provenance:
                lines.append(f"    {provenance}")
    finally:
        storage.close()
    for line in lines:
        print(line)
    return 0


def _cmd_recs_dismiss(args: argparse.Namespace) -> int:
    """Dismiss one recommendation (sticky across refreshes)."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.set_recommendation_status(args.mbid, "dismissed")
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print("Dismissed. It will not reappear in future refreshes.")
    return 0


def _cmd_recs_promote(args: argparse.Namespace) -> int:
    """Promote one recommendation (F8 watches releases from promoted artists)."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage.set_recommendation_status(args.mbid, "promoted")
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print("Promoted. Its upcoming releases join discovery in the next watch cycle.")
    return 0


def _cmd_recs(args: argparse.Namespace) -> int:
    """Dispatch a `recommendations …` subcommand."""
    handler = _RECS_COMMANDS.get(args.recs_command)
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


_ARTISTS_COMMANDS = {
    "list": _cmd_artists_list,
    "settings": _cmd_artists_settings,
}

_SETTINGS_COMMANDS = {
    "show": _cmd_settings_show,
    "default-types": _cmd_settings_default_types,
}

_RECS_COMMANDS = {
    "list": _cmd_recs_list,
    "dismiss": _cmd_recs_dismiss,
    "promote": _cmd_recs_promote,
}


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run the offline checklist and print it; the exit code is the verdict.

    The exit code is the point: 0/1/2 for pass/warn/fail makes this usable
    as a container healthcheck, so it must reflect the checks rather than
    whether the command itself ran. A crash would exit non-zero anyway, which
    is the correct direction.
    """
    results = doctor_run_checks(args.data_dir, check_upstream=args.check_upstream)
    if args.as_json:
        print(json.dumps(doctor_render_json(results), indent=2, sort_keys=True))
    else:
        print(doctor_render_text(results))
    return doctor_exit_code(results)


def _cmd_backup(args: argparse.Namespace) -> int:
    """Write a verified snapshot of the data directory (issue #53)."""
    data_dir = resolve_data_dir(args.data_dir)
    try:
        result = create_backup(data_dir, out_path=args.out, created_at=utcnow().isoformat())
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wrote {result.archive} (schema v{result.schema_version}, encore {result.encore_version})."
    )
    print(f"  {DB_FILENAME}  sha256:{result.digests[DB_FILENAME]}")
    print(f"  {KEY_FILENAME} sha256:{result.digests[KEY_FILENAME]}")
    print(
        "This archive contains the Fernet key, so it can decrypt every stored secret. "
        "Store it as you would the key itself."
    )
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    """Verify an archive and rebuild a data directory from it (issue #53)."""
    data_dir = resolve_data_dir(args.data_dir)
    try:
        result = restore_backup(args.archive, data_dir, force=args.force)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {result.data_dir} from {args.archive}.")
    if result.migrated:
        print(
            f"  schema  v{result.schema_version_in_archive} in the archive, "
            f"migrated forward to v{result.schema_version_after}"
        )
    else:
        print(f"  schema  v{result.schema_version_after}")
    # `skipped` is printed as `skipped`. An operator restoring a database that
    # happens to hold no ciphertext is told the pairing was never tested, not
    # shown a tick the archive did not earn.
    print(f"  key pairing  {result.key_pairing.value}: {result.key_pairing_reason}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Write the portable watch-state document (issue #54)."""
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        document = export_state(storage)
    finally:
        storage.close()
    try:
        destination = write_document(document, args.out)
    except PortableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Wrote {destination}: {len(document['artists'])} artist(s), "
        f"{len(document['recommendations'])} recommendation decision(s), "
        f"{len(document['channels'])} channel(s)."
    )
    print("No Apprise URL, Plex token or feed token is in this file.")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    """Merge a watch-state document into this install (issue #54)."""
    try:
        payload = json.loads(Path(args.document).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read {args.document}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"error: {args.document} is not valid JSON: {exc}", file=sys.stderr)
        return 1
    try:
        document = read_document(payload)
    except PortableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        storage = Storage(args.data_dir)
    except StorageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    strategy = ImportStrategy(args.strategy)
    try:
        plan = import_state(storage, document, strategy=strategy, dry_run=args.dry_run)
    except (PortableError, StorageError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        storage.close()
    print(plan.render(strategy=strategy, applied=not args.dry_run))
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "serve": _cmd_serve,
    "doctor": _cmd_doctor,
    "backup": _cmd_backup,
    "restore": _cmd_restore,
    "export": _cmd_export,
    "import": _cmd_import,
    "sync": _cmd_sync,
    "match": _cmd_match,
    "matches": _cmd_matches,
    "watch": _cmd_watch,
    "notify": _cmd_notify,
    "events": _cmd_events,
    "recommend": _cmd_recommend,
    "recommendations": _cmd_recs,
    "channels": _cmd_channels,
    "feeds": _cmd_feeds,
    "plex": _cmd_plex,
    "artists": _cmd_artists,
    "settings": _cmd_settings,
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
