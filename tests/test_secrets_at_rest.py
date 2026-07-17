"""Encrypted-at-rest proof (docs/adr/0008): plaintext never touches the DB file.

The load-bearing test here greps the *raw database bytes* for the plaintext
token — the acceptance criterion the roadmap sets for F0 — rather than trusting
the ORM to have called the cipher.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from encore.secretstore import SecretCipher
from encore.storage import DB_FILENAME, KEY_FILENAME, Storage, StorageError

# An unmistakable needle: nothing else in the schema or fixtures contains it.
PLEX_TOKEN = "PLEX-TOKEN-needle-3f9a1b7c2d"  # noqa: S105 - deliberately fake, exists to be grepped for


def _raw_db_bytes(data_dir: Path) -> bytes:
    """Concatenate the database file and any WAL/SHM siblings."""
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"{DB_FILENAME}{suffix}"
        if candidate.exists():
            blob += candidate.read_bytes()
    return blob


def test_plex_token_never_appears_in_db_file_plaintext(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    storage.set_plex_credentials("http://plex.local:32400", PLEX_TOKEN)
    assert storage.get_plex_credentials() == ("http://plex.local:32400", PLEX_TOKEN)
    storage.close()

    raw = _raw_db_bytes(tmp_path)
    assert PLEX_TOKEN.encode() not in raw
    # Sanity check that we really read the right bytes: the base URL is not a
    # secret and is stored plaintext, so it must be present.
    assert b"plex.local" in raw


def test_restored_db_without_key_fails_before_creating_replacement(tmp_path: Path) -> None:
    original_dir = tmp_path / "original"
    stolen_dir = tmp_path / "stolen-backup"

    original = Storage(original_dir)
    original.set_plex_credentials("http://plex.local:32400", PLEX_TOKEN)
    original.close()

    # Simulate a stolen backup: every file except the key travels.
    stolen_dir.mkdir()
    for item in original_dir.iterdir():
        if item.name != KEY_FILENAME:
            (stolen_dir / item.name).write_bytes(item.read_bytes())

    database_bytes = (stolen_dir / DB_FILENAME).read_bytes()
    with pytest.raises(
        StorageError, match="Restore the database and key together from the same backup"
    ):
        Storage(stolen_dir)
    assert not (stolen_dir / KEY_FILENAME).exists()
    assert (stolen_dir / DB_FILENAME).read_bytes() == database_bytes


def test_key_file_is_reused_not_regenerated(tmp_path: Path) -> None:
    first = Storage(tmp_path)
    key_bytes = (tmp_path / KEY_FILENAME).read_bytes()
    first.close()

    second = Storage(tmp_path)
    assert (tmp_path / KEY_FILENAME).read_bytes() == key_bytes
    second.close()


@pytest.mark.parametrize("mode", [0o640, 0o604])
def test_existing_key_with_group_or_other_permissions_fails_without_modification(
    tmp_path: Path, mode: int
) -> None:
    storage = Storage(tmp_path)
    storage.close()
    key_path = tmp_path / KEY_FILENAME
    key_bytes = key_path.read_bytes()
    key_path.chmod(mode)

    with pytest.raises(StorageError, match=rf"unsafe mode {mode:04o}"):
        Storage(tmp_path)

    assert key_path.read_bytes() == key_bytes
    assert stat.S_IMODE(key_path.stat().st_mode) == mode


def test_symlink_key_path_is_rejected_without_creating_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    real_key = tmp_path / "real.key"
    SecretCipher.load_or_create(real_key)
    key_path = data_dir / KEY_FILENAME
    key_path.symlink_to(real_key)

    with pytest.raises(StorageError, match="must be a regular file"):
        Storage(data_dir)

    assert key_path.is_symlink()
    assert not (data_dir / DB_FILENAME).exists()


def test_non_regular_key_path_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / KEY_FILENAME).mkdir()

    with pytest.raises(StorageError, match="must be a regular file"):
        Storage(data_dir)


def test_invalid_existing_key_is_never_overwritten(tmp_path: Path) -> None:
    key_path = tmp_path / KEY_FILENAME
    original = b"not-a-fernet-key"
    key_path.write_bytes(original)
    key_path.chmod(0o600)

    with pytest.raises(StorageError, match="is invalid"):
        Storage(tmp_path)

    assert key_path.read_bytes() == original
    assert not (tmp_path / DB_FILENAME).exists()


def test_concurrent_first_start_reads_exclusive_create_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path = tmp_path / KEY_FILENAME
    winner_key = Fernet.generate_key()
    real_open = os.open
    race_injected = False

    def racing_open(path: os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        nonlocal race_injected
        if flags & os.O_EXCL and not race_injected:
            race_injected = True
            winner_fd = real_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(winner_fd, winner_key)
            finally:
                os.close(winner_fd)
            raise FileExistsError
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)
    cipher = SecretCipher.load_or_create(key_path)
    ciphertext = Fernet(winner_key).encrypt(b"winner")

    assert cipher.decrypt(ciphertext) == "winner"
    assert key_path.read_bytes() == winner_key


def test_cipher_roundtrip_and_nonce_uniqueness(tmp_path: Path) -> None:
    cipher = SecretCipher.load_or_create(tmp_path / KEY_FILENAME)
    first = cipher.encrypt("same plaintext")
    second = cipher.encrypt("same plaintext")
    assert cipher.decrypt(first) == "same plaintext"
    assert cipher.decrypt(second) == "same plaintext"
    # Fernet nonces: equal plaintexts must not produce equal ciphertexts.
    assert first != second


def test_unset_credentials_return_none(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    assert storage.get_plex_credentials() is None
    storage.close()
