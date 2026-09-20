from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from marcellus import db


def test_open_frigate_ro_rejects_writes(frigate_db_path: Path) -> None:
    conn = db.open_frigate_ro(frigate_db_path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO event (id, camera, label, start_time) VALUES ('x','c','l',0)")


def test_open_sidecar_creates_schema(sidecar_db_path: Path) -> None:
    assert not sidecar_db_path.exists()
    conn = db.open_sidecar(sidecar_db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(triage_labels)")}
    assert cols == {"event_id", "label", "note", "labeled_at", "session"}
    conn.close()


def test_open_sidecar_idempotent(sidecar_db_path: Path) -> None:
    db.open_sidecar(sidecar_db_path).close()
    db.open_sidecar(sidecar_db_path).close()
    conn = sqlite3.connect(sidecar_db_path)
    n = conn.execute("SELECT COUNT(*) FROM triage_labels").fetchone()[0]
    assert n == 0


def test_open_sidecar_backfills_dwell_seconds_on_a_pre_migration_activities_table(
    sidecar_db_path: Path,
) -> None:
    """`push_activities` shipped without `dwell_seconds`, which was later added
    to `SIDECAR_SCHEMA`'s CREATE TABLE -- a no-op against an already-existing
    table. A deployment whose table predates the column needs the ALTER in
    `_ADDED_COLUMNS`, exactly like `push_devices`/`push_handles` get; without
    it every `touch_activity(dwell_seconds=...)` call raises
    "no such column: dwell_seconds"."""
    conn = sqlite3.connect(sidecar_db_path)
    conn.execute(
        "CREATE TABLE push_activities ("
        " activity_id TEXT PRIMARY KEY, apns_token TEXT NOT NULL, "
        " situation_id TEXT NOT NULL, track_id TEXT NOT NULL, "
        " camera TEXT NOT NULL DEFAULT '', token TEXT NOT NULL DEFAULT '', "
        " collapse_id TEXT NOT NULL DEFAULT '', handle TEXT NOT NULL DEFAULT '', "
        " stage TEXT NOT NULL DEFAULT 'arriving', thumbnail_revision INTEGER NOT NULL DEFAULT 1, "
        " from_detection INTEGER NOT NULL DEFAULT 0, promoted INTEGER NOT NULL DEFAULT 0, "
        " created_at REAL NOT NULL, last_push_at REAL NOT NULL DEFAULT 0, "
        " last_seen_at REAL NOT NULL DEFAULT 0, ended_at REAL)"
    )
    conn.commit()
    conn.close()

    from marcellus.push import store

    conn = db.open_sidecar(sidecar_db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(push_activities)")}
    assert "dwell_seconds" in cols

    store.open_activity(
        conn, activity_id="a1", apns_token="tok", situation_id="s1", track_id="t1",
        camera="doorbell", collapse_id="c1", handle="h1", now=1.0,
    )
    store.touch_activity(conn, "a1", dwell_seconds=5, now=2.0)  # must not raise
    conn.commit()
    row = store.get_activity(conn, "a1")
    assert row["dwell_seconds"] == 5
    conn.close()


def test_open_sidecar_backfills_excluded_at_on_a_pre_migration_enrichments_table(
    sidecar_db_path: Path,
) -> None:
    """`face_enrichments` shipped without `excluded_at` (Wave 6B-2's soft
    exclude); a deployment whose table predates the column needs the ALTER in
    `_ADDED_COLUMNS`, same story as `push_activities.dwell_seconds`."""
    conn = sqlite3.connect(sidecar_db_path)
    conn.execute(
        "CREATE TABLE face_enrichments ("
        " event_id TEXT PRIMARY KEY, camera TEXT NOT NULL, event_start_ts REAL NOT NULL, "
        " cluster_id INTEGER, distance REAL, faces_found INTEGER NOT NULL DEFAULT 0, "
        " faces_used INTEGER NOT NULL DEFAULT 0, best_quality REAL, embedding BLOB, "
        " sub_label_written TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        " detail TEXT, processed_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    conn = db.open_sidecar(sidecar_db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(face_enrichments)")}
    assert "excluded_at" in cols
    # Must not raise "no such column" -- the whole point of the ALTER.
    conn.execute(
        "INSERT INTO face_enrichments (event_id, camera, event_start_ts, status, "
        "processed_at, excluded_at) VALUES ('e1', 'cam', 0, 'enriched', '', ?)",
        ("2026-08-31T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()


def test_open_joined_round_trip(frigate_db_path: Path, sidecar_db_path: Path) -> None:
    conn = db.open_joined(frigate_db_path, sidecar_db_path)
    # Insert a triage label via the joined handle.
    conn.execute(
        "INSERT INTO sidecar.triage_labels (event_id, label, labeled_at, session) "
        "VALUES (?, ?, ?, ?)",
        ("e1", "tp", "2026-05-15T12:00:00Z", "test"),
    )
    conn.commit()

    # Join Frigate's events with our labels.
    rows = conn.execute(
        """
        SELECT e.id, e.camera, t.label
          FROM main.event e
          LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
         WHERE e.id = ?
        """,
        ("e1",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["camera"] == "alley-overview"
    assert rows[0]["label"] == "tp"


def test_parse_event_data(frigate_db_path: Path) -> None:
    conn = db.open_frigate_ro(frigate_db_path)
    row = conn.execute("SELECT * FROM event WHERE id = 'e1'").fetchone()
    parsed = db.parse_event_data(row)
    assert parsed["data_score"] == 0.92
    assert parsed["data_top_score"] == 0.94
    assert parsed["data_box"] == [0.1, 0.2, 0.3, 0.4]
    assert parsed["data_type"] == "object"


def test_time_window_clause() -> None:
    sql, params = db.time_window_clause(7)
    assert sql == "start_time >= ?"
    assert len(params) == 1
    assert time.time() - params[0] == pytest.approx(7 * 86400, abs=2)


def test_percentile_basics() -> None:
    assert db.percentile([], 50) != db.percentile([], 50)  # NaN
    assert db.percentile([1.0], 0) == 1.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0) == 1.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0
    assert db.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0


def test_recording_coverage_merges_across_segment_seams(tmp_path: Path) -> None:
    """§4.4 promises merged intervals, not raw segments.

    Consecutive Frigate segments don't abut exactly -- measured live, 2052 of
    2063 seams on `street` were under 0.1s (median 3.3ms) -- so an
    exact-adjacency join never fired and six hours came back as 2064 intervals
    describing what ~15 do. Real discontinuities are over a second.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, path TEXT, "
        "start_time REAL, end_time REAL);"
    )
    base = 1_800_000_000.0
    rows = []
    t = base
    for i in range(60):
        rows.append((f"s{i}", "street", "/x.mp4", t, t + 10.0))
        # Millisecond seam, as real segments have.
        t += 10.0033
    # One genuine outage: a 5 minute hole.
    t += 300.0
    for i in range(60, 90):
        rows.append((f"s{i}", "street", "/x.mp4", t, t + 10.0))
        t += 10.0033
    conn.executemany("INSERT INTO recordings VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()

    ro = db.open_frigate_ro(frigate_db)
    try:
        result = db.recording_coverage(ro, "street", base, t + 10, now=t + 20)
    finally:
        ro.close()

    recorded = result["recorded"]
    assert len(recorded) == 2, f"expected 2 intervals around the outage, got {len(recorded)}"
    assert recorded[1][0] - recorded[0][1] > 250, "the real outage must survive the merge"


def test_recording_coverage_keeps_gaps_above_the_tolerance(tmp_path: Path) -> None:
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, path TEXT, "
        "start_time REAL, end_time REAL);"
    )
    base = 1_800_000_000.0
    conn.executemany(
        "INSERT INTO recordings VALUES (?, ?, ?, ?, ?)",
        [
            ("a", "street", "/x.mp4", base, base + 10.0),
            # 0.5s: above the 0.25s tolerance, so a distinct interval.
            ("b", "street", "/x.mp4", base + 10.5, base + 20.0),
        ],
    )
    conn.commit()
    conn.close()

    ro = db.open_frigate_ro(frigate_db)
    try:
        result = db.recording_coverage(ro, "street", base, base + 30, now=base + 30)
    finally:
        ro.close()
    assert len(result["recorded"]) == 2


def test_added_columns_has_no_duplicate_table_keys():
    """A duplicate table key in the _ADDED_COLUMNS dict literal silently
    clobbers the earlier entry (Python dict semantics) — how the zones_csv
    migration vanished on 2026-08-14 and broke every card upsert in
    production. Parse the source: each table may appear exactly once."""
    import ast
    import inspect

    from marcellus import db as db_module

    tree = ast.parse(inspect.getsource(db_module))
    for node in ast.walk(tree):
        target_names = []
        if isinstance(node, ast.Assign):
            target_names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_names = [node.target.id]
        if "_ADDED_COLUMNS" in target_names and node.value is not None:
            keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
            assert len(keys) == len(set(keys)), f"duplicate table keys: {keys}"
            break
    else:
        raise AssertionError("_ADDED_COLUMNS assignment not found")


def test_read_with_retry_succeeds_after_transient_locks() -> None:
    calls = {"n": 0}

    def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = db.read_with_retry(_flaky, attempts=3, backoff=0.001)
    assert result == "ok"
    assert calls["n"] == 3


def test_read_with_retry_raises_typed_error_when_always_locked() -> None:
    def _always_locked() -> None:
        raise sqlite3.OperationalError("database table is locked")

    with pytest.raises(db.DBLockedError):
        db.read_with_retry(_always_locked, attempts=3, backoff=0.001)


def test_read_with_retry_does_not_retry_a_non_locking_operational_error() -> None:
    calls = {"n": 0}

    def _bad_sql() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: bogus")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db.read_with_retry(_bad_sql, attempts=3, backoff=0.001)
    assert calls["n"] == 1


def test_open_sidecar_migrates_pre_zones_csv_card_table(tmp_path):
    """A DB created before zones_csv must gain the column on open — the
    dedup query and every card upsert read it."""
    import sqlite3

    from marcellus import db as db_module

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE push_cards ("
        " card_key TEXT PRIMARY KEY, level TEXT NOT NULL,"
        " subject_kind TEXT NOT NULL DEFAULT '', place_class TEXT NOT NULL DEFAULT '',"
        " camera TEXT NOT NULL DEFAULT '', zone_name TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL, updated_at REAL NOT NULL,"
        " state_since_at REAL NOT NULL, sound_count INTEGER NOT NULL DEFAULT 0,"
        " handled INTEGER NOT NULL DEFAULT 0, handled_at REAL, last_sound_at REAL,"
        " resound_count INTEGER NOT NULL DEFAULT 0, resolved INTEGER NOT NULL DEFAULT 0,"
        " closed INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    migrated = db_module.open_sidecar(path)
    cols = {r["name"] for r in migrated.execute("PRAGMA table_info(push_cards)")}
    assert "zones_csv" in cols
    assert "peak_level" in cols
