"""Consistent, verified snapshots of the ``/data`` unit (issue #53).

ADR-0005 makes the deployment model "one SQLite file in one mounted volume",
and ADR-0008 puts a Fernet key file *beside* that database. The README has
therefore always described backup as a manual procedure with two sharp edges
it documents itself: copying a live ``encore.db`` can capture a torn WAL, and
copying a database without its companion key produces an archive that is
unrecoverable by design. This module turns that prose into two verbs.

``create_backup`` takes the database through SQLite's online-backup API, so
WAL state is folded into a single consistent file without stopping the
container, and writes ``encore.db``, ``encore.key`` and ``manifest.json``
into one tar. ``restore_backup`` verifies the manifest before it writes
anything, then verifies that the key in the archive is the key that encrypted
the database in the archive.

**A check that could not run reports ``skipped``, never ``ok``.** The
key/database pairing is proved by decrypting a stored ciphertext, and a fresh
install has no ciphertext to decrypt. Reporting that pairing as "verified"
because nothing contradicted it would be the portfolio's dominant defect —
absence rendered as a value — in the one place an operator is relying on the
answer. ``KeyPairing`` is therefore tri-state, ``restore_backup`` renders the
untested case as ``skipped`` with the reason, and only ``MISMATCHED`` is
fatal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

from encore import __version__
from encore.secretstore import SecretCipher, SecretDecryptionError, SecretKeyError
from encore.storage import DB_FILENAME, KEY_FILENAME, MIGRATIONS, Storage, StorageError

__all__ = [
    "ARCHIVE_MEMBERS",
    "CIPHERTEXT_PROBES",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "BackupError",
    "BackupResult",
    "CiphertextProbe",
    "KeyPairing",
    "RestoreResult",
    "create_backup",
    "read_manifest",
    "restore_backup",
]

MANIFEST_FILENAME = "manifest.json"
MANIFEST_VERSION = 1
ARCHIVE_MEMBERS = (DB_FILENAME, KEY_FILENAME, MANIFEST_FILENAME)

# Members are read into memory for digesting; a data directory that does not
# fit is a different deployment than ADR-0005 describes. The cap exists so a
# hostile archive cannot exhaust memory before its digests are ever checked.
_MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
_DIGEST_CHUNK = 1024 * 1024


class BackupError(Exception):
    """An archive could not be written, or could not be trusted enough to restore."""


class KeyPairing(StrEnum):
    """Whether the archive's key is provably the key that encrypted its database.

    ``SKIPPED`` is not a pass. It means the database held no ciphertext to
    decrypt, so the question was never asked; the caller is told so in those
    words rather than being handed a green tick it did not earn.
    """

    OK = "ok"
    MISMATCHED = "mismatched"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class BackupResult:
    """What ``create_backup`` wrote."""

    archive: Path
    schema_version: int
    digests: dict[str, str]
    encore_version: str


@dataclass(frozen=True)
class RestoreResult:
    """What ``restore_backup`` restored, and what it could and could not prove."""

    data_dir: Path
    schema_version_in_archive: int
    schema_version_after: int
    migrated: bool
    key_pairing: KeyPairing
    key_pairing_reason: str


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_DIGEST_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_version(db_path: Path) -> int:
    """Return the database's ``PRAGMA user_version``."""
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _copy_database(source: Path, destination: Path) -> None:
    """Copy a live database through SQLite's online-backup API.

    ``Connection.backup`` holds a read transaction for the duration of the
    copy, so the destination is a single consistent image with WAL content
    folded in — the guarantee a filesystem copy of ``encore.db`` alone cannot
    make while the schedulers are writing.
    """
    if not source.exists():
        raise BackupError(f"no database at {source}; nothing to back up")
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open database {source} for backup: {exc}") from exc
    try:
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            # The copy is a fresh file with no WAL of its own. Checkpointing is
            # unnecessary, but the integrity check is cheap and turns "the copy
            # ran" into "the copy is readable", which is what the archive claims.
            row = destination_connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise BackupError(
                    f"the database copy failed its integrity check: {row[0] if row else 'no result'}"
                )
        finally:
            destination_connection.close()
    except sqlite3.Error as exc:
        raise BackupError(f"cannot copy database {source}: {exc}") from exc
    finally:
        source_connection.close()


