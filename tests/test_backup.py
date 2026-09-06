"""Tests for `encore backup` / `encore restore` (issue #53).

The four "Done when" clauses of #53 are each a test here, plus the property
this module exists to protect: a check that could not run reports ``skipped``,
never a pass. `test_pairing_is_skipped_not_ok_when_there_is_no_ciphertext` and
`test_restore_output_never_claims_an_untested_pairing_passed` are that guard —
they fail if the tri-state is ever collapsed back into a boolean.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tarfile
import threading
from pathlib import Path

import pytest
from sqlmodel import select

from encore import cli
from encore.backup import (
    ARCHIVE_MEMBERS,
    CIPHERTEXT_PROBES,
    MANIFEST_FILENAME,
    BackupError,
    KeyPairing,
    create_backup,
    read_manifest,
    restore_backup,
)
from encore.models import NotificationChannel
from encore.secretstore import SecretCipher
from encore.storage import DB_FILENAME, KEY_FILENAME, Storage

pytestmark = pytest.mark.recovery

FIXED_CREATED_AT = "2026-09-06T00:00:00+00:00"
SENTINEL_URL = "ntfy://sentinel-token-do-not-log@example.invalid/topic"


def _configured_install(data_dir: Path) -> Storage:
    """Return a data directory holding at least one encrypted column."""
    storage = Storage(data_dir)
    storage.set_plex_credentials("http://plex.invalid:32400", "plex-token-sentinel")
    return storage


def _bare_install(data_dir: Path) -> Storage:
    """Return a data directory that was created but never configured."""
    return Storage(data_dir)


def _rewrite_archive(source: Path, destination: Path, replacements: dict[str, bytes]) -> None:
    """Copy an archive, substituting the payload of named members."""
    with tarfile.open(source, "r") as reader, tarfile.open(destination, "w") as writer:
        for info in reader.getmembers():
            payload = replacements.get(info.name)
            if payload is None:
                stream = reader.extractfile(info)
                assert stream is not None
                payload = stream.read()
            new_info = tarfile.TarInfo(name=info.name)
            new_info.size = len(payload)
            new_info.mode = info.mode
            import io

            writer.addfile(new_info, io.BytesIO(payload))


# --- Done when #1: a backup taken during writes is consistent ------------------


def test_backup_taken_while_writing_passes_integrity_check(tmp_path: Path) -> None:
    """A snapshot taken through the online-backup API while another thread commits.

    This is the clause a filesystem copy of `encore.db` cannot satisfy: with
    WAL on, `cp` can capture a database whose committed rows live in a WAL the
    copy did not take.
    """
    source = tmp_path / "data"
    storage = _configured_install(source)
    stop = threading.Event()
    committed: list[str] = []

    def writer() -> None:
        index = 0
        while not stop.is_set():
            name = f"channel-{index}"
            storage.add_channel(name=name, url=f"ntfy://example.invalid/{index}", mode="instant")
            committed.append(name)
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        # Let the writer get ahead so the copy genuinely overlaps commits.
        while len(committed) < 5:
            pass
        archive = tmp_path / "snapshot.tar"
        # Snapshot the names that had *already committed* before the copy began.
        # Anything committed during the copy may or may not be captured — that
        # is what a point-in-time snapshot means — but nothing committed before
        # it may be missing.
        committed_before_copy = set(committed)
        create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)
    finally:
        stop.set()
        thread.join()
        storage.close()
    assert len(committed_before_copy) >= 5, "the writer never got ahead; the test proved nothing"

    target = tmp_path / "restored"
    restore_backup(archive, target, force=False)

    connection = sqlite3.connect(target / DB_FILENAME)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored = {row[0] for row in connection.execute("SELECT name FROM channels")}
    finally:
        connection.close()

    assert committed_before_copy <= restored


# --- Done when #2: an altered manifest digest refuses and writes nothing --------


def test_restore_refuses_an_altered_digest_and_writes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    with tarfile.open(archive, "r") as reader:
        stream = reader.extractfile(MANIFEST_FILENAME)
        assert stream is not None
        manifest = json.loads(stream.read())
    manifest["digests"][DB_FILENAME] = "0" * 64
    tampered = tmp_path / "tampered.tar"
    _rewrite_archive(
        archive,
        tampered,
        {MANIFEST_FILENAME: (json.dumps(manifest, sort_keys=True) + "\n").encode()},
    )

    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="corrupt or was altered"):
        restore_backup(tampered, target, force=False)
    assert not target.exists(), "a refused restore must not create the data directory"


def test_restore_refuses_an_altered_database_body(tmp_path: Path) -> None:
    """The digest is checked against the bytes in the archive, not against itself."""
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    with tarfile.open(archive, "r") as reader:
        stream = reader.extractfile(DB_FILENAME)
        assert stream is not None
        body = bytearray(stream.read())
    body[-1] ^= 0xFF
    tampered = tmp_path / "tampered.tar"
    _rewrite_archive(archive, tampered, {DB_FILENAME: bytes(body)})

    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="corrupt or was altered"):
        restore_backup(tampered, target, force=False)
    assert not target.exists()


# --- Done when #3: a foreign key is refused before the service can start --------


def test_restore_refuses_a_database_paired_with_a_foreign_key(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    foreign_dir = tmp_path / "other"
    foreign_dir.mkdir()
    foreign_key_path = foreign_dir / KEY_FILENAME
    SecretCipher.load_or_create(foreign_key_path)
    foreign_key = foreign_key_path.read_bytes()

    with tarfile.open(archive, "r") as reader:
        stream = reader.extractfile(MANIFEST_FILENAME)
        assert stream is not None
        manifest = json.loads(stream.read())
    # Update the digest too, so the run fails on the *pairing* check and not
    # merely on the digest — otherwise this test would pass for the wrong reason.
    manifest["digests"][KEY_FILENAME] = hashlib.sha256(foreign_key).hexdigest()
    swapped = tmp_path / "swapped.tar"
    _rewrite_archive(
        archive,
        swapped,
        {
            KEY_FILENAME: foreign_key,
            MANIFEST_FILENAME: (json.dumps(manifest, sort_keys=True) + "\n").encode(),
        },
    )

    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="does not decrypt"):
        restore_backup(swapped, target, force=False)
    assert not target.exists(), "the pairing check must run before anything is written"


# --- Done when #4: two backups of unchanged data differ only in created_at ------


def test_two_backups_of_unchanged_data_have_identical_manifests_bar_created_at(
    tmp_path: Path,
) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()

    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    create_backup(source, out_path=first, created_at="2026-09-06T00:00:00+00:00")
    create_backup(source, out_path=second, created_at="2026-09-07T00:00:00+00:00")

    manifests = []
    for archive in (first, second):
        with tarfile.open(archive, "r") as reader:
            stream = reader.extractfile(MANIFEST_FILENAME)
            assert stream is not None
            manifests.append(json.loads(stream.read()))

    assert manifests[0]["created_at"] != manifests[1]["created_at"]
    for manifest in manifests:
        del manifest["created_at"]
    assert manifests[0] == manifests[1]


# --- The point of the module: a check that could not run is not a pass ----------


def test_pairing_is_skipped_not_ok_when_there_is_no_ciphertext(tmp_path: Path) -> None:
    """An install with nothing encrypted yet cannot prove its key belongs to its db.

    The tempting implementation returns `ok` here, because no decryption
    failed. That is absence rendered as a value: the operator would read a
    green pairing for an archive whose key was never tested against anything.
    """
    source = tmp_path / "data"
    _bare_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    result = restore_backup(archive, tmp_path / "restored", force=False)
    assert result.key_pairing is KeyPairing.SKIPPED
    assert "could not be tested" in result.key_pairing_reason
    assert "not a passing check" in result.key_pairing_reason


def test_pairing_is_ok_only_when_a_ciphertext_actually_decrypted(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    result = restore_backup(archive, tmp_path / "restored", force=False)
    assert result.key_pairing is KeyPairing.OK
    assert "decrypts" in result.key_pairing_reason


def test_restore_output_never_claims_an_untested_pairing_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rendered CLI line for an untested pairing must say `skipped`."""
    source = tmp_path / "data"
    _bare_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    target = tmp_path / "restored"
    assert cli.main(["restore", str(archive), "--data-dir", str(target)]) == 0
    printed = capsys.readouterr().out
    assert "key pairing  skipped" in printed
    assert "key pairing  ok" not in printed


