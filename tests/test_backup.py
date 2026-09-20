"""Round-trip and safety tests for `marcellus.backup`."""

from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.backup import (
    DB_NAME,
    LEGACY_DB_NAMES,
    MANIFEST_NAME,
    SECRET_NAME,
    BackupError,
    BackupManifest,
    create_backup,
    restore_backup,
)
from marcellus.config import FrigateSection, Settings, SidecarSection


def _settings(frigate_db_path: Path, sidecar_db_path: Path) -> Settings:
    return Settings(
        frigate=FrigateSection(db_path=frigate_db_path),
        sidecar=SidecarSection(db_path=sidecar_db_path),
    )


def _seed(sidecar_db_path: Path, note: str) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        conn.execute(
            "INSERT INTO triage_labels (event_id, label, note, labeled_at) VALUES (?, ?, ?, ?)",
            ("e1", "tp", note, "2026-01-01T00:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _label_note(sidecar_db_path: Path) -> str:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        row = conn.execute("SELECT note FROM triage_labels WHERE event_id = 'e1'").fetchone()
        return str(row["note"]) if row else ""
    finally:
        conn.close()


def test_directory_round_trip(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    _seed(sidecar_db_path, "original")

    dest = tmp_path / "backup"
    manifest = create_backup(settings, dest)
    assert DB_NAME in manifest.files
    assert (dest / MANIFEST_NAME).exists()

    # Mutate the live DB after the backup...
    conn = db.open_sidecar(sidecar_db_path)
    conn.execute("UPDATE triage_labels SET note = 'mutated' WHERE event_id = 'e1'")
    conn.commit()
    conn.close()
    assert _label_note(sidecar_db_path) == "mutated"

    # ...restore should bring the original row back.
    restore_backup(settings, dest, force=True)
    assert _label_note(sidecar_db_path) == "original"


def test_tar_gz_round_trip(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    _seed(sidecar_db_path, "tar-original")

    dest = tmp_path / "backup.tar.gz"
    manifest = create_backup(settings, dest)
    assert dest.exists() and dest.is_file()

    conn = db.open_sidecar(sidecar_db_path)
    conn.execute("UPDATE triage_labels SET note = 'mutated' WHERE event_id = 'e1'")
    conn.commit()
    conn.close()

    restore_backup(settings, dest, force=True)
    assert _label_note(sidecar_db_path) == "tar-original"
    assert manifest.version


def test_session_secret_is_backed_up_and_restored(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    db.open_sidecar(sidecar_db_path).close()
    sidecar_db_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path = sidecar_db_path.parent / SECRET_NAME
    secret_path.write_bytes(b"a" * 32)

    dest = tmp_path / "backup"
    manifest = create_backup(settings, dest)
    assert SECRET_NAME in manifest.files

    secret_path.write_bytes(b"b" * 32)
    restore_backup(settings, dest, force=True)
    assert secret_path.read_bytes() == b"a" * 32


def test_missing_session_secret_is_fine(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    db.open_sidecar(sidecar_db_path).close()
    assert not (sidecar_db_path.parent / SECRET_NAME).exists()

    dest = tmp_path / "backup"
    manifest = create_backup(settings, dest)
    assert SECRET_NAME not in manifest.files
    restore_backup(settings, dest, force=True)  # must not raise


def test_restore_refuses_without_force(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    db.open_sidecar(sidecar_db_path).close()
    dest = tmp_path / "backup"
    create_backup(settings, dest)

    with pytest.raises(BackupError, match="stop marcellus"):
        restore_backup(settings, dest)


def test_restore_refuses_on_manifest_hash_mismatch(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    db.open_sidecar(sidecar_db_path).close()
    dest = tmp_path / "backup"
    create_backup(settings, dest)

    (dest / DB_NAME).write_bytes(b"corrupted")

    with pytest.raises(BackupError, match="manifest"):
        restore_backup(settings, dest, force=True)


def test_restore_refuses_missing_db_not_backup(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    empty_dir = tmp_path / "not-a-backup"
    empty_dir.mkdir()
    with pytest.raises(BackupError, match="manifest.json"):
        restore_backup(settings, empty_dir, force=True)


def test_create_backup_refuses_missing_db(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path)
    assert not sidecar_db_path.exists()
    with pytest.raises(BackupError, match="not found"):
        create_backup(settings, tmp_path / "backup")


def test_restore_accepts_legacy_db_filename(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """A backup made before the rename still has its DB stored under the old
    `frigate-sidecar.db` name; restore must fall back to it."""
    settings = _settings(frigate_db_path, sidecar_db_path)
    _seed(sidecar_db_path, "legacy-original")

    dest = tmp_path / "backup"
    manifest = create_backup(settings, dest)

    legacy_name = LEGACY_DB_NAMES[0]
    (dest / DB_NAME).rename(dest / legacy_name)
    manifest.files[legacy_name] = manifest.files.pop(DB_NAME)
    (dest / MANIFEST_NAME).write_text(manifest.to_json())

    conn = db.open_sidecar(sidecar_db_path)
    conn.execute("UPDATE triage_labels SET note = 'mutated' WHERE event_id = 'e1'")
    conn.commit()
    conn.close()

    restore_backup(settings, dest, force=True)
    assert _label_note(sidecar_db_path) == "legacy-original"


def test_manifest_json_round_trip() -> None:
    m = BackupManifest(
        version="1.2.3", created_at="2026-01-01T00:00:00+00:00",
        files={"a": {"sha256": "abc", "size": 3}},
    )
    m2 = BackupManifest.from_json(m.to_json())
    assert m2 == m