def _tar_member(name: str, path: Path) -> tarfile.TarInfo:
    """Build a deterministic tar header for one archive member.

    Ownership, mtime and name are pinned so two backups of unchanged data
    differ only where the manifest says they differ (``created_at``). The key
    keeps mode 0600 inside the archive; the database is 0600 too, because the
    archive that carries the key is exactly as sensitive as the key.
    """
    info = tarfile.TarInfo(name=name)
    info.size = path.stat().st_size
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def create_backup(
    data_dir: str | Path | None = None,
    *,
    out_path: str | Path,
    created_at: str,
) -> BackupResult:
    """Write one consistent, digest-verified snapshot of ``data_dir`` to ``out_path``.

    ``created_at`` is supplied by the caller rather than read from the clock
    so that the "two backups of unchanged data differ only in ``created_at``"
    property is testable without patching time.

    Raises:
        BackupError: the data directory is incomplete, the database cannot be
            copied consistently, or the archive cannot be written.
    """
    source_dir = Path(data_dir) if data_dir is not None else Path("data")
    db_source = source_dir / DB_FILENAME
    key_source = source_dir / KEY_FILENAME
    if not key_source.exists():
        raise BackupError(
            f"no Fernet key at {key_source}; a database backed up without its key is "
            "unrecoverable by design (docs/adr/0008)"
        )
    destination = Path(out_path)
    if destination.exists():
        raise BackupError(f"refusing to overwrite an existing archive at {destination}")

    with tempfile.TemporaryDirectory(prefix="encore-backup-") as staging_name:
        staging = Path(staging_name)
        db_staged = staging / DB_FILENAME
        _copy_database(db_source, db_staged)
        key_staged = staging / KEY_FILENAME
        shutil.copyfile(key_source, key_staged)
        os.chmod(key_staged, 0o600)

        schema_version = _schema_version(db_staged)
        digests = {
            DB_FILENAME: _sha256_file(db_staged),
            KEY_FILENAME: _sha256_file(key_staged),
        }
        manifest = {
            "created_at": created_at,
            "digests": digests,
            "encore_version": __version__,
            "manifest_version": MANIFEST_VERSION,
            "schema_version": schema_version,
        }
        manifest_staged = staging / MANIFEST_FILENAME
        manifest_staged.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )

        # Write to a sibling temporary path and rename, so an interrupted run
        # never leaves a truncated archive at the operator's chosen filename.
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, partial_name = tempfile.mkstemp(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".partial"
        )
        os.close(handle_fd)
        partial = Path(partial_name)
        try:
            with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as archive:
                for member in ARCHIVE_MEMBERS:
                    staged = staging / member
                    with staged.open("rb") as stream:
                        archive.addfile(_tar_member(member, staged), stream)
            os.chmod(partial, 0o600)
            partial.replace(destination)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise BackupError(f"cannot write archive {destination}: {exc}") from exc

    return BackupResult(
        archive=destination,
        schema_version=schema_version,
        digests=digests,
        encore_version=__version__,
    )


def _extract_member(archive: tarfile.TarFile, name: str) -> bytes:
    """Read one expected member, rejecting anything that is not a plain file.

    Nothing here is written to disk: the archive is fully validated in memory
    before the target directory is touched, which is what lets a failed
    restore leave the operator's data directory untouched.
    """
    try:
        info = archive.getmember(name)
    except KeyError as exc:
        raise BackupError(f"archive is missing {name}") from exc
    if not info.isfile():
        raise BackupError(f"archive member {name} is not a regular file")
    if info.size > _MAX_MEMBER_BYTES:
        raise BackupError(f"archive member {name} is implausibly large ({info.size} bytes)")
    stream: IO[bytes] | None = archive.extractfile(info)
    if stream is None:  # pragma: no cover - isfile() already excludes this
        raise BackupError(f"archive member {name} could not be read")
    with stream:
        return stream.read()


