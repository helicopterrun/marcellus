"""Backup and restore for the sidecar's own state.

State worth backing up is small and separable from everything else the
sidecar touches:

* the sidecar's SQLite DB (`Settings.sidecar.db_path`) -- labels, push
  devices/tokens, face clusters, scrub-cache index;
* `<db_path>.parent/.session_secret` -- losing this signs every remember-me
  cookie out at once, so it's worth keeping, but it isn't secret material
  Frigate needs;
* the resolved sidecar config file (same discovery `config.load_settings`
  uses).

Deliberately NOT backed up: the scrub cache directory and the face-model
directory -- both are regenerable from Frigate's own recordings/DB, and are
typically far larger than the state above.

The DB is copied via `sqlite3.Connection.backup()` from a fresh read-only
connection, which is safe to run against a live writer (unlike copying the
raw file, which can catch it mid-checkpoint). Restoring is the reverse:
verify the manifest, then atomically replace the DB file and drop any stale
`-wal`/`-shm` siblings so the restored file isn't mixed with WAL frames from
a different generation of the DB.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from frigate_sidecar import __version__
from frigate_sidecar.config import Settings, _discover_yaml_path
from frigate_sidecar.db import open_sidecar

log = logging.getLogger(__name__)

DB_NAME = "frigate-sidecar.db"
SECRET_NAME = ".session_secret"
MANIFEST_NAME = "manifest.json"


class BackupError(Exception):
    """A backup or restore operation refused to proceed."""


@dataclasses.dataclass
class BackupManifest:
    version: str
    created_at: str
    files: dict[str, dict[str, Any]]  # name -> {"sha256": ..., "size": ...}

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> BackupManifest:
        data = json.loads(text)
        return cls(version=data["version"], created_at=data["created_at"], files=data["files"])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _config_file_for(settings: Settings) -> Path | None:
    """Best-effort resolve the YAML file `load_settings` actually read.

    `Settings` doesn't remember which file it was built from, so this repeats
    `config._discover_yaml_path`'s own search (env var, then the default
    search path) -- the same discovery `config.py:~830` documents. Returns
    None if no config file is in use (env-var-only deployment, or a `Settings`
    built directly in tests).
    """
    path = _discover_yaml_path(None)
    if path is not None and path.exists():
        return path
    return None


def _sqlite_backup(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` via sqlite3's backup API -- safe against a live
    writer, unlike a raw file copy (which can catch mid-WAL-checkpoint)."""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5.0)
    try:
        dest_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()


