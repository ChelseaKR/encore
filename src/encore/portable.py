"""A portable, secret-free description of what an install watches (issue #54).

``encore backup`` (issue #53) is a byte-level copy of one install: the whole
data directory, the key included, restorable only by the same build. This is
the other half — a cross-version, human-readable, diffable document of what an
install *watches and prefers*, which survives a schema migration and can be
read, edited and shared by a person.

Two properties define the format, and both are enforced by tests rather than
by care.

**Secrets are structurally absent.** The exporter never reads a ``*_cipher``
column, and it never reads ``NotificationChannel.last_error`` either. That
second exclusion is not obvious and is the more dangerous one:
``notify/engine.py`` records ``error=str(exc)`` on a failed send, an Apprise
URL *is* a credential (``ntfy://user:pass@host``), and an Apprise exception is
entirely capable of quoting the URL it failed on. A field that usually holds a
harmless message and occasionally holds a credential is a credential field.
:data:`CHANNEL_EXPORTED_FIELDS` is an allow-list for exactly this reason —
here a new column must be added deliberately, which is the opposite of the
reasoning the receipt-style digests want, because here the risk is *including*
something rather than omitting it.

**Absence is exported as absence.** An artist with no per-artist override
exports no override — not a copy of the resolved defaults. The distinction is
the whole point of a partial override layer: writing the effective policy into
the document would turn every artist into an artist explicitly pinned to
today's defaults, so a later change to the defaults would silently stop
applying to them. :func:`export_state` therefore serializes
``canonical_override_json`` output, which is ``None`` for an empty layer, and
omits the key entirely rather than emitting ``null`` or ``{}``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from encore.artistsettings import (
    SettingsError,
    canonical_override_json,
    parse_settings_json,
)
from encore.storage import Storage, StorageError

__all__ = [
    "CHANNEL_EXPORTED_FIELDS",
    "SCHEMA_VERSION",
    "SECRET_BEARING_SUBSTRINGS",
    "ImportPlan",
    "ImportStrategy",
    "PortableError",
    "export_state",
    "import_state",
    "read_document",
]

SCHEMA_VERSION = 1

# The only `NotificationChannel` columns that leave this install.
#
# An allow-list, deliberately: the failure mode here is *including* a column
# that turns out to carry a secret, and a deny-list would let a column added
# next year ride out by default. `last_error` is excluded by name and by test
# — see the module docstring.
CHANNEL_EXPORTED_FIELDS = ("name", "mode", "enabled", "digest_interval_hours")

# Substrings that mark a value as secret-bearing wherever they appear. Used by
# `import_state` to refuse a document somebody has hand-edited a credential
# into, and by the export tests as a structural check on the output.
SECRET_BEARING_SUBSTRINGS = ("cipher", "token", "password", "secret")


class PortableError(Exception):
    """A watch-state document cannot be written, read, or trusted."""


class ImportStrategy(StrEnum):
    """What to do when the file and the install both describe the same thing."""

    KEEP_LOCAL = "keep-local"
    PREFER_FILE = "prefer-file"


@dataclass(frozen=True)
class ImportPlan:
    """What an import would do, or did. Printed by ``--dry-run`` unchanged."""

    added: tuple[str, ...]
    matched: tuple[str, ...]
    conflicts: tuple[str, ...]
    skipped: tuple[str, ...]

    def render(self, *, strategy: ImportStrategy, applied: bool) -> str:
        """Render the plan as the text ``export``/``import`` print."""
        verb = "Imported" if applied else "Would import"
        lines = [
            f"{verb} under strategy {strategy.value}:",
            f"  added      {len(self.added)}",
            f"  matched    {len(self.matched)}   (already identical here)",
            f"  conflicts  {len(self.conflicts)}",
            f"  skipped    {len(self.skipped)}",
        ]
        for label, rows in (
            ("added", self.added),
            ("conflict", self.conflicts),
            ("skipped", self.skipped),
        ):
            for row in rows:
                lines.append(f"  {label}: {row}")
        return "\n".join(lines)


def _artist_entry(
    key: str, name: str, mbid: str | None, source: str, override_json: str | None
) -> dict[str, Any]:
    """One artist row, with an absent override left absent."""
    entry: dict[str, Any] = {"key": key, "name": name, "source": source}
    if mbid is not None:
        entry["mbid"] = mbid
    if override_json is not None:
        # Stored canonical JSON is re-parsed into a mapping so the document is
        # readable and diffable rather than carrying an embedded JSON string.
        entry["settings"] = json.loads(override_json)
    return entry


def export_state(storage: Storage) -> dict[str, Any]:
    """Build the portable document for ``storage``.

    Reads only plaintext columns. Every collection is sorted by a stable key
    so two exports of unchanged state are byte-identical.
    """
    matches = {match.artist_key: match for match in storage.list_artist_matches()}
    artists: list[dict[str, Any]] = []
    for row, _status in storage.list_artist_directory():
        match = matches.get(row.plex_rating_key)
        mbid = match.mbid if match is not None and match.status in ("auto", "manual") else None
        artists.append(
            _artist_entry(
                key=row.plex_rating_key,
                name=row.name,
                mbid=mbid,
                source="plex",
                override_json=row.settings_json,
            )
        )

    recommendations: list[dict[str, Any]] = []
    for status in ("dismissed", "promoted"):
        for recommendation in storage.list_recommendations(status=status, limit=100_000):
            recommendations.append(
                {
                    "mbid": recommendation.mbid,
                    "name": recommendation.name,
                    "status": status,
                }
            )

    channels: list[dict[str, Any]] = []
    for channel in storage.list_channels():
        channels.append({field: getattr(channel, field) for field in CHANNEL_EXPORTED_FIELDS})

    defaults_json = canonical_override_json(storage.get_watch_defaults())
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artists": sorted(artists, key=lambda entry: entry["key"]),
        "recommendations": sorted(recommendations, key=lambda entry: entry["mbid"]),
        "channels": sorted(channels, key=lambda entry: entry["name"]),
    }
    # Same rule as an artist's override: no global defaults configured means
    # the key is absent, not a snapshot of the built-in policy.
    if defaults_json is not None:
        document["watch_defaults"] = json.loads(defaults_json)
    return document


def render(document: dict[str, Any]) -> str:
    """Return the exact bytes ``encore export`` writes."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _find_secret_bearing(node: Any, path: str = "") -> str | None:
    """Return the first path in ``node`` whose key looks secret-bearing, if any."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            lowered = str(key).casefold()
            if any(marker in lowered for marker in SECRET_BEARING_SUBSTRINGS):
                return here
            found = _find_secret_bearing(value, here)
            if found is not None:
                return found
    elif isinstance(node, list):
        for position, value in enumerate(node):
            found = _find_secret_bearing(value, f"{path}[{position}]")
            if found is not None:
                return found
    return None


def _looks_like_an_apprise_url(value: Any) -> bool:
    """Whether a string carries a scheme and credentials, i.e. is a channel URL."""
    return isinstance(value, str) and "://" in value and "@" in value


def _check_no_secret_material(payload: dict[str, Any]) -> None:
    """Refuse a document carrying credential material, by key name and by value.

    By *name* rather than by value for the structural half: a key called
    anything matching :data:`SECRET_BEARING_SUBSTRINGS` is refused wherever it
    appears, because a document that has grown a ``url_cipher`` did not come
    from this exporter, and importing it would mean writing credential
    material this install cannot decrypt into a column meant to hold one it
    can.
    """
    offending = _find_secret_bearing(payload)
    if offending is not None:
        raise PortableError(
            f"watch-state document carries a secret-bearing field at {offending}; "
            "an export from this build never contains one, and importing it would "
            "write credential material into this install"
        )
    for entry in payload.get("channels", []):
        for value in (entry or {}).values():
            if _looks_like_an_apprise_url(value):
                raise PortableError(
                    "watch-state document's channel section carries what looks like "
                    "an Apprise URL; channel URLs are credentials and are never "
                    "exported or imported"
                )


def _check_artist_entries(payload: dict[str, Any]) -> None:
    """Every artist entry must be identifiable and carry settings that parse."""
    for entry in payload.get("artists", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise PortableError("an artist entry has no key")
        settings = entry.get("settings")
        if settings is None:
            continue
        try:
            parse_settings_json(json.dumps(settings))
        except SettingsError as exc:
            raise PortableError(
                f"artist {entry['key']!r} carries invalid watch settings: {exc}"
            ) from exc


def read_document(payload: Any) -> dict[str, Any]:
    """Validate a watch-state document, refusing one carrying secret material.

    Raises:
        PortableError: the document is malformed, from an unreadable schema
            version, or carries secret material.
    """
    if not isinstance(payload, dict):
        raise PortableError("watch-state document is not a JSON object")
    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PortableError(
            f"watch-state document is schema version {version!r}; this build of "
            f"encore reads version {SCHEMA_VERSION} only"
        )
    for section in ("artists", "recommendations", "channels"):
        if not isinstance(payload.get(section, []), list):
            raise PortableError(f"watch-state document's {section!r} is not a list")
    _check_no_secret_material(payload)
    _check_artist_entries(payload)
    return payload


def _override_matches(storage: Storage, key: str, wanted: Any) -> bool | None:
    """Whether the install's stored override equals ``wanted``; ``None`` if absent."""
    try:
        current = storage.get_artist_settings(key)
    except StorageError:
        return None
    current_json = canonical_override_json(current)
    current_value: Any = json.loads(current_json) if current_json is not None else None
    return bool(current_value == wanted)


