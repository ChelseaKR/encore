"""Fernet secrets-at-rest cipher (docs/adr/0008).

The key lives in a file *beside* the database — never inside it — created with
mode 0600 on first use. This protects a database-only copy when the companion
key was stored separately and remains protected. It does not protect a copy of
the whole ``/data`` volume or backup (which contains both files), or a root-level
attacker on the live host. That boundary is stated honestly in ADR-0008 and the
DPIA rather than implied away.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["SecretCipher", "SecretDecryptionError", "SecretKeyError"]


class SecretKeyError(Exception):
    """The on-disk Fernet key is missing, unsafe, unreadable, or invalid."""


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
        """Load a safe key, or atomically create it for a true first start.

        Existing paths are never replaced or chmod'ed. A concurrent first-start
        process may win the exclusive create; in that case this process reads
        the winner's key or fails clearly if the winner has not completed it.
        """
        try:
            return cls._load_existing(key_path)
        except FileNotFoundError:
            pass

        key = Fernet.generate_key()
        # O_EXCL: refuse to clobber a file that appeared between the check and
        # the create; 0o600 from the first byte, not chmod'ed after the fact.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(key_path, flags, 0o600)
        except FileExistsError:
            try:
                return cls._load_existing(key_path)
            except (FileNotFoundError, SecretKeyError) as exc:
                raise SecretKeyError(
                    f"Fernet key {key_path} appeared during concurrent initialization "
                    "but is not safely readable; retry after the other startup finishes"
                ) from exc
        except OSError as exc:
            raise SecretKeyError(f"cannot create Fernet key {key_path}: {exc}") from exc

        try:
            remaining = memoryview(key)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("key write made no progress")
                remaining = remaining[written:]
            os.fsync(fd)
        except OSError as exc:
            raise SecretKeyError(
                f"could not finish writing new Fernet key {key_path}; "
                "the incomplete file was left in place for fail-closed recovery"
            ) from exc
        finally:
            os.close(fd)
        return cls(key)

    @classmethod
    def _load_existing(cls, key_path: Path) -> SecretCipher:
        """Open an existing regular, private key without following symlinks."""
        try:
            entry_stat = key_path.lstat()
        except FileNotFoundError:
            raise  # Signals a true first start to ``load_or_create``.
        except OSError as exc:
            raise SecretKeyError(f"cannot inspect Fernet key {key_path}: {exc}") from exc
        cls._validate_key_stat(key_path, entry_stat)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(key_path, flags)
        except OSError as exc:
            raise SecretKeyError(f"cannot open Fernet key {key_path}: {exc}") from exc
        try:
            opened_stat = os.fstat(fd)
            cls._validate_key_stat(key_path, opened_stat)
            if (entry_stat.st_dev, entry_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
                raise SecretKeyError(f"Fernet key {key_path} changed while it was being opened")
            chunks: list[bytes] = []
            while chunk := os.read(fd, 4096):
                chunks.append(chunk)
        except OSError as exc:
            raise SecretKeyError(f"cannot read Fernet key {key_path}: {exc}") from exc
        finally:
            os.close(fd)

        try:
            return cls(b"".join(chunks).strip())
        except (TypeError, ValueError) as exc:
            raise SecretKeyError(
                f"Fernet key {key_path} is invalid; restore the matching database and key backup"
            ) from exc

    @staticmethod
    def _validate_key_stat(key_path: Path, key_stat: os.stat_result) -> None:
        """Reject links/special files and group/other-accessible key modes."""
        if not stat.S_ISREG(key_stat.st_mode):
            raise SecretKeyError(
                f"Fernet key {key_path} must be a regular file; symlinks and special files "
                "are not accepted"
            )
        mode = stat.S_IMODE(key_stat.st_mode)
        if mode & 0o077:
            raise SecretKeyError(
                f"Fernet key {key_path} has unsafe mode {mode:04o}; "
                "group and other permissions must be zero"
            )

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
