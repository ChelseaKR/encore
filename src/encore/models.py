"""SQLModel table definitions — the schema's first cut (encore-plans/04).

M1-F0 scope: only the ``settings`` singleton exists. Artists, release-groups,
events, channels, and recommendations land with the features that read and
write them (F1-F7), each added by its own migration in `encore.storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel

__all__ = ["SETTINGS_ROW_ID", "AppSettings"]

SETTINGS_ROW_ID = 1


def utcnow() -> datetime:
    """Timezone-aware UTC now (SQLite stores it as ISO-8601 text)."""
    return datetime.now(UTC)


class AppSettings(SQLModel, table=True):
    """The singleton settings row (``id`` is always ``SETTINGS_ROW_ID``).

    Secret-bearing columns hold Fernet ciphertexts (``*_cipher``, bytes) —
    never plaintext. Encryption and decryption happen in `encore.storage`
    with the key file stored beside the database (docs/adr/0008).
    """

    __tablename__ = "settings"

    id: int | None = Field(default=None, primary_key=True)
    plex_base_url: str | None = Field(default=None)
    plex_token_cipher: bytes | None = Field(default=None)
    updated_at: datetime = Field(default_factory=utcnow)
