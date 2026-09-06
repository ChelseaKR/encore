"""`encore export` / `encore import` — portable, secret-free watch state (#54).

The four "Done when" clauses of #54 are each a test here. Two properties get
their own guards, because both are the kind that decay silently:

- **secrets are structurally absent** — including `NotificationChannel.
  last_error`, which usually holds a harmless message and occasionally holds
  an Apprise URL, because `notify/engine.py` records `error=str(exc)` and an
  Apprise URL is a credential;
- **absence is exported as absence** — an artist with no override exports no
  override, not a copy of today's resolved defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlmodel import select

from encore import cli
from encore.artistsettings import SettingsOverride
from encore.models import Artist, NotificationChannel
from encore.portable import (
    CHANNEL_EXPORTED_FIELDS,
    SCHEMA_VERSION,
    SECRET_BEARING_SUBSTRINGS,
    ImportStrategy,
    PortableError,
    export_state,
    import_state,
    read_document,
    render,
)
from encore.storage import Storage

pytestmark = pytest.mark.recovery

PLEX_TOKEN = "plex-token-sentinel-do-not-export"  # noqa: S105 - a test sentinel
CHANNEL_URL = "ntfy://user:sentinel-password@example.invalid/topic"


def _install(tmp_path: Path, name: str = "data") -> Storage:
    return Storage(tmp_path / name)


def _seed_artists(storage: Storage, rows: list[tuple[str, str]]) -> None:
    """Create Plex artist rows the way a sync would have."""
    with storage.session() as session:
        for rating_key, name in rows:
            session.add(Artist(plex_rating_key=rating_key, name=name, library_key="1"))
        session.commit()


def _populated(tmp_path: Path, name: str = "data") -> Storage:
    """Return an install with a token, a channel, two artists and an override."""
    storage = _install(tmp_path, name)
    storage.set_plex_credentials("http://plex.invalid:32400", PLEX_TOKEN)
    storage.add_channel(name="phone", url=CHANNEL_URL, mode="instant")
    _seed_artists(storage, [("a1", "First Artist"), ("a2", "Second Artist")])
    storage.set_artist_settings("a1", SettingsOverride(priority="instant"))
    return storage


# --- Done when #4: absence is absence -----------------------------------------


def test_an_artist_with_no_override_exports_no_override(tmp_path: Path) -> None:
    """The property the partial-override design depends on.

    Writing the resolved defaults into every artist would pin each of them to
    today's policy, so a later change to the global defaults would silently
    stop applying. The key is omitted, not emitted as null or {}.
    """
    storage = _populated(tmp_path)
    try:
        document = export_state(storage)
    finally:
        storage.close()
    entries = {entry["key"]: entry for entry in document["artists"]}
    assert "settings" in entries["a1"], "an artist that HAS an override must export it"
    assert "settings" not in entries["a2"], (
        "an artist with no override exported one; absence must stay absence"
    )


def test_an_install_with_no_defaults_and_no_channels_exports_a_small_document(
    tmp_path: Path,
) -> None:
    storage = _install(tmp_path)
    try:
        document = export_state(storage)
    finally:
        storage.close()
    assert document["artists"] == []
    assert document["channels"] == []
    assert document["recommendations"] == []
    assert "watch_defaults" not in document, (
        "an unconfigured install exported a snapshot of the built-in defaults"
    )
    assert len(render(document)) < 200


# --- Done when #1: export -> fresh install -> import -> export is byte-identical


def test_round_trip_yields_a_byte_identical_second_document(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source")
    try:
        first = render(export_state(source))
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        # Two things the document deliberately cannot carry, supplied the way a
        # person would on new hardware: the library membership (a Plex sync) and
        # the channel URL (`encore channels add`, with the credential in hand).
        # A portable document describes policy, not inventory and not secrets.
        _seed_artists(target, [("a1", "First Artist"), ("a2", "Second Artist")])
        target.add_channel(name="phone", url=CHANNEL_URL, mode="instant")
        import_state(
            target,
            read_document(json.loads(first)),
            strategy=ImportStrategy.PREFER_FILE,
        )
        second = render(export_state(target))
    finally:
        target.close()
    assert first == second


# --- Done when #2: a document carrying a credential is refused by name --------


@pytest.mark.no_secrets_in_logs
def test_a_document_carrying_a_cipher_field_is_refused(tmp_path: Path) -> None:
    storage = _populated(tmp_path)
    try:
        document = export_state(storage)
    finally:
        storage.close()
    document["channels"][0]["url_cipher"] = "Z0FBQUFBQm..."
    with pytest.raises(PortableError, match="secret-bearing field"):
        read_document(document)


@pytest.mark.no_secrets_in_logs
def test_a_document_carrying_an_apprise_url_is_refused(tmp_path: Path) -> None:
    storage = _populated(tmp_path)
    try:
        document = export_state(storage)
    finally:
        storage.close()
    document["channels"][0]["endpoint"] = CHANNEL_URL
    with pytest.raises(PortableError, match="Apprise URL"):
        read_document(document)


@pytest.mark.parametrize("marker", SECRET_BEARING_SUBSTRINGS)
def test_every_declared_secret_marker_is_actually_refused(marker: str) -> None:
    """The marker list is not decoration: each entry rejects a document."""
    document = {
        "schema_version": SCHEMA_VERSION,
        "artists": [{"key": "a1", "name": "A", "source": "plex", f"plex_{marker}": "x"}],
        "recommendations": [],
        "channels": [],
    }
    with pytest.raises(PortableError, match="secret-bearing field"):
        read_document(document)


# --- Done when #3: keep-local leaves the local row alone and lists the conflict


def test_keep_local_leaves_the_local_row_unchanged_and_lists_the_conflict(
    tmp_path: Path,
) -> None:
    source = _populated(tmp_path, "source")
    try:
        document = export_state(source)
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        _seed_artists(target, [("a1", "First Artist")])
        target.set_artist_settings("a1", SettingsOverride(priority="digest"))
        plan = import_state(target, read_document(document), strategy=ImportStrategy.KEEP_LOCAL)
        assert target.get_artist_settings("a1").priority == "digest"
    finally:
        target.close()
    assert any("a1" in row for row in plan.conflicts)
    assert plan.added == ()


def test_prefer_file_takes_the_file_s_settings(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source")
    try:
        document = export_state(source)
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        _seed_artists(target, [("a1", "First Artist")])
        target.set_artist_settings("a1", SettingsOverride(priority="digest"))
        import_state(target, read_document(document), strategy=ImportStrategy.PREFER_FILE)
        assert target.get_artist_settings("a1").priority == "instant"
    finally:
        target.close()


def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    source = _populated(tmp_path, "source")
    try:
        document = export_state(source)
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        _seed_artists(target, [("a1", "First Artist")])
        target.set_artist_settings("a1", SettingsOverride(priority="digest"))
        plan = import_state(
            target,
            read_document(document),
            strategy=ImportStrategy.PREFER_FILE,
            dry_run=True,
        )
        assert plan.added, "the plan must still describe what it would do"
        assert target.get_artist_settings("a1").priority == "digest"
    finally:
        target.close()


def test_an_artist_this_install_has_never_seen_is_skipped_with_a_reason(
    tmp_path: Path,
) -> None:
    """Report a never-synced artist, never drop it silently.

    An import reporting fewer rows than it read is the same defect as a failed
    read counted as zero.
    """
    source = _populated(tmp_path, "source")
    try:
        document = export_state(source)
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        plan = import_state(target, read_document(document), strategy=ImportStrategy.PREFER_FILE)
    finally:
        target.close()
    artist_rows = [row for row in plan.skipped if row.startswith(("a1", "a2"))]
    assert len(artist_rows) == 2
    assert all("not present in this install" in row for row in artist_rows)
    assert plan.added == ()


# --- Secret hygiene: nothing secret leaves, by construction --------------------


@pytest.mark.no_secrets_in_logs
def test_no_secret_material_appears_anywhere_in_the_document(tmp_path: Path) -> None:
    storage = _populated(tmp_path)
    try:
        storage.rotate_feed_token()
        feed_token = storage.get_feed_token()
        text = render(export_state(storage))
    finally:
        storage.close()
    assert feed_token is not None
    for secret in (PLEX_TOKEN, CHANNEL_URL, "sentinel-password", feed_token):
        assert secret not in text


@pytest.mark.no_secrets_in_logs
def test_a_channel_last_error_holding_a_url_is_never_exported(tmp_path: Path) -> None:
    """The dangerous exclusion, and the reason the field list is an allow-list.

    `notify/engine.py` records `error=str(exc)` on a failed send. Apprise
    exceptions are entirely capable of quoting the URL they failed on, and an
    Apprise URL is a credential. A field that usually holds a harmless message
    and occasionally holds a credential is a credential field.
    """
    storage = _populated(tmp_path)
    try:
        with storage.session() as session:
            channel = session.exec(select(NotificationChannel)).one()
            channel.last_error = f"Apprise failed to send to {CHANNEL_URL}"
            session.add(channel)
            session.commit()
        text = render(export_state(storage))
    finally:
        storage.close()
    assert CHANNEL_URL not in text
    assert "last_error" not in text
    assert "sentinel-password" not in text


def test_the_channel_field_list_names_only_non_secret_columns(tmp_path: Path) -> None:
    """Every exported channel field must exist and must not be secret-bearing.

    An allow-list only helps while it is checked. This fails if a field is
    renamed out of the model, and it fails if a secret-looking one is added.
    """
    model_fields = set(NotificationChannel.model_fields)
    for field in CHANNEL_EXPORTED_FIELDS:
        assert field in model_fields, f"{field!r} is not a NotificationChannel column"
        lowered = field.casefold()
        assert not any(marker in lowered for marker in SECRET_BEARING_SUBSTRINGS)
    assert "last_error" not in CHANNEL_EXPORTED_FIELDS
    assert "url_cipher" not in CHANNEL_EXPORTED_FIELDS


def test_every_ciphertext_column_in_the_schema_is_outside_the_export(
    tmp_path: Path,
) -> None:
    """Walk the real schema rather than trusting the field list to be complete."""
    import sqlite3

    storage = _populated(tmp_path)
    try:
        text = render(export_state(storage))
        db_path = storage.db_path
    finally:
        storage.close()
    connection = sqlite3.connect(db_path)
    try:
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in connection.execute(f"PRAGMA table_info({table})"):
                if row[1].endswith("_cipher"):
                    assert row[1] not in text, f"{table}.{row[1]} reached the export"
    finally:
        connection.close()


# --- Document validation ------------------------------------------------------


def test_a_future_schema_version_is_refused_by_name(tmp_path: Path) -> None:
    """An otherwise-valid document, rejected for its version and nothing else."""
    storage = _populated(tmp_path)
    try:
        document = export_state(storage)
    finally:
        storage.close()
    document["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(PortableError, match=str(SCHEMA_VERSION + 1)):
        read_document(document)
    # Proof the document is otherwise sound.
    document["schema_version"] = SCHEMA_VERSION
    assert read_document(document) is document


def test_an_artist_with_invalid_settings_is_refused(tmp_path: Path) -> None:
    document = {
        "schema_version": SCHEMA_VERSION,
        "artists": [{"key": "a1", "name": "A", "source": "plex", "settings": {"priority": "loud"}}],
        "recommendations": [],
        "channels": [],
    }
    with pytest.raises(PortableError, match="invalid watch settings"):
        read_document(document)


def test_an_artist_entry_with_no_key_is_refused() -> None:
    document = {
        "schema_version": SCHEMA_VERSION,
        "artists": [{"name": "A"}],
        "recommendations": [],
        "channels": [],
    }
    with pytest.raises(PortableError, match="no key"):
        read_document(document)


# --- CLI ----------------------------------------------------------------------


@pytest.mark.no_secrets_in_logs
def test_cli_export_then_import_round_trips_without_printing_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_dir = tmp_path / "source"
    storage = _populated(tmp_path, "source")
    storage.close()
    document_path = tmp_path / "watchlist.json"
    assert cli.main(["export", "--data-dir", str(source_dir), "--out", str(document_path)]) == 0

    target_dir = tmp_path / "target"
    target = Storage(target_dir)
    _seed_artists(target, [("a1", "First Artist")])
    target.close()
    assert (
        cli.main(
            [
                "import",
                str(document_path),
                "--data-dir",
                str(target_dir),
                "--strategy",
                "prefer-file",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for secret in (PLEX_TOKEN, CHANNEL_URL, "sentinel-password"):
        assert secret not in combined
    assert "added" in combined


def test_cli_export_refuses_to_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = _populated(tmp_path)
    storage.close()
    out = tmp_path / "existing.json"
    out.write_text("keep me", encoding="utf-8")
    assert cli.main(["export", "--data-dir", str(tmp_path / "data"), "--out", str(out)]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "keep me"


def test_cli_import_reports_a_malformed_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cli.main(["import", str(bad), "--data-dir", str(tmp_path / "data")]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_cli_import_dry_run_says_it_would(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    storage = _populated(tmp_path, "source")
    storage.close()
    document_path = tmp_path / "watchlist.json"
    cli.main(["export", "--data-dir", str(tmp_path / "source"), "--out", str(document_path)])
    capsys.readouterr()
    assert (
        cli.main(["import", str(document_path), "--data-dir", str(tmp_path / "t"), "--dry-run"])
        == 0
    )
    assert "Would import" in capsys.readouterr().out


def test_a_channel_is_reported_as_unimportable_with_the_reason(tmp_path: Path) -> None:
    """Never silently ignored.

    The Apprise URL *is* the channel and is deliberately absent from the
    document, so there is nothing to recreate one from. An import that quietly
    skipped the section would let somebody move to new hardware believing
    their notifications came with them.
    """
    source = _populated(tmp_path, "source")
    try:
        document = export_state(source)
    finally:
        source.close()

    target = _install(tmp_path, "target")
    try:
        plan = import_state(target, read_document(document), strategy=ImportStrategy.PREFER_FILE)
    finally:
        target.close()
    channel_rows = [row for row in plan.skipped if row.startswith("channel ")]
    assert len(channel_rows) == 1
    assert "credential" in channel_rows[0]
    assert "encore channels add" in channel_rows[0]
