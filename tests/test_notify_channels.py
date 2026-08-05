"""Channel storage tests (F4), including the encrypted-at-rest proof for Apprise URLs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from encore.notify.render import RenderedNotification
from encore.notify.sender import AppriseSender, DeliveryError
from encore.storage import DB_FILENAME, Storage, StorageError
from tests.notify_fixtures import CHANNEL_URL


@pytest.fixture(name="storage")
def storage_fixture(tmp_path: Path) -> Storage:
    return Storage(tmp_path / "data")


def _raw_db_bytes(data_dir: Path) -> bytes:
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        candidate = data_dir / f"{DB_FILENAME}{suffix}"
        if candidate.exists():
            blob += candidate.read_bytes()
    return blob


@pytest.mark.no_secrets_in_logs
def test_apprise_url_never_appears_in_the_database_file_plaintext(tmp_path: Path) -> None:
    # The same load-bearing proof F0 applies to the Plex token: an Apprise URL
    # is a credential, so grep the raw bytes rather than trusting the ORM.
    data_dir = tmp_path / "data"
    storage = Storage(data_dir)
    channel = storage.add_channel("phone", CHANNEL_URL)
    assert storage.channel_url(channel) == CHANNEL_URL
    storage.close()

    raw = _raw_db_bytes(data_dir)
    assert CHANNEL_URL.encode() not in raw
    assert b"APPRISE-URL-needle" not in raw
    # Sanity check that the right bytes were read: the operator's own label is
    # not a secret and is stored in the clear.
    assert b"phone" in raw


def test_channel_names_are_unique(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    with pytest.raises(StorageError, match="already exists"):
        storage.add_channel("phone", "discord://id/token")


def test_invalid_mode_and_interval_are_rejected(storage: Storage) -> None:
    with pytest.raises(StorageError, match="invalid channel mode"):
        storage.add_channel("phone", CHANNEL_URL, mode="carrier-pigeon")
    with pytest.raises(StorageError, match="positive number of hours"):
        storage.add_channel("phone", CHANNEL_URL, mode="digest", digest_interval_hours=0)


def test_enabled_only_listing_and_toggling(storage: Storage) -> None:
    storage.add_channel("phone", CHANNEL_URL)
    storage.add_channel("email", "mailto://user:pw@example.com", mode="digest")

    assert [c.name for c in storage.list_channels()] == ["phone", "email"]
    storage.set_channel_enabled("email", False)
    assert [c.name for c in storage.list_channels(enabled_only=True)] == ["phone"]
    storage.set_channel_enabled("email", True)
    assert len(storage.list_channels(enabled_only=True)) == 2


def test_unknown_channel_operations_fail_loudly(storage: Storage) -> None:
    with pytest.raises(StorageError, match="no notification channel"):
        storage.set_channel_enabled("nope", True)
    with pytest.raises(StorageError, match="no notification channel"):
        storage.remove_channel("nope")


def test_invalid_delivery_status_is_rejected(storage: Storage) -> None:
    with pytest.raises(StorageError, match="invalid delivery status"):
        storage.update_delivery(1, "maybe", 1)


class _FakeApprise:
    """Stands in for ``apprise.Apprise`` so no packet leaves the test host."""

    def __init__(self, accept: bool = True, delivered: bool = True, explode: bool = False) -> None:
        self.accept = accept
        self.delivered = delivered
        self.explode = explode

    def add(self, url: str) -> bool:
        if self.explode:
            raise ValueError(f"plugin blew up parsing {url}")
        return self.accept

    def notify(self, title: str, body: str) -> bool:
        return self.delivered


def _patch_apprise(monkeypatch: pytest.MonkeyPatch, fake: _FakeApprise) -> None:
    import apprise

    monkeypatch.setattr(apprise, "Apprise", lambda: fake)


def test_apprise_sender_reports_an_unrecognized_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_apprise(monkeypatch, _FakeApprise(accept=False))
    with pytest.raises(DeliveryError, match="did not recognize"):
        AppriseSender().send(CHANNEL_URL, RenderedNotification("t", "b"))


def test_apprise_sender_reports_a_dropped_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_apprise(monkeypatch, _FakeApprise(delivered=False))
    with pytest.raises(DeliveryError, match="rejected or dropped"):
        AppriseSender().send(CHANNEL_URL, RenderedNotification("t", "b"))


def test_apprise_sender_succeeds_quietly(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_apprise(monkeypatch, _FakeApprise())
    AppriseSender().send(CHANNEL_URL, RenderedNotification("t", "b"))


@pytest.mark.no_secrets_in_logs
def test_a_plugin_exception_never_leaks_the_channel_url(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A plugin is free to echo the URL into its own exception message, so the
    # adapter reports the exception *type* and nothing else (docs/adr/0012).
    _patch_apprise(monkeypatch, _FakeApprise(explode=True))
    with caplog.at_level(logging.DEBUG), pytest.raises(DeliveryError) as raised:
        AppriseSender().send(CHANNEL_URL, RenderedNotification("t", "b"))

    assert "ValueError" in str(raised.value)
    assert CHANNEL_URL not in str(raised.value)
    assert CHANNEL_URL not in "\n".join(record.getMessage() for record in caplog.records)


def test_real_apprise_rejects_a_nonsense_url() -> None:
    # One test against the real library (offline: parsing only, no request) so
    # the adapter is not merely proven against its own stub.
    with pytest.raises(DeliveryError, match="did not recognize"):
        AppriseSender().send("not-a-real-scheme://nowhere", RenderedNotification("t", "b"))


def test_an_f3_database_upgrades_to_the_notification_schema(tmp_path: Path) -> None:
    # A real v4 database (F3 build): no channels, no deliveries, no machine id.
    from encore.storage import MIGRATIONS

    data_dir = tmp_path / "data"
    storage = Storage(data_dir)
    storage.set_plex_credentials("http://plex.local:32400", "token-abc")
    with storage.engine.connect() as connection:
        connection.exec_driver_sql("DROP TABLE deliveries")
        connection.exec_driver_sql("DROP TABLE channels")
        connection.exec_driver_sql("ALTER TABLE settings DROP COLUMN plex_machine_identifier")
        connection.exec_driver_sql("PRAGMA user_version = 4")
        connection.commit()
    storage.close()

    upgraded = Storage(data_dir)
    with upgraded.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == len(MIGRATIONS)
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {"channels", "deliveries"} <= tables
    # Settings survived, and the new column is present and empty.
    assert upgraded.get_plex_credentials() == ("http://plex.local:32400", "token-abc")
    assert upgraded.get_plex_machine_identifier() is None
    upgraded.set_plex_machine_identifier("machine-1")
    assert upgraded.get_plex_machine_identifier() == "machine-1"
    upgraded.close()