def test_pairing_finds_a_channel_url_when_settings_hold_no_secret(tmp_path: Path) -> None:
    """The probe walks every ciphertext column, not only the Plex token."""
    source = tmp_path / "data"
    storage = Storage(source)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    result = restore_backup(archive, tmp_path / "restored", force=False)
    assert result.key_pairing is KeyPairing.OK
    assert "channel URL" in result.key_pairing_reason


# --- Refusals and archive hygiene ---------------------------------------------


def test_restore_refuses_a_non_empty_target_without_force(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    target = tmp_path / "restored"
    target.mkdir()
    (target / "keep-me").write_text("existing", encoding="utf-8")

    with pytest.raises(BackupError, match="not empty"):
        restore_backup(archive, target, force=False)
    assert (target / "keep-me").read_text(encoding="utf-8") == "existing"

    restore_backup(archive, target, force=True)
    assert (target / DB_FILENAME).exists()


def test_backup_refuses_to_overwrite_an_existing_archive(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    archive.write_bytes(b"not an archive")
    with pytest.raises(BackupError, match="refusing to overwrite"):
        create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)
    assert archive.read_bytes() == b"not an archive"


def test_backup_refuses_a_data_directory_with_no_key(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    (source / KEY_FILENAME).unlink()
    with pytest.raises(BackupError, match="unrecoverable by design"):
        create_backup(source, out_path=tmp_path / "snapshot.tar", created_at=FIXED_CREATED_AT)


def test_restore_rejects_an_archive_with_unexpected_members(tmp_path: Path) -> None:
    """A member outside the fixed set is refused rather than ignored or extracted."""
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    import io

    with tarfile.open(archive, "a") as writer:
        payload = b"../../etc/passwd"
        info = tarfile.TarInfo(name="../escape")
        info.size = len(payload)
        writer.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupError, match="unexpected members"):
        restore_backup(archive, tmp_path / "restored", force=False)


def test_archive_holds_exactly_the_three_expected_members(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)
    with tarfile.open(archive, "r") as reader:
        assert sorted(reader.getnames()) == sorted(ARCHIVE_MEMBERS)
        assert reader.getmember(KEY_FILENAME).mode == 0o600


# --- Manifest validation: missing evidence is not a pass -----------------------


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.pop("digests"), "no digests map"),
        (lambda m: m["digests"].pop(KEY_FILENAME), f"no digest for {KEY_FILENAME}"),
        (lambda m: m["digests"].pop(DB_FILENAME), f"no digest for {DB_FILENAME}"),
        (lambda m: m.pop("schema_version"), "no integer schema_version"),
        (lambda m: m.update(manifest_version=99), "version 1 only"),
    ],
)
def test_manifest_missing_evidence_is_rejected(mutate: object, message: str) -> None:
    manifest = {
        "created_at": FIXED_CREATED_AT,
        "digests": {DB_FILENAME: "a" * 64, KEY_FILENAME: "b" * 64},
        "encore_version": "0.1.0",
        "manifest_version": 1,
        "schema_version": 12,
    }
    mutate(manifest)  # type: ignore[operator]
    with pytest.raises(BackupError, match=message):
        read_manifest(json.dumps(manifest).encode())