def _merge_artists(
    storage: Storage,
    document: dict[str, Any],
    strategy: ImportStrategy,
    dry_run: bool,
    added: list[str],
    matched: list[str],
    conflicts: list[str],
    skipped: list[str],
) -> None:
    """Apply the file's per-artist overrides, recording every outcome."""
    for entry in document.get("artists", []):
        key = entry["key"]
        wanted = entry.get("settings")
        current = _override_matches(storage, key, wanted)
        if current is None:
            skipped.append(f"{key}: not present in this install (never synced from Plex)")
            continue
        if current:
            matched.append(key)
            continue
        # The install has this artist and disagrees about its settings.
        if strategy is ImportStrategy.KEEP_LOCAL:
            conflicts.append(f"{key}: local settings kept; the file differs")
            continue
        if not dry_run:
            override = parse_settings_json(json.dumps(wanted) if wanted is not None else None)
            storage.set_artist_settings(key, override)
        added.append(f"{key}: settings taken from the file")


def import_state(
    storage: Storage,
    document: dict[str, Any],
    *,
    strategy: ImportStrategy = ImportStrategy.KEEP_LOCAL,
    dry_run: bool = False,
) -> ImportPlan:
    """Merge a watch-state document into ``storage``.

    Only settings this install can act on are applied: an artist the file
    names but this install has never synced cannot be given a per-artist
    override, because there is no row to hang it on. Those are reported as
    ``skipped`` with the reason rather than silently dropped — an import that
    says "12 added" while 40 entries went nowhere is the same defect as a
    report of zero for a read that failed.

    Nothing here opens a socket. MBIDs are stored as-is; matching is the next
    scheduled ``mb-match`` pass's job.
    """
    added: list[str] = []
    matched: list[str] = []
    conflicts: list[str] = []
    skipped: list[str] = []

    _merge_artists(storage, document, strategy, dry_run, added, matched, conflicts, skipped)

    # Channels are exported for a reader and cannot be imported. The Apprise
    # URL *is* the channel — it carries the credential — and it is deliberately
    # not in this document, so there is nothing here to recreate a channel
    # from. Saying so is the point: an import that silently ignored the
    # channel section would let somebody move to new hardware believing their
    # notifications came with them.
    for entry in document.get("channels", []):
        name = entry.get("name")
        skipped.append(
            f"channel {name!r}: not imported — an Apprise URL is a credential and is "
            "never in this document. Recreate it with `encore channels add`."
        )

    for entry in document.get("recommendations", []):
        mbid = entry.get("mbid")
        status = entry.get("status")
        if status not in ("dismissed", "promoted"):
            skipped.append(f"{mbid}: unknown recommendation status {status!r}")
            continue
        try:
            if not dry_run:
                storage.set_recommendation_status(str(mbid), str(status))
            added.append(f"{mbid}: recommendation {status}")
        except StorageError:
            skipped.append(f"{mbid}: no recommendation row here; refresh recommendations first")

    return ImportPlan(
        added=tuple(added),
        matched=tuple(matched),
        conflicts=tuple(conflicts),
        skipped=tuple(skipped),
    )


def write_document(document: dict[str, Any], out_path: str | Path) -> Path:
    """Write the document to ``out_path``, refusing to clobber."""
    destination = Path(out_path)
    if destination.exists():
        raise PortableError(f"refusing to overwrite an existing file at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(document), encoding="utf-8")
    return destination
