"""encounters/repair.py: `marcellus encounters repair` (docs/encounters.md
"Repair")."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from marcellus import db
from marcellus.encounters import repair, store
from marcellus.encounters.linker import Atom, LinkDecision, LinkerConfig
from tests.test_scrub import REVIEWSEGMENT_SCHEMA

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)


def _frigate_db(tmp_path: Path, name: str = "frigate.db") -> Path:
    p = tmp_path / name
    conn = sqlite3.connect(p)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    conn.commit()
    conn.close()
    return p


def _insert_reviewsegment(
    path: Path, *, rid: str, camera: str, start: float, end: float | None
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES (?, ?, ?, ?, 'alert', '{}')",
        (rid, camera, start, end),
    )
    conn.commit()
    conn.close()


def _seed_member(
    sidecar_conn: sqlite3.Connection,
    *,
    atom_id: str,
    now: float,
    start: float,
    end: float | None,
    camera: str = "alley-wide",
) -> str:
    """Insert a legit member row via `upsert_atom` (start_time > 0, so the
    write guard doesn't reject it), then hand-corrupt it to simulate a
    pre-#67 row -- exactly the shape `repair` targets."""
    atom = Atom(
        atom_id=atom_id,
        camera=camera,
        start_time=max(start, 1.0),
        end_time=end,
        labels=("person",),
        zones=(),
        event_ids=(),
        sub_labels=(),
        severity="detection",
    )
    decision = LinkDecision(encounter_id=None, reason="new", confidence=1.0)
    encounter_id = store.upsert_atom(sidecar_conn, atom, decision, now)
    sidecar_conn.execute(
        "UPDATE encounter_members SET start_time = ?, end_time = ? WHERE atom_id = ?",
        (start, end, atom_id),
    )
    sidecar_conn.commit()
    return encounter_id


def _member(conn: sqlite3.Connection, atom_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM encounter_members WHERE atom_id = ?", (atom_id,)
    ).fetchone()


def test_zero_start_with_live_frigate_segment_is_fixed(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=now - 500, end=now - 490)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        _seed_member(sidecar_conn, atom_id="a1", now=now, start=0.0, end=now - 490)
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.scanned == 1
        assert summary.fixed_start == 1
        assert summary.deleted_members == 0
        row = _member(sidecar_conn, "a1")
        assert row is not None
        assert row["start_time"] == now - 500
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_null_end_with_closed_frigate_segment_is_fixed(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=now - 500, end=now - 490)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        _seed_member(sidecar_conn, atom_id="a1", now=now, start=now - 500, end=None)
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.fixed_end == 1
        row = _member(sidecar_conn, "a1")
        assert row is not None
        assert row["end_time"] == now - 490
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_null_end_with_young_open_frigate_segment_is_untouched(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=now - 30, end=None)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        _seed_member(sidecar_conn, atom_id="a1", now=now, start=now - 30, end=None)
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.fixed_start == 0
        assert summary.fixed_end == 0
        assert summary.deleted_members == 0
        row = _member(sidecar_conn, "a1")
        assert row is not None
        assert row["end_time"] is None
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_null_end_with_ancient_open_frigate_segment_is_deleted(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    stale_start = now - CFG.max_duration_s - 100
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=stale_start, end=None)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        enc_id = _seed_member(sidecar_conn, atom_id="a1", now=now, start=stale_start, end=None)
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.deleted_members == 1
        assert summary.deleted_encounters == 1
        assert _member(sidecar_conn, "a1") is None
        assert store.get(sidecar_conn, enc_id) is None
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_member_with_no_frigate_row_is_deleted(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        enc_id = _seed_member(sidecar_conn, atom_id="a1", now=now, start=0.0, end=None)
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.deleted_members == 1
        assert summary.deleted_encounters == 1
        assert _member(sidecar_conn, "a1") is None
        assert store.get(sidecar_conn, enc_id) is None
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_encounter_partially_repaired_keeps_healthy_members_and_recomputes(
    tmp_path: Path,
) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(
        frigate_db, rid="bad", camera="alley-wide", start=now - 500, end=now - 490
    )
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        enc_id = _seed_member(sidecar_conn, atom_id="bad", now=now, start=0.0, end=now - 490)
        # A healthy second member in the same encounter.
        healthy = Atom(
            atom_id="good",
            camera="alley-wide",
            start_time=now - 480,
            end_time=now - 470,
            labels=("person",),
            zones=(),
            event_ids=(),
            sub_labels=(),
            severity="detection",
        )
        store.upsert_atom(
            sidecar_conn,
            healthy,
            LinkDecision(encounter_id=enc_id, reason="same_camera", confidence=0.9),
            now,
        )

        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert summary.fixed_start == 1
        assert summary.recomputed == 1
        assert summary.deleted_encounters == 0

        enc_row = store.get(sidecar_conn, enc_id)
        assert enc_row is not None
        assert enc_row["atom_count"] == 2
        assert enc_row["start_time"] == now - 500
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=now - 500, end=now - 490)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        _seed_member(sidecar_conn, atom_id="a1", now=now, start=0.0, end=now - 490)
        before = dict(_member(sidecar_conn, "a1"))
        summary = repair.repair(sidecar_conn, frigate_conn, now, CFG, dry_run=True)
        assert summary.fixed_start == 1
        after = dict(_member(sidecar_conn, "a1"))
        assert before == after
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_second_run_is_idempotent_and_reports_zeros(tmp_path: Path) -> None:
    now = time.time()
    frigate_db = _frigate_db(tmp_path)
    _insert_reviewsegment(frigate_db, rid="a1", camera="alley-wide", start=now - 500, end=now - 490)
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    frigate_conn = db.open_frigate_ro(frigate_db)
    try:
        _seed_member(sidecar_conn, atom_id="a1", now=now, start=0.0, end=None)
        first = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert first.fixed_start == 1
        assert first.fixed_end == 1
        second = repair.repair(sidecar_conn, frigate_conn, now, CFG)
        assert second.scanned == 0
        assert second.fixed_start == 0
        assert second.fixed_end == 0
        assert second.deleted_members == 0
        assert second.deleted_encounters == 0
        assert second.recomputed == 0
    finally:
        sidecar_conn.close()
        frigate_conn.close()


def test_upsert_atom_refuses_new_member_with_nonpositive_start(tmp_path: Path) -> None:
    sidecar_conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        atom = Atom(
            atom_id="zero",
            camera="alley-wide",
            start_time=0.0,
            end_time=None,
            labels=("person",),
        )
        decision = LinkDecision(encounter_id=None, reason="new", confidence=1.0)
        try:
            store.upsert_atom(sidecar_conn, atom, decision, time.time())
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for start_time <= 0 insert")
        assert _member(sidecar_conn, "zero") is None
    finally:
        sidecar_conn.close()