def test_manifest_schema_version_true_is_not_an_integer() -> None:
    """`isinstance(True, int)` is True in Python; a bool must not pass as a version."""
    manifest = {
        "created_at": FIXED_CREATED_AT,
        "digests": {DB_FILENAME: "a" * 64, KEY_FILENAME: "b" * 64},
        "encore_version": "0.1.0",
        "manifest_version": 1,
        "schema_version": True,
    }
    with pytest.raises(BackupError, match="no integer schema_version"):
        read_manifest(json.dumps(manifest).encode())


def test_restore_refuses_a_schema_newer_than_this_build(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)

    with tarfile.open(archive, "r") as reader:
        stream = reader.extractfile(MANIFEST_FILENAME)
        assert stream is not None
        manifest = json.loads(stream.read())
    manifest["schema_version"] = 9999
    ahead = tmp_path / "ahead.tar"
    _rewrite_archive(
        archive,
        ahead,
        {MANIFEST_FILENAME: (json.dumps(manifest, sort_keys=True) + "\n").encode()},
    )
    with pytest.raises(BackupError, match="upgrade encore before restoring"):
        restore_backup(ahead, tmp_path / "restored", force=False)


# --- Round trip and secret hygiene --------------------------------------------


def test_round_trip_preserves_every_row_and_the_decryptable_secret(tmp_path: Path) -> None:
    source = tmp_path / "data"
    storage = _configured_install(source)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()

    archive = tmp_path / "snapshot.tar"
    create_backup(source, out_path=archive, created_at=FIXED_CREATED_AT)
    target = tmp_path / "restored"
    restore_backup(archive, target, force=False)

    restored = Storage(target)
    try:
        credentials = restored.get_plex_credentials()
        assert credentials == ("http://plex.invalid:32400", "plex-token-sentinel")
        with restored.session() as session:
            channels = list(session.exec(select(NotificationChannel)).all())
        assert [channel.name for channel in channels] == ["phone"]
        assert restored.cipher.decrypt(channels[0].url_cipher) == SENTINEL_URL
    finally:
        restored.close()