def read_manifest(payload: bytes) -> dict[str, Any]:
    """Parse and structurally validate a manifest document.

    Every field the restore relies on is required. A manifest missing its
    ``digests`` map is rejected rather than treated as "no digests to check":
    an unverifiable archive must not restore more easily than a corrupt one.
    """
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"archive manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("archive manifest is not a JSON object")
    version = manifest.get("manifest_version")
    if version != MANIFEST_VERSION:
        raise BackupError(
            f"archive manifest is version {version!r}; this build reads version "
            f"{MANIFEST_VERSION} only"
        )
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise BackupError("archive manifest has no integer schema_version")
    digests = manifest.get("digests")
    if not isinstance(digests, dict):
        raise BackupError("archive manifest has no digests map")
    for member in (DB_FILENAME, KEY_FILENAME):
        recorded = digests.get(member)
        if not isinstance(recorded, str) or not recorded:
            raise BackupError(f"archive manifest records no digest for {member}")
    return manifest


# Every ciphertext column in the schema, as (table, column, human description).
#
# A name that drifts out of the schema would make ``_verify_key_pairing`` fall
# through to ``SKIPPED`` for every archive — safe, but silently useless. That
# is what `test_every_probe_names_a_real_column` is for: it opens a
# current-schema database and fails if any entry here no longer resolves.
#
# The SQL is written out in full rather than composed from ``table`` and
# ``column``. Interpolating even a module constant into a statement is the
# shape the SAST rule blocks, and a probe list is exactly the kind of table a
# later change turns into a parameter; keeping the statements literal means
# there is no interpolation site to grow one. ``test_probe_sql_matches_its_
# declared_table_and_column`` holds the literal against the declared names so
# the two halves cannot drift apart.
@dataclass(frozen=True)
class CiphertextProbe:
    """One ciphertext column the key/database pairing can be proved against."""

    table: str
    column: str
    description: str
    columns_sql: str
    select_sql: str


CIPHERTEXT_PROBES: tuple[CiphertextProbe, ...] = (
    CiphertextProbe(
        table="settings",
        column="plex_token_cipher",
        description="the Plex token",
        columns_sql="PRAGMA table_info(settings)",
        select_sql=(
            "SELECT plex_token_cipher FROM settings WHERE plex_token_cipher IS NOT NULL LIMIT 1"
        ),
    ),
    CiphertextProbe(
        table="settings",
        column="feed_token_cipher",
        description="the feed token",
        columns_sql="PRAGMA table_info(settings)",
        select_sql=(
            "SELECT feed_token_cipher FROM settings WHERE feed_token_cipher IS NOT NULL LIMIT 1"
        ),
    ),
    CiphertextProbe(
        table="channels",
        column="url_cipher",
        description="a channel URL",
        columns_sql="PRAGMA table_info(channels)",
        select_sql="SELECT url_cipher FROM channels WHERE url_cipher IS NOT NULL LIMIT 1",
    ),
)


def _verify_key_pairing(db_path: Path, key_path: Path) -> tuple[KeyPairing, str]:
    """Prove the archive's key decrypts the archive's database, or say why not.

    Returns ``SKIPPED`` when the database holds no ciphertext at all — a fresh
    install that was never configured. That is a real and common state, and it
    is reported as untested rather than as a pass.
    """
    try:
        cipher = SecretCipher.load_or_create(key_path)
    except SecretKeyError as exc:
        return KeyPairing.MISMATCHED, f"the archive's Fernet key is unusable: {exc}"

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for probe in CIPHERTEXT_PROBES:
            if probe.table not in tables:
                continue
            columns = {row[1] for row in connection.execute(probe.columns_sql)}
            if probe.column not in columns:
                continue
            row = connection.execute(probe.select_sql).fetchone()
            if row is None or row[0] is None:
                continue
            try:
                cipher.decrypt(bytes(row[0]))
            except SecretDecryptionError:
                return (
                    KeyPairing.MISMATCHED,
                    f"the archive's Fernet key does not decrypt {probe.description} in the "
                    "archive's database; these two files came from different installs",
                )
            return KeyPairing.OK, f"the archive's Fernet key decrypts {probe.description}"
    except sqlite3.Error as exc:
        raise BackupError(f"cannot read the archive's database: {exc}") from exc
    finally:
        connection.close()
    return (
        KeyPairing.SKIPPED,
        "the archive's database holds no encrypted column, so the key/database "
        "pairing could not be tested — this is not a passing check",
    )


