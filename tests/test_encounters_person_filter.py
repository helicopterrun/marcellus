"""`GET /v1/encounters?person=` and the rejected-face quality column."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FaceEnrichSection, FrigateSection, Settings, SidecarSection
from marcellus.faces import enrich
from marcellus.server import create_app
from tests.test_encounters_routes import _seed_at
from tests.test_face_enrich_routes import (
    _cand,
    _joined,
    _StubClient,
    _StubEngine,
    stub_engine,  # noqa: F401  (fixture)
)

NOW = time.time()
HOUR = 3600.0


@pytest.fixture
def settings(tmp_path: Path, frigate_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
        face_enrich=FaceEnrichSection(enabled=True),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _cluster(settings: Settings, cid: int) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO face_clusters (cluster_id, centroid, observation_count, "
            "created_at, last_seen_at) VALUES (?, ?, 1, '', 0)",
            (cid, enrich.pack_embedding([1.0, 0.0])),
        )
        conn.commit()
    finally:
        conn.close()


def _face(settings: Settings, atom_id: str, cid: int, *, excluded: bool = False) -> None:
    _cluster(settings, cid)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
            "status, processed_at, excluded_at) VALUES (?, 'alley-wide', 0, ?, 'enriched', '', ?)",
            (f"ev-{atom_id}", cid, "x" if excluded else None),
        )
        conn.commit()
    finally:
        conn.close()


def _ids(client: TestClient, **params: object) -> list[str]:
    resp = client.get("/v1/encounters", params=params)  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()["encounters"]]


def test_person_filter_matches_and_excludes(settings: Settings, client: TestClient) -> None:
    mine = _seed_at(settings, "a1", start=NOW - HOUR)
    other = _seed_at(settings, "a2", start=NOW - 2 * HOUR)
    gone = _seed_at(settings, "a3", start=NOW - 3 * HOUR)
    _seed_at(settings, "a4", start=NOW - 4 * HOUR)  # no face at all
    _face(settings, "a1", 1)
    _face(settings, "a2", 2)
    _face(settings, "a3", 1, excluded=True)
    assert _ids(client, person=1) == [mine]
    assert _ids(client, person=2) == [other]
    assert gone not in _ids(client, person=1)


def test_person_filter_multi_member_encounter(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "m1", start=NOW - HOUR)
    _seed_at(settings, "m2", start=NOW - HOUR + 30, camera="shed", encounter_id=enc)
    _face(settings, "m2", 7)  # only the second member's event has the face
    assert _ids(client, person=7) == [enc]


def test_person_filter_merged_old_id_resolves(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "a1", start=NOW - HOUR)
    _face(settings, "a1", 1)
    _cluster(settings, 2)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        enrich.merge_clusters(conn, 1, 2)
        conn.commit()
    finally:
        conn.close()
    assert _ids(client, person=1) == [enc]
    assert _ids(client, person=2) == [enc]


def test_person_filter_unknown_id_404(client: TestClient) -> None:
    resp = client.get("/v1/encounters", params={"person": 999})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "person_not_found"


def test_person_filter_disabled_is_400(settings: Settings) -> None:
    off = settings.model_copy(update={"face_enrich": FaceEnrichSection(enabled=False)})
    resp = TestClient(create_app(off)).get("/v1/encounters", params={"person": 1})
    assert resp.status_code == 400


def test_person_filter_paging_counts_filtered_rows(
    settings: Settings, client: TestClient
) -> None:
    ids = []
    for i in range(6):
        ids.append(_seed_at(settings, f"p{i}", start=NOW - (i + 1) * HOUR))
        if i % 2 == 0:
            _face(settings, f"p{i}", 1)
    # Newest-first: p0, p2, p4 match. limit=2 must return two matches, not
    # the matches among the two newest rows.
    page1 = client.get("/v1/encounters", params={"person": 1, "limit": 2}).json()["encounters"]
    assert [e["id"] for e in page1] == [ids[0], ids[2]]
    page2 = _ids(client, person=1, limit=2, before=page1[-1]["start"])
    assert page2 == [ids[4]]


def test_person_filter_ands_with_other_filters(settings: Settings, client: TestClient) -> None:
    a = _seed_at(settings, "c1", start=NOW - HOUR, camera="alley-wide", severity="alert")
    b = _seed_at(settings, "c2", start=NOW - 2 * HOUR, camera="shed", severity="alert")
    c = _seed_at(settings, "c3", start=NOW - 3 * HOUR, camera="alley-wide", severity="detection")
    for aid in ("c1", "c2", "c3"):
        _face(settings, aid, 1)
    assert _ids(client, person=1) == [a, b, c]
    assert _ids(client, person=1, camera="alley-wide") == [a, c]
    assert _ids(client, person=1, severity="alert") == [a, b]
    assert _ids(client, person=1, camera="alley-wide", severity="alert") == [a]


def test_person_filter_is_behind_the_auth_gate(
    settings: Settings,
) -> None:
    gated = settings.model_copy(
        update={
            "sidecar": SidecarSection(db_path=settings.sidecar.db_path, require_frigate_auth=True)
        }
    )
    anon = TestClient(create_app(gated))
    assert anon.get("/v1/encounters", params={"person": 1}).status_code in (401, 403)


def test_capabilities_encounter_filter(settings: Settings) -> None:
    on = TestClient(create_app(settings)).get("/v1/capabilities").json()["people"]
    assert on["encounter_filter"] is True
    off = settings.model_copy(update={"face_enrich": FaceEnrichSection(enabled=False)})
    assert TestClient(create_app(off)).get("/v1/capabilities").json()["people"][
        "encounter_filter"
    ] is False


def test_person_filter_query_time_on_a_few_thousand_encounters(settings: Settings) -> None:
    from marcellus.encounters import store

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO face_clusters (cluster_id, centroid, observation_count, created_at, "
            "last_seen_at) VALUES (1, ?, 1, '', 0)",
            (enrich.pack_embedding([1.0, 0.0]),),
        )
        n = 4000
        for i in range(n):
            conn.execute(
                "INSERT INTO encounters (id, start_time, end_time, updated_at, sealed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"e{i}", NOW - i * 600.0, NOW - i * 600.0 + 30, NOW, NOW),
            )
            for k in range(2):
                conn.execute(
                    "INSERT INTO encounter_members (atom_id, encounter_id, camera, start_time, "
                    "severity, event_ids_json, link_reason, confidence, joined_at) "
                    "VALUES (?, ?, 'alley-wide', ?, 'alert', ?, 'new', 1.0, 0)",
                    (f"e{i}-{k}", f"e{i}", NOW - i * 600.0, f'["ev{i}-{k}"]'),
                )
                conn.execute(
                    "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                    "status, processed_at) VALUES (?, 'alley-wide', 0, ?, 'enriched', '')",
                    (f"ev{i}-{k}", 1 if i % 400 == 399 else None),
                )
        conn.commit()
        t0 = time.perf_counter()
        rows = store.list_recent(conn, since=0.0, limit=200, person_cluster=1)
        elapsed = time.perf_counter() - t0
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM encounters WHERE EXISTS (SELECT 1 FROM "
            "encounter_members m, json_each(m.event_ids_json) j JOIN face_enrichments fe ON "
            "fe.event_id = j.value WHERE m.encounter_id = encounters.id AND fe.cluster_id = 1 "
            "AND fe.excluded_at IS NULL) ORDER BY start_time DESC"
        ).fetchall()
        print(f"PERSON_FILTER_MS={elapsed * 1000:.1f} rows={len(rows)}")
        print("\n".join(str(tuple(r)) for r in plan))
    finally:
        conn.close()
    assert len(rows) == 10
    assert elapsed < 2.0


# --------------------------------------------------------------------------
# rejected_best_quality
# --------------------------------------------------------------------------


def _row(conn: sqlite3.Connection, eid: str) -> sqlite3.Row:
    row: sqlite3.Row = conn.execute(
        "SELECT * FROM face_enrichments WHERE event_id = ?", (eid,)
    ).fetchone()
    return row


def test_rejected_best_quality_set_when_all_faces_fall_under_gate(
    tmp_path: Path, stub_engine: _StubEngine  # noqa: F811
) -> None:
    cfg = FaceEnrichSection(cameras=["gate-face"])
    conn = _joined(tmp_path)
    try:
        stub_engine.quality = 0.01
        stub_engine.by_ts = {105.0: 0.04}
        assert enrich.process_event(conn, _StubClient(), _cand("lq"), cfg=cfg) == "no_faces"  # type: ignore[arg-type]
        row = _row(conn, "lq")
        assert row["rejected_best_quality"] == pytest.approx(0.04)
        assert row["best_quality"] is None and row["faces_used"] == 0
        assert row["best_crop_path"] is None
    finally:
        conn.close()


def test_rejected_best_quality_null_when_nothing_passed_the_gates(
    tmp_path: Path, stub_engine: _StubEngine  # noqa: F811
) -> None:
    cfg = FaceEnrichSection(cameras=["gate-face"])
    conn = _joined(tmp_path)
    try:
        stub_engine.faces_in_jpeg = lambda *a, **k: []  # type: ignore[method-assign]
        assert enrich.process_event(conn, _StubClient(), _cand("nf"), cfg=cfg) == "no_faces"  # type: ignore[arg-type]
        assert _row(conn, "nf")["rejected_best_quality"] is None
    finally:
        conn.close()


def test_rejected_best_quality_null_on_enriched(
    tmp_path: Path, stub_engine: _StubEngine  # noqa: F811
) -> None:
    cfg = FaceEnrichSection(cameras=["gate-face"])
    conn = _joined(tmp_path)
    try:
        assert enrich.process_event(conn, _StubClient(), _cand("ok"), cfg=cfg) == "enriched"  # type: ignore[arg-type]
        assert _row(conn, "ok")["rejected_best_quality"] is None
    finally:
        conn.close()


def test_rejected_best_quality_migrates_an_old_db(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE face_enrichments (event_id TEXT PRIMARY KEY, camera TEXT NOT NULL, "
        "event_start_ts REAL NOT NULL, cluster_id INTEGER, distance REAL, "
        "faces_found INTEGER NOT NULL DEFAULT 0, faces_used INTEGER NOT NULL DEFAULT 0, "
        "best_quality REAL, embedding BLOB, sub_label_written TEXT, status TEXT NOT NULL, "
        "attempts INTEGER NOT NULL DEFAULT 0, detail TEXT, processed_at TEXT NOT NULL, "
        "excluded_at TEXT, best_frame_ts REAL, best_bbox TEXT, best_crop_path TEXT, "
        "stack_status TEXT)"
    )
    old.execute(
        "INSERT INTO face_enrichments (event_id, camera, event_start_ts, status, processed_at) "
        "VALUES ('e', 'c', 1.0, 'no_faces', '')"
    )
    old.commit()
    old.close()
    conn = db.open_sidecar(path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(face_enrichments)")}
        assert "rejected_best_quality" in cols
        assert _row(conn, "e")["rejected_best_quality"] is None
    finally:
        conn.close()


def test_rejected_best_quality_is_not_on_any_wire() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "marcellus"
    for rel in ("models/wire.py", "routes/people.py", "faces/people.py"):
        assert "rejected_best_quality" not in (root / rel).read_text(), rel
