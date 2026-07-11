"""Encrypted-at-rest proof (docs/adr/0008): plaintext never touches the DB file.

The load-bearing test here greps the *raw database bytes* for the plaintext
token — the acceptance criterion the roadmap sets for F0 — rather than trusting
the ORM to have called the cipher.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from encore.secretstore import SecretCipher, SecretDecryptionError
from encore.storage import DB_FILENAME, KEY_FILENAME, Storage

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


def test_restored_db_without_its_key_cannot_decrypt(tmp_path: Path) -> None:
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

    stolen = Storage(stolen_dir)  # fresh dir grows a *new* key file
    with pytest.raises(SecretDecryptionError, match="restore its key file"):
        stolen.get_plex_credentials()
    stolen.close()


def test_key_file_is_reused_not_regenerated(tmp_path: Path) -> None:
    first = Storage(tmp_path)
    key_bytes = (tmp_path / KEY_FILENAME).read_bytes()
    first.close()

    second = Storage(tmp_path)
    assert (tmp_path / KEY_FILENAME).read_bytes() == key_bytes
    second.close()


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
