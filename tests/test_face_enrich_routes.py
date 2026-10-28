"""Route tests for /enrich/clusters, plus process_event/run_cycle with a
stubbed engine — the orchestration paths minus actual inference."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FaceEnrichSection,
    FrigateSection,
    Settings,
    SidecarSection,
)
from marcellus.faces import enrich
from marcellus.frigate_api import FrigateAPIError, FrigateClient
from marcellus.server import create_app
from tests.conftest import FRIGATE_EVENT_SCHEMA


def _seed_clusters(settings: Settings) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        for name, emb in (("alice", [1.0, 0.0]), (None, [0.0, 1.0])):
            conn.execute(
                "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
                "last_seen_at) VALUES (?, ?, 2, '', ?)",
                (name, enrich.pack_embedding(emb), time.time()),
            )
        for eid, cid in (("ev-a", 1), ("ev-b", 2)):
            conn.execute(
                "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                "embedding, best_quality, status, processed_at) "
                "VALUES (?, 'gate-face', 0, ?, ?, 0.5, 'enriched', '')",
                (eid, cid, enrich.pack_embedding([1.0, 0.0])),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        face_enrich=FaceEnrichSection(cameras=["gate-face"], crops_dir=tmp_path / "crops"),
    )
    _seed_clusters(settings)
    return TestClient(create_app(settings))


def test_clusters_page_renders(client: TestClient) -> None:
    r = client.get("/enrich/clusters")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "unknown #2" in r.text


def test_clusters_json_orders_named_first(client: TestClient) -> None:
    r = client.get("/enrich/clusters.json")
    assert r.status_code == 200
    clusters = r.json()["clusters"]
    assert clusters[0]["name"] == "alice"
    assert clusters[0]["sample_event_id"] == "ev-a"


def test_naming_promotes_rebuilds_and_retro_labels(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[tuple[str, str]] = []

    def _fake_set(self: Any, event_id: str, sub_label: str, *, score: Any = None) -> None:
        if event_id == "ev-expired":
            raise FrigateAPIError("HTTP 404")
        written.append((event_id, sub_label))

    monkeypatch.setattr(FrigateClient, "set_sub_label", _fake_set)
    r = client.post("/enrich/clusters/2/name", json={"name": "mail carrier"})
    assert r.status_code == 200
    # Cluster 2 has one sighting (ev-b); it gets retro-labeled.
    assert r.json()["relabeled"] == 1
    assert written == [("ev-b", "mail carrier")]
    listing = client.get("/enrich/clusters.json").json()["clusters"]
    names = {c["cluster_id"]: c["name"] for c in listing}
    assert names[2] == "mail carrier"


def test_retro_label_survives_expired_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_404(self: Any, event_id: str, sub_label: str, *, score: Any = None) -> None:
        raise FrigateAPIError("HTTP 404")

    monkeypatch.setattr(FrigateClient, "set_sub_label", _always_404)
    r = client.post("/enrich/clusters/1/name", json={"name": "alice"})
    assert r.status_code == 200
    assert r.json()["relabeled"] == 0


def test_naming_rejects_blank_and_missing(client: TestClient) -> None:
    assert client.post("/enrich/clusters/1/name", json={"name": "  "}).status_code == 400
    assert client.post("/enrich/clusters/99/name", json={"name": "x"}).status_code == 404


def test_merge_moves_enrichments_and_deletes_source(client: TestClient) -> None:
    assert client.post("/enrich/clusters/2/merge", json={"into": 1}).status_code == 200
    clusters = client.get("/enrich/clusters.json").json()["clusters"]
    assert [c["cluster_id"] for c in clusters] == [1]
    assert client.post("/enrich/clusters/1/merge", json={"into": 1}).status_code == 400
    assert client.post("/enrich/clusters/1/merge", json={"into": 99}).status_code == 404


def test_delete_clears_assignments(client: TestClient) -> None:
    assert client.post("/enrich/clusters/2/delete").status_code == 200
    assert client.post("/enrich/clusters/2/delete").status_code == 404


def test_thumb_rejects_unknown_event(client: TestClient) -> None:
    assert client.get("/enrich/thumb/not-ours").status_code == 404


def test_page_shows_sightings_stats_and_similar_hint(client: TestClient) -> None:
    r = client.get("/enrich/clusters")
    assert r.status_code == 200
    # Sighting strip: both seeded events render thumbnails + snapshot links.
    assert "/enrich/thumb/ev-a" in r.text
    assert "/api/events/ev-a/snapshot.jpg" in r.text
    # Stats bar renders (both seeded rows have event_start_ts=0, outside 7d).
    assert "enriched" in r.text and "worker:" in r.text
    # Seeded centroids are orthogonal — no similarity hint.
    assert "looks like" not in r.text


def test_similar_hint_appears_for_close_centroids(client: TestClient) -> None:
    # Pull cluster 2's centroid next to cluster 1's.
    conn = sqlite3.connect(client.app.state.settings.sidecar.db_path)  # type: ignore[attr-defined]
    conn.execute(
        "UPDATE face_clusters SET centroid = ? WHERE cluster_id = 2",
        (enrich.pack_embedding(enrich.l2_normalize([0.95, 0.05])),),
    )
    conn.commit()
    conn.close()
    r = client.get("/enrich/clusters")
    assert "looks like" in r.text
    sim = client.get("/enrich/clusters.json").json()["similar"]
    assert sim["2"]["cluster_id"] == 1 and sim["2"]["name"] == "alice"


def test_sighting_remove_detaches_and_deletes_empty_cluster(client: TestClient) -> None:
    # ev-b is cluster 2's only sighting: removing it deletes the cluster.
    r = client.post("/enrich/events/ev-b/remove")
    assert r.status_code == 200 and r.json()["cluster_deleted"] is True
    clusters = client.get("/enrich/clusters.json").json()["clusters"]
    assert [c["cluster_id"] for c in clusters] == [1]
    # ev-a stays in cluster 1, which survives with a rebuilt centroid.
    r = client.post("/enrich/events/ev-a/remove")
    assert r.json()["cluster_deleted"] is True  # it was also the only sighting
    # A detached sighting can't be removed twice.
    assert client.post("/enrich/events/ev-a/remove").status_code == 404


def test_exclude_hides_sighting_and_stats_but_show_excluded_reveals_it(
    client: TestClient,
) -> None:
    r = client.patch("/enrich/events/ev-a", json={"excluded": True})
    assert r.status_code == 200
    body = r.json()
    assert body == {"event_id": "ev-a", "excluded": True, "cluster_id": 1}

    # Hidden by default from the sightings list...
    clusters = client.get("/enrich/clusters.json").json()["clusters"]
    c1 = next(c for c in clusters if c["cluster_id"] == 1)
    assert [s["event_id"] for s in c1["sightings"]] == []

    # ...but present (dimmed) with ?show_excluded=1.
    clusters = client.get("/enrich/clusters.json?show_excluded=1").json()["clusters"]
    c1 = next(c for c in clusters if c["cluster_id"] == 1)
    assert [s["event_id"] for s in c1["sightings"]] == ["ev-a"]
    assert client.get("/enrich/clusters?show_excluded=1").text.count("id-excluded") >= 1


def test_exclude_undo_clears_and_restores_sighting(client: TestClient) -> None:
    assert client.patch("/enrich/events/ev-a", json={"excluded": True}).status_code == 200
    r = client.patch("/enrich/events/ev-a", json={"excluded": False})
    assert r.status_code == 200 and r.json()["excluded"] is False
    clusters = client.get("/enrich/clusters.json").json()["clusters"]
    c1 = next(c for c in clusters if c["cluster_id"] == 1)
    assert [s["event_id"] for s in c1["sightings"]] == ["ev-a"]


def test_exclude_unknown_event_404s(client: TestClient) -> None:
    assert client.patch("/enrich/events/nope", json={"excluded": True}).status_code == 404


def test_exclude_does_not_feed_rebuilt_centroid(client: TestClient) -> None:
    """Excluding a cluster's only remaining sighting must not leave its old
    embedding in the exact centroid rebuild (naming/merge path)."""
    conn = db.open_sidecar(client.app.state.settings.sidecar.db_path)  # type: ignore[attr-defined]
    conn.execute(
        "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
        "embedding, best_quality, status, processed_at) "
        "VALUES ('ev-c', 'gate-face', 0, 1, ?, 0.9, 'enriched', '')",
        (enrich.pack_embedding([0.0, 1.0]),),
    )
    conn.commit()
    conn.close()
    assert client.patch("/enrich/events/ev-a", json={"excluded": True}).status_code == 200
    conn = db.open_sidecar(client.app.state.settings.sidecar.db_path)  # type: ignore[attr-defined]
    row = conn.execute(
        "SELECT centroid FROM face_clusters WHERE cluster_id = 1"
    ).fetchone()
    conn.close()
    centroid = enrich.unpack_embedding(row["centroid"])
    # Only ev-c's [0, 1] embedding remains -- not ev-a's [1, 0].
    assert centroid[1] > centroid[0]


def test_excluded_sighting_drops_out_of_toolbar_stats(client: TestClient) -> None:
    conn = db.open_sidecar(client.app.state.settings.sidecar.db_path)  # type: ignore[attr-defined]
    conn.execute(
        "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
        "best_quality, status, processed_at) "
        "VALUES ('ev-recent', 'gate-face', ?, 1, 0.9, 'enriched', '')",
        (time.time(),),
    )
    conn.commit()
    conn.close()
    before = client.get("/enrich/clusters.json").json()["stats"]["counts"].get("enriched", 0)
    assert client.patch("/enrich/events/ev-recent", json={"excluded": True}).status_code == 200
    after = client.get("/enrich/clusters.json").json()["stats"]["counts"].get("enriched", 0)
    assert after == before - 1


def test_hard_remove_still_works_alongside_soft_exclude(client: TestClient) -> None:
    """The old hard-detach route stays available (audit escape hatch)."""
    r = client.post("/enrich/events/ev-b/remove")
    assert r.status_code == 200 and r.json()["cluster_deleted"] is True


# ---------------------------------------------------------------------------
# process_event / run_cycle with a stubbed engine and client.


class _StubEngine:
    """Yields one good face per frame along a fixed embedding.

    `by_ts` overrides the quality for the frame at that instant, so a test
    can shape an event's best-to-worst order.
    """

    def __init__(self, embedding: list[float], quality: float = 0.8) -> None:
        self.embedding = embedding
        self.quality = quality
        self.by_ts: dict[float, float] = {}

    def faces_in_jpeg(
        self, jpeg: bytes, *, frame_ts: float, min_face_area_px: int
    ) -> list[enrich.DetectedFace]:
        q = next((v for t, v in self.by_ts.items() if abs(t - frame_ts) < 1e-6), self.quality)
        return [
            enrich.DetectedFace(
                frame_ts=frame_ts, area_px=8000.0, quality=q,
                embedding=list(self.embedding), bbox=(0.1, 0.1, 0.15, 0.2),
            )
        ]


class _StubClient:
    def __init__(self, jpeg: bytes | None = b"jpeg") -> None:
        self.jpeg = jpeg
        self.sub_labels: list[tuple[str, str, float | None]] = []

    def recording_snapshot(
        self, camera: str, ts: float, *, timeout: float = 15.0
    ) -> tuple[bytes | None, int]:
        return (self.jpeg, 200 if self.jpeg else 404)

    def set_sub_label(self, event_id: str, sub_label: str, *, score: float | None = None) -> None:
        self.sub_labels.append((event_id, sub_label, score))


def _joined(tmp_path: Path) -> sqlite3.Connection:
    frigate = tmp_path / "f.db"
    c = sqlite3.connect(frigate)
    c.executescript(FRIGATE_EVENT_SCHEMA)
    c.commit()
    c.close()
    return db.open_joined(frigate, tmp_path / "s.db")


def _cand(eid: str = "ev1") -> enrich.Candidate:
    return enrich.Candidate(
        event_id=eid, camera="gate-face", start_time=100.0, end_time=110.0, attempts=0
    )


@pytest.fixture
def stub_engine(monkeypatch: pytest.MonkeyPatch) -> _StubEngine:
    eng = _StubEngine(enrich.l2_normalize([1.0, 1.0]))
    monkeypatch.setattr(enrich, "_engine", lambda _dir: eng)
    return eng


def test_process_event_clusters_but_writes_no_sub_label_for_unnamed(
    tmp_path: Path, stub_engine: _StubEngine
) -> None:
    conn = _joined(tmp_path)
    cfg = FaceEnrichSection(cameras=["gate-face"])
    client = _StubClient()
    try:
        assert enrich.process_event(conn, client, _cand(), cfg=cfg) == "enriched"  # type: ignore[arg-type]
        assert client.sub_labels == []
        row = conn.execute("SELECT * FROM face_enrichments WHERE event_id = 'ev1'").fetchone()
        assert row["status"] == "enriched" and row["cluster_id"] == 1
        assert row["faces_used"] <= cfg.best_n
    finally:
        conn.close()


def test_process_event_named_match_writes_sub_label(
    tmp_path: Path, stub_engine: _StubEngine
) -> None:
    conn = _joined(tmp_path)
    cfg = FaceEnrichSection(cameras=["gate-face"])
    client = _StubClient()
    try:
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES ('alice', ?, 3, '', 0)",
            (enrich.pack_embedding(stub_engine.embedding),),
        )
        enrich.process_event(conn, client, _cand(), cfg=cfg)  # type: ignore[arg-type]
        assert [(e, n) for e, n, _ in client.sub_labels] == [("ev1", "alice")]
        row = conn.execute("SELECT * FROM face_enrichments WHERE event_id = 'ev1'").fetchone()
        assert row["sub_label_written"] == "alice"
    finally:
        conn.close()


def test_process_event_no_frames_and_low_quality(
    tmp_path: Path, stub_engine: _StubEngine
) -> None:
    cfg = FaceEnrichSection(cameras=["gate-face"])
    conn = _joined(tmp_path)
    try:
        no_frames = enrich.process_event(conn, _StubClient(jpeg=None), _cand("e-nf"), cfg=cfg)  # type: ignore[arg-type]
        assert no_frames == "no_frames"
        stub_engine.quality = 0.01  # below min_quality
        assert enrich.process_event(conn, _StubClient(), _cand("e-lq"), cfg=cfg) == "no_faces"  # type: ignore[arg-type]
        statuses = {
            r["event_id"]: r["status"]
            for r in conn.execute("SELECT event_id, status FROM face_enrichments")
        }
        assert statuses == {"e-nf": "no_frames", "e-lq": "no_faces"}
    finally:
        conn.close()


def _frame_jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (40, 80, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def test_process_event_stores_a_best_first_face_stack(
    tmp_path: Path, stub_engine: _StubEngine
) -> None:
    # 11 frames over 100..110 at 0.3; three stand out. best_n=2 and
    # min_quality=0.5 leave ranks 0-1 used, 2 over the gate but past best_n,
    # 3-4 under the gate -- all five kept, best on top.
    cfg = FaceEnrichSection(
        cameras=["gate-face"], crops_dir=tmp_path / "crops", best_n=2, min_quality=0.5,
    )
    stub_engine.quality = 0.3
    stub_engine.by_ts = {105.0: 0.95, 102.0: 0.9, 108.0: 0.6}
    conn = _joined(tmp_path)
    try:
        status = enrich.process_event(conn, _StubClient(jpeg=_frame_jpeg()), _cand(), cfg=cfg)  # type: ignore[arg-type]
        assert status == "enriched"
        stack = [
            (r["rank"], r["quality"], r["frame_ts"], r["used"])
            for r in conn.execute("SELECT * FROM face_crops WHERE event_id = 'ev1' ORDER BY rank")
        ]
        assert stack == [
            (0, pytest.approx(0.95), pytest.approx(105.0), 1),
            (1, pytest.approx(0.9), pytest.approx(102.0), 1),
            (2, pytest.approx(0.6), pytest.approx(108.0), 0),
            (3, pytest.approx(0.3), pytest.approx(100.0), 0),
            (4, pytest.approx(0.3), pytest.approx(101.0), 0),
        ]
        row = conn.execute("SELECT * FROM face_enrichments WHERE event_id = 'ev1'").fetchone()
        assert row["best_quality"] == pytest.approx(0.95)
        assert row["best_frame_ts"] == pytest.approx(105.0)
        assert json.loads(row["best_bbox"]) == [0.1, 0.1, 0.15, 0.2]
        assert row["best_crop_path"] == "1970-01-01/ev1_0.jpg"
        for rank in range(5):
            assert (tmp_path / "crops" / f"1970-01-01/ev1_{rank}.jpg").stat().st_size > 0
    finally:
        conn.close()


def test_process_event_survives_an_undecodable_best_frame(
    tmp_path: Path, stub_engine: _StubEngine
) -> None:
    cfg = FaceEnrichSection(cameras=["gate-face"], crops_dir=tmp_path / "crops")
    conn = _joined(tmp_path)
    try:
        assert enrich.process_event(conn, _StubClient(), _cand(), cfg=cfg) == "enriched"  # type: ignore[arg-type]
        row = conn.execute("SELECT * FROM face_enrichments WHERE event_id = 'ev1'").fetchone()
        assert row["best_crop_path"] is None and row["best_frame_ts"] is not None
        paths = [r[0] for r in conn.execute("SELECT crop_path FROM face_crops")]
        assert paths and all(p is None for p in paths)
    finally:
        conn.close()


def _set_crop(settings: Settings, event_id: str, rel: str) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "UPDATE face_enrichments SET best_crop_path = ? WHERE event_id = ?", (rel, event_id)
        )
        conn.commit()
    finally:
        conn.close()


def test_face_route_serves_the_crop_and_404s_otherwise(client: TestClient) -> None:
    settings = client.app.state.settings  # type: ignore[attr-defined]
    crops = Path(settings.face_enrich.crops_dir)
    (crops / "d").mkdir(parents=True)
    (crops / "d" / "ev-a.jpg").write_bytes(b"\xff\xd8crop")
    _set_crop(settings, "ev-a", "d/ev-a.jpg")
    r = client.get("/enrich/face/ev-a.jpg")
    assert r.status_code == 200 and r.content == b"\xff\xd8crop"
    assert r.headers["content-type"] == "image/jpeg"
    assert client.get("/enrich/face/ev-b.jpg").status_code == 404  # no crop stored
    assert client.get("/enrich/face/nope.jpg").status_code == 404  # not enriched
    # A row pointing outside crops_dir is refused, not read.
    (crops.parent / "secret.jpg").write_bytes(b"secret")
    _set_crop(settings, "ev-b", "../secret.jpg")
    assert client.get("/enrich/face/ev-b.jpg").status_code == 404


def _set_stack(settings: Settings, event_id: str, qualities: list[float]) -> None:
    crops = Path(settings.face_enrich.crops_dir)
    (crops / "d").mkdir(parents=True, exist_ok=True)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        for rank, q in enumerate(qualities):
            rel = f"d/{event_id}_{rank}.jpg"
            (crops / rel).write_bytes(f"face{rank}".encode())
            conn.execute(
                "INSERT INTO face_crops (event_id, rank, quality, frame_ts, bbox, used, "
                "crop_path) VALUES (?, ?, ?, 0, '[]', ?, ?)",
                (event_id, rank, q, 1 if q >= 0.5 else 0, rel),
            )
        conn.commit()
    finally:
        conn.close()


def test_stack_ranks_are_served_and_rendered_best_first(client: TestClient) -> None:
    settings = client.app.state.settings  # type: ignore[attr-defined]
    _set_stack(settings, "ev-a", [0.91, 0.62, 0.31])
    assert client.get("/enrich/face/ev-a/2.jpg").content == b"face2"
    assert client.get("/enrich/face/ev-a/3.jpg").status_code == 404
    html = client.get("/enrich/clusters").text
    srcs = [f"/enrich/face/ev-a/{r}.jpg" for r in range(3)]
    assert all(src in html for src in srcs)
    assert html.index(srcs[0]) < html.index(srcs[1]) < html.index(srcs[2])
    assert html.count("id-face-unused") == 1  # only the 0.31 face missed the gate
    # A sighting without a stack still shows Frigate's thumbnail.
    assert "/enrich/thumb/ev-b" in html and "/enrich/thumb/ev-a" not in html.split(
        'data-event="ev-a"', 1
    )[1].split("</figure>", 1)[0]
    faces = client.get("/enrich/clusters.json").json()["clusters"][0]["sightings"][0]["faces"]
    assert [f["rank"] for f in faces] == [0, 1, 2]


def test_deleting_a_cluster_deletes_its_crops(client: TestClient) -> None:
    settings = client.app.state.settings  # type: ignore[attr-defined]
    crops = Path(settings.face_enrich.crops_dir)
    (crops / "d").mkdir(parents=True)
    for eid in ("ev-a", "ev-b"):
        (crops / "d" / f"{eid}.jpg").write_bytes(b"x")
        _set_crop(settings, eid, f"d/{eid}.jpg")
    _set_stack(settings, "ev-b", [0.8, 0.4])
    assert client.post("/enrich/clusters/2/delete").status_code == 200
    assert not (crops / "d" / "ev-b.jpg").exists()
    assert not (crops / "d" / "ev-b_0.jpg").exists() and not (crops / "d" / "ev-b_1.jpg").exists()
    assert client.get("/enrich/face/ev-b/0.jpg").status_code == 404
    assert (crops / "d" / "ev-a.jpg").exists()  # other cluster untouched
    assert client.get("/enrich/face/ev-b.jpg").status_code == 404


def test_run_cycle_records_errors_and_retries_until_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frigate_db_path: Path
) -> None:
    now = time.time()
    frigate = tmp_path / "f.db"
    c = sqlite3.connect(frigate)
    c.executescript(FRIGATE_EVENT_SCHEMA)
    c.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time) "
        "VALUES ('boom', 'gate-face', 'person', ?, ?)",
        (now - 300, now - 200),
    )
    c.commit()
    c.close()
    settings = Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000", db_path=frigate),
        sidecar=SidecarSection(db_path=tmp_path / "s.db"),
        face_enrich=FaceEnrichSection(cameras=["gate-face"], max_attempts=2),
    )

    def _explode(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("inference exploded")

    monkeypatch.setattr(enrich, "process_event", _explode)
    for expected_attempts in (1, 2):
        summary = enrich.run_cycle(settings, now=now)
        assert summary["errors"] == 1
        conn = db.open_sidecar(settings.sidecar.db_path)
        row = conn.execute("SELECT attempts, status FROM face_enrichments").fetchone()
        conn.close()
        assert row["status"] == "error" and row["attempts"] == expected_attempts
    # Attempts exhausted: the event is no longer a candidate.
    assert enrich.run_cycle(settings, now=now)["candidates"] == 0