@pytest.mark.no_secrets_in_logs
def test_backup_and_restore_print_no_secret_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither verb prints the key, the Plex token, or a channel URL."""
    source = tmp_path / "data"
    storage = _configured_install(source)
    storage.add_channel(name="phone", url=SENTINEL_URL, mode="instant")
    storage.close()
    key_material = (source / KEY_FILENAME).read_bytes().decode("ascii")

    archive = tmp_path / "snapshot.tar"
    assert cli.main(["backup", "--data-dir", str(source), "--out", str(archive)]) == 0
    target = tmp_path / "restored"
    assert cli.main(["restore", str(archive), "--data-dir", str(target)]) == 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for secret in (key_material, "plex-token-sentinel", SENTINEL_URL, "sentinel-token-do-not-log"):
        assert secret not in combined


def test_cli_backup_reports_a_missing_key_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "data"
    _configured_install(source).close()
    (source / KEY_FILENAME).unlink()
    assert cli.main(["backup", "--data-dir", str(source), "--out", str(tmp_path / "a.tar")]) == 1
    assert "unrecoverable by design" in capsys.readouterr().err


def test_cli_restore_reports_a_missing_archive_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(["restore", str(tmp_path / "absent.tar"), "--data-dir", str(tmp_path / "d")]) == 1
    )
    assert "no archive at" in capsys.readouterr().err


def test_every_probe_names_a_real_column(tmp_path: Path) -> None:
    """The pairing probe degrades to `skipped` if a name drifts — so pin the names.

    `_verify_key_pairing` skips any table or column it does not find. That is
    the right behaviour for an archive from an older schema, but it means a
    rename in `storage.py` would turn the pairing check into a permanent
    `skipped` with nothing to notice it. This test opens a current-schema
    database and fails if any probe no longer resolves.
    """
    source = tmp_path / "data"
    _bare_install(source).close()
    connection = sqlite3.connect(source / DB_FILENAME)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for probe in CIPHERTEXT_PROBES:
            assert probe.table in tables, (
                f"probe names table {probe.table!r}, which the schema does not have"
            )
            columns = {row[1] for row in connection.execute(probe.columns_sql)}
            assert probe.column in columns, (
                f"probe names {probe.table}.{probe.column!r}, which does not exist"
            )
    finally:
        connection.close()


def test_probes_cover_every_ciphertext_column_in_the_schema(tmp_path: Path) -> None:
    """A new `*_cipher` column must be added to the probe list, not silently missed.

    Without this, adding an encrypted column to a table the probes do not name
    would leave an install whose *only* secret lives there reporting `skipped`
    forever.
    """
    source = tmp_path / "data"
    _bare_install(source).close()
    connection = sqlite3.connect(source / DB_FILENAME)
    try:
        found: set[tuple[str, str]] = set()
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in connection.execute(f"PRAGMA table_info({table})"):
                if row[1].endswith("_cipher"):
                    found.add((table, row[1]))
    finally:
        connection.close()
    probed = {(probe.table, probe.column) for probe in CIPHERTEXT_PROBES}
    assert found == probed, (
        "the schema's ciphertext columns and the pairing probes have diverged; "
        f"unprobed: {sorted(found - probed)}, stale probes: {sorted(probed - found)}"
    )


def test_probe_sql_matches_its_declared_table_and_column() -> None:
    """The literal statements and the declared names must not drift apart.

    The SQL is written out rather than interpolated, which removes the
    injection site but creates a second copy of each name. This holds the two
    copies together by *parsing* each statement — a probe whose `select_sql`
    read a different column than it declares would silently prove the pairing
    against the wrong thing, or against nothing.
    """
    for probe in CIPHERTEXT_PROBES:
        pragma = re.fullmatch(r"PRAGMA table_info\((\w+)\)", probe.columns_sql)
        assert pragma is not None, f"unparseable columns_sql: {probe.columns_sql!r}"
        assert pragma.group(1) == probe.table

        # The back-reference is the point: the column named in the projection
        # must be the same one the predicate filters on.
        selection = re.fullmatch(
            r"SELECT (\w+) FROM (\w+) WHERE \1 IS NOT NULL LIMIT 1", probe.select_sql
        )
        assert selection is not None, f"unparseable select_sql: {probe.select_sql!r}"
        assert selection.group(1) == probe.column
        assert selection.group(2) == probe.table
