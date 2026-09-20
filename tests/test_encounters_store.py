"""encounters/store.py: schema, upsert idempotency, aggregates, sealing,
watermark (docs/encounters.md "Tests" section)."""

from __future__ import annotations

import time
from pathlib import Path

from marcellus import db
from marcellus.encounters import store
from marcellus.encounters.linker import Atom, LinkDecision, LinkerConfig

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)


def _atom(atom_id: str, camera: str = "alley-wide", start: float = 0.0, **kw: object) -> Atom:
    defaults: dict[str, object] = dict(
        end_time=start + 10.0,
        labels=("person",),
        zones=(),
        event_ids=(f"ev-{atom_id}",),
        sub_labels=(),
        severity="detection",
    )
    defaults.update(kw)
    return Atom(atom_id=atom_id, camera=camera, start_time=start, **defaults)  # type: ignore[arg-type]


def test_schema_applies(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        for table in ("encounters", "encounter_members", "encounter_decisions", "encounter_state"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert cols, f"{table} missing"
    finally:
        conn.close()


def test_upsert_twice_updates_not_duplicates(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        atom = _atom("a1", start=now, end_time=None)
        decision = LinkDecision(None, "new", 1.0)
        enc_id = store.upsert_atom(conn, atom, decision, now)

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1

        updated_atom = _atom("a1", start=now, end_time=now + 15.0)
        again_decision = LinkDecision(enc_id, "same_camera", 0.9)
        enc_id2 = store.upsert_atom(conn, updated_atom, again_decision, now + 20.0)
        assert enc_id2 == enc_id

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
        row = conn.execute("SELECT end_time FROM encounter_members WHERE atom_id = 'a1'").fetchone()
        assert row["end_time"] == now + 15.0
        enc_row = store.get(conn, enc_id)
        assert enc_row is not None
        assert enc_row["end_time"] == now + 15.0
    finally:
        conn.close()


def test_recompute_aggregates(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", camera="alley-wide", start=now, labels=("person",)),
            LinkDecision(None, "new", 1.0),
            now,
        )
        store.upsert_atom(
            conn,
            _atom(
                "a2",
                camera="shed",
                start=now + 10,
                labels=("person", "dog"),
                sub_labels=("rex",),
                severity="alert",
            ),
            LinkDecision(enc_id, "adjacent", 0.6),
            now + 10,
        )
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["atom_count"] == 2
        assert row["peak_severity"] == "alert"
        import json

        assert json.loads(row["cameras_json"]) == ["alley-wide", "shed"]
        assert set(json.loads(row["labels_json"])) == {"person", "dog"}
        assert set(json.loads(row["identities_json"])) == {"rex"}
    finally:
        conn.close()


def test_seal_stale_seals_quiet_encounters(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 10000, end_time=now - 9990),
            LinkDecision(None, "new", 1.0),
            now - 10000,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 1
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is not None
        # sealed encounters are not returned by load_open
        assert enc_id not in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_leaves_fresh_encounters_open(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 5, end_time=now - 1),
            LinkDecision(None, "new", 1.0),
            now - 5,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 0
        assert enc_id in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_never_seals_an_open_member_below_max_duration(
    sidecar_db_path: Path,
) -> None:
    """A member with end_time IS NULL is still active. Before the fix,
    seal_stale's `COALESCE(m.end_time, m.start_time)` treated it as having
    ended at its own start -- so an encounter whose only member started 500s
    ago (well under max_duration_s=1800) but never got an "end" message
    (still genuinely live) looked "quiet" for 500s -- past the 270s
    (1.5x the largest 180s gap) staleness threshold -- and got sealed out
    from under the linker. It must stay open as long as its span hasn't hit
    max_duration_s."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 500, end_time=None),  # still open, no end message
            LinkDecision(None, "new", 1.0),
            now - 500,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 0
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is None
        assert enc_id in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_still_seals_an_open_member_past_max_duration(
    sidecar_db_path: Path,
) -> None:
    """An open member is not exempt from the hard `max_duration_s` cap --
    only from the "gone quiet" rule."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - CFG.max_duration_s - 100, end_time=None),
            LinkDecision(None, "new", 1.0),
            now - CFG.max_duration_s - 100,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 1
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is not None
    finally:
        conn.close()


def test_watermark_roundtrip(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        assert store.get_watermark(conn) is None
        store.set_watermark(conn, 12345.5)
        assert store.get_watermark(conn) == 12345.5
        store.set_watermark(conn, 12400.0)
        assert store.get_watermark(conn) == 12400.0
    finally:
        conn.close()