def _target_is_empty(data_dir: Path) -> bool:
    """Report whether a restore target holds nothing that would be overwritten."""
    if not data_dir.exists():
        return True
    return not any(data_dir.iterdir())


def restore_backup(
    archive_path: str | Path,
    data_dir: str | Path,
    *,
    force: bool = False,
) -> RestoreResult:
    """Rebuild ``data_dir`` from ``archive_path``, verifying before writing.

    The order matters and is the point of the verb: the manifest, the digests
    and the key/database pairing are all checked against a staging copy, and
    only then is anything placed in ``data_dir``. A failure at any step leaves
    the operator's directory exactly as it was.

    Raises:
        BackupError: the archive is malformed, its digests do not match, its
            key does not belong to its database, or the target is non-empty
            without ``force``.
    """
    source = Path(archive_path)
    target = Path(data_dir)
    if not source.exists():
        raise BackupError(f"no archive at {source}")
    if not force and not _target_is_empty(target):
        raise BackupError(
            f"data directory {target} is not empty; refusing to overwrite it. "
            "Pass --force if replacing its contents is what you intend."
        )

    with tempfile.TemporaryDirectory(prefix="encore-restore-") as staging_name:
        staging = Path(staging_name)
        try:
            with tarfile.open(source, "r") as archive:
                unexpected = sorted(set(archive.getnames()) - set(ARCHIVE_MEMBERS))
                if unexpected:
                    raise BackupError(
                        f"archive {source} carries unexpected members: {', '.join(unexpected)}"
                    )
                payloads = {member: _extract_member(archive, member) for member in ARCHIVE_MEMBERS}
        except (tarfile.TarError, OSError) as exc:
            raise BackupError(f"cannot read archive {source}: {exc}") from exc

        manifest = read_manifest(payloads[MANIFEST_FILENAME])
        digests: dict[str, Any] = manifest["digests"]
        for member in (DB_FILENAME, KEY_FILENAME):
            actual = hashlib.sha256(payloads[member]).hexdigest()
            if actual != digests[member]:
                raise BackupError(
                    f"archive {source} is corrupt or was altered: {member} hashes to "
                    f"{actual}, but the manifest records {digests[member]}. Nothing was written."
                )

        schema_in_archive = int(manifest["schema_version"])
        if schema_in_archive > len(MIGRATIONS):
            raise BackupError(
                f"archive holds a v{schema_in_archive} schema, but this build of encore "
                f"only understands up to v{len(MIGRATIONS)} — upgrade encore before restoring"
            )

        staged_db = staging / DB_FILENAME
        staged_key = staging / KEY_FILENAME
        staged_db.write_bytes(payloads[DB_FILENAME])
        # Written through a 0600 descriptor rather than chmod'ed afterwards, so
        # the key is never briefly group-readable inside the staging directory.
        key_fd = os.open(staged_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(key_fd, payloads[KEY_FILENAME])
        finally:
            os.close(key_fd)

        pairing, reason = _verify_key_pairing(staged_db, staged_key)
        if pairing is KeyPairing.MISMATCHED:
            raise BackupError(f"{reason}. Nothing was written to {target}.")

        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(staged_db, target / DB_FILENAME)
        os.chmod(target / DB_FILENAME, 0o600)
        restored_key = target / KEY_FILENAME
        restored_key.unlink(missing_ok=True)
        key_fd = os.open(restored_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(key_fd, payloads[KEY_FILENAME])
        finally:
            os.close(key_fd)

    # Opening Storage runs the ordered forward migrations, so an archive taken
    # by an older build lands usable rather than merely present.
    try:
        storage = Storage(target)
    except StorageError as exc:
        raise BackupError(f"restored data directory is not usable: {exc}") from exc
    storage.close()
    schema_after = _schema_version(target / DB_FILENAME)

    return RestoreResult(
        data_dir=target,
        schema_version_in_archive=schema_in_archive,
        schema_version_after=schema_after,
        migrated=schema_after != schema_in_archive,
        key_pairing=pairing,
        key_pairing_reason=reason,
    )
