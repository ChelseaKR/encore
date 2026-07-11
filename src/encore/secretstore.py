"""Fernet secrets-at-rest cipher (docs/adr/0008).

The key lives in a file *beside* the database — never inside it — created with
mode 0600 on first use. This protects **copies** of the data (a stolen backup,
a misdirected volume snapshot); it explicitly does not defend against a
root-level attacker on the live host, who can read the key file directly. That
boundary is stated honestly in ADR-0008 and the DPIA rather than implied away.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["SecretCipher", "SecretDecryptionError"]


class SecretDecryptionError(Exception):
    """A stored ciphertext could not be decrypted with the configured key.

    The usual cause is a database restored without its companion key file —
    the backup guidance (ADR-0008 consequences) is to copy both, and this
    error message says so instead of surfacing a bare cryptography traceback.
    """


class SecretCipher:
    """Encrypts and decrypts secret columns with a Fernet key stored on disk."""

    def __init__(self, key: bytes) -> None:
        """Wrap a raw url-safe base64 Fernet key (normally via ``load_or_create``)."""
        self._fernet = Fernet(key)

    @classmethod
    def load_or_create(cls, key_path: Path) -> SecretCipher:
        """Load the key at ``key_path``, generating it (mode 0600) if absent."""
        if key_path.exists():
            return cls(key_path.read_bytes().strip())
        key = Fernet.generate_key()
        # O_EXCL: refuse to clobber a file that appeared between the check and
        # the create; 0o600 from the first byte, not chmod'ed after the fact.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return cls(key)

    def encrypt(self, plaintext: str) -> bytes:
        """Return the Fernet ciphertext for ``plaintext``."""
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, ciphertext: bytes) -> str:
        """Return the plaintext for ``ciphertext``.

        Raises:
            SecretDecryptionError: the ciphertext does not verify under this
                key — e.g. a database file restored without its key file.
        """
        try:
            return self._fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise SecretDecryptionError(
                "cannot decrypt stored secret: the Fernet key beside the database "
                "does not match the one that encrypted it. If this database was "
                "restored from a backup, restore its key file too (docs/adr/0008)."
            ) from exc