@contextmanager
def _work_dir(dest: Path, *, is_tar: bool) -> Iterator[Path]:
    if not is_tar:
        dest.mkdir(parents=True, exist_ok=True)
        yield dest
        return
    tmp = Path(tempfile.mkdtemp(prefix="fsc-backup-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def create_backup(settings: Settings, dest: Path) -> BackupManifest:
    """Write a backup to `dest`.

    `dest` is written as a plain directory, or as a `.tar.gz` archive if its
    name ends with that suffix.
    """
    db_path = Path(settings.sidecar.db_path)
    if not db_path.exists():
        raise BackupError(f"sidecar DB not found: {db_path}")

    is_tar = dest.name.endswith(".tar.gz")
    with _work_dir(dest, is_tar=is_tar) as work_dir:
        _sqlite_backup(db_path, work_dir / DB_NAME)

        names = [DB_NAME]
        secret_path = db_path.parent / SECRET_NAME
        if secret_path.exists():
            shutil.copy2(secret_path, work_dir / SECRET_NAME)
            names.append(SECRET_NAME)

        config_path = _config_file_for(settings)
        if config_path is not None:
            shutil.copy2(config_path, work_dir / config_path.name)
            names.append(config_path.name)

        files = {
            name: {"sha256": _sha256(work_dir / name), "size": (work_dir / name).stat().st_size}
            for name in names
        }
        manifest = BackupManifest(
            version=__version__,
            created_at=datetime.now(timezone.utc).isoformat(),
            files=files,
        )
        (work_dir / MANIFEST_NAME).write_text(manifest.to_json())

        if is_tar:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(dest, "w:gz") as tar:
                for name in [*names, MANIFEST_NAME]:
                    tar.add(work_dir / name, arcname=name)

    return manifest


@contextmanager
def _read_dir(src: Path) -> Iterator[Path]:
    if src.name.endswith(".tar.gz"):
        tmp = Path(tempfile.mkdtemp(prefix="fsc-restore-"))
        try:
            with tarfile.open(src, "r:gz") as tar:
                # `filter="data"` only exists from 3.12 -- prod is 3.10, so
                # guard on the module having it rather than assuming.
                kwargs = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                tar.extractall(tmp, **kwargs)  # type: ignore[arg-type]
            yield tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        yield src


def _table_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: {row[1] for row in conn.execute(f"PRAGMA table_info({t})")} for t in tables}


def _existing_db_is_newer(existing: Path, backup_db: Path) -> bool:
    """True when `existing` has tables/columns `backup_db` predates.

    No `PRAGMA user_version` is set anywhere in this codebase, so this falls
    back to the table/column superset check the spec calls for: if the live
    DB's schema isn't a subset of the backup's, the restore is stepping back
    across a migration -- worth a warning in the log, not a refusal, since the
    forward migration on the first open re-creates whatever is missing.
    """
    if not existing.exists():
        return False
    existing_conn = sqlite3.connect(f"file:{existing}?mode=ro", uri=True)
    backup_conn = sqlite3.connect(f"file:{backup_db}?mode=ro", uri=True)
    try:
        existing_cols = _table_columns(existing_conn)
        backup_cols = _table_columns(backup_conn)
    finally:
        existing_conn.close()
        backup_conn.close()
    if set(existing_cols) - set(backup_cols):
        return True
    return any(cols - backup_cols.get(table, set()) for table, cols in existing_cols.items())


def restore_backup(settings: Settings, src: Path, *, force: bool = False) -> BackupManifest:
    """Restore `src` (a directory or `.tar.gz` from `create_backup`) onto
    `settings`. Raises `BackupError` and changes nothing on any refusal.
    """
    with _read_dir(src) as src_dir:
        manifest_path = src_dir / MANIFEST_NAME
        if not manifest_path.exists():
            raise BackupError(f"not a Marcellus backup: no {MANIFEST_NAME} in {src}")
        manifest = BackupManifest.from_json(manifest_path.read_text())

        for name, meta in manifest.files.items():
            p = src_dir / name
            if not p.exists():
                raise BackupError(f"manifest lists {name!r} but it is missing from {src}")
            if p.stat().st_size != meta.get("size") or _sha256(p) != meta["sha256"]:
                raise BackupError(f"{name}: does not match the manifest -- backup is corrupt")

        if not force:
            raise BackupError(
                "refusing to restore: stop frigate-sidecar first, then re-run with "
                "--force (restoring under a running service corrupts the WAL)"
            )

        db_path = Path(settings.sidecar.db_path)
        backup_db = src_dir / DB_NAME
        if _existing_db_is_newer(db_path, backup_db):
            # Not a refusal: the schema is additive and `open_sidecar` below
            # re-creates missing tables and re-adds missing columns, so an
            # older backup migrates forward cleanly. Only the rows written
            # since the backup are lost -- which is what a restore means.
            log.warning(
                "%s has tables/columns this backup predates; they will be "
                "re-created empty by the forward migration",
                db_path,
            )

        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_db = db_path.with_name(db_path.name + ".restore-tmp")
        shutil.copy2(backup_db, tmp_db)
        os.replace(tmp_db, db_path)
        for suffix in ("-wal", "-shm"):
            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

        secret_src = src_dir / SECRET_NAME
        if secret_src.exists():
            shutil.copy2(secret_src, db_path.parent / SECRET_NAME)

        config_name = next(
            (n for n in manifest.files if n not in (DB_NAME, SECRET_NAME)), None
        )
        if config_name is not None:
            config_dest = _config_file_for(settings) or (db_path.parent / config_name)
            config_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_dir / config_name, config_dest)

        # Opens (and, if the restored file predates a since-added column,
        # migrates) the restored DB forward once before handing control back.
        open_sidecar(db_path).close()

    return manifest
