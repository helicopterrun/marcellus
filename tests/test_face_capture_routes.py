"""Route tests for /faces/captures."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FaceCaptureSection,
    FaceEnrichSection,
    FrigateSection,
    Settings,
    SidecarSection,
)
from marcellus.faces.handles import handle_for
from marcellus.server import create_app


def _jpeg(w: int = 64, h: int = 36) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _seed(settings: Settings) -> dict[str, int]:
    """Two captures in one visit, plus a row whose file is missing."""
    out_dir = Path(settings.face_capture.output_dir)
    (out_dir / "2026-08-22").mkdir(parents=True, exist_ok=True)
    for name in ("a.jpg", "a.thumb.jpg", "b.jpg"):
        (out_dir / "2026-08-22" / name).write_bytes(_jpeg())

    conn = db.open_sidecar(settings.sidecar.db_path)
    ids: dict[str, int] = {}
    try:
        rows = [
            ("d1", "a", "2026-08-22/a.jpg", "2026-08-22/a.thumb.jpg", 0),
            ("d1", "b", "2026-08-22/b.jpg", None, 4000),
            ("d1", "gone", "2026-08-22/missing.jpg", None, 8000),
            ("d1", "evil", "../../../etc/passwd", None, 12000),
        ]
        for eid, tag, full, thumb, off in rows:
            cur = conn.execute(
                "INSERT INTO face_captures (trigger_event_id, trigger_camera, "
                "trigger_label, trigger_start_ts, visit_key, capture_camera, offset_ms, "
                "frame_ts, status, full_path, thumb_path, width, height, bytes, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    eid, "doorbell", "person", 1_787_400_000.0, "d1", "gate-face", off,
                    1_787_400_000.0, "captured", full, thumb, 2560, 1440, 100, "now",
                ),
            )
            ids[tag] = int(cur.lastrowid or 0)
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.fixture
def env(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> tuple[TestClient, dict[str, int]]:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        face_capture=FaceCaptureSection(
            enabled=True,
            capture_camera="gate-face",
            trigger_cameras=["doorbell"],
            output_dir=tmp_path / "captures",
        ),
    )
    ids = _seed(settings)
    return TestClient(create_app(settings)), ids


def test_page_renders_and_groups_by_visit(env: tuple[TestClient, dict[str, int]]) -> None:
    client, _ = env
    r = client.get("/faces/captures?days=365")
    assert r.status_code == 200
    assert "gate-face" in r.text
    assert "doorbell" in r.text


def test_json_groups_captures_under_one_visit(env: tuple[TestClient, dict[str, int]]) -> None:
    client, _ = env
    r = client.get("/faces/captures.json?days=365")
    assert r.status_code == 200
    visits = r.json()["visits"]
    assert len(visits) == 1
    assert visits[0]["visit_key"] == "d1"
    # offsets sorted ascending
    offs = [c["offset_ms"] for c in visits[0]["captures"]]
    assert offs == sorted(offs)


def test_full_and_thumb_are_served_by_row_id(env: tuple[TestClient, dict[str, int]]) -> None:
    client, ids = env
    assert client.get(f"/faces/captures/{ids['a']}/full.jpg").status_code == 200
    assert client.get(f"/faces/captures/{ids['a']}/thumb.jpg").status_code == 200
    # a row with no thumb_path 404s rather than serving the full frame
    assert client.get(f"/faces/captures/{ids['b']}/thumb.jpg").status_code == 404


def test_unknown_id_and_missing_file_404(env: tuple[TestClient, dict[str, int]]) -> None:
    client, ids = env
    assert client.get("/faces/captures/999999/full.jpg").status_code == 404
    assert client.get(f"/faces/captures/{ids['gone']}/full.jpg").status_code == 404


def test_hand_edited_row_cannot_escape_output_dir(
    env: tuple[TestClient, dict[str, int]],
) -> None:
    """The containment assertion, not a filename guard: ids are ours, paths are not."""
    client, ids = env
    r = client.get(f"/faces/captures/{ids['evil']}/full.jpg")
    assert r.status_code == 404
    assert "root:" not in r.text


def test_review_single_and_whole_visit(env: tuple[TestClient, dict[str, int]]) -> None:
    client, ids = env
    r = client.post(f"/faces/captures/{ids['a']}/review", json={"review": "kept"})
    assert r.status_code == 200 and r.json()["updated"] == 1

    r = client.post(
        f"/faces/captures/{ids['b']}/review", json={"review": "discarded", "visit": True}
    )
    assert r.status_code == 200 and r.json()["updated"] >= 2

    assert client.get("/faces/captures.json?days=365&review=pending").json()["visits"] == []


def test_captures_json_rejects_out_of_range_days_and_limit(
    env: tuple[TestClient, dict[str, int]],
) -> None:
    """`days` and `limit` go straight into the SQL query -- clamp them
    server-side rather than trusting the client."""
    client, _ids = env
    assert client.get("/faces/captures.json?days=366").status_code == 422
    assert client.get("/faces/captures.json?days=0").status_code == 422
    assert client.get("/faces/captures.json?limit=501").status_code == 422
    assert client.get("/faces/captures.json?limit=0").status_code == 422
    # In-range values still work.
    assert client.get("/faces/captures.json?days=365&limit=500").status_code == 200


def test_captures_view_rejects_out_of_range_days_and_limit(
    env: tuple[TestClient, dict[str, int]],
) -> None:
    client, _ids = env
    assert client.get("/faces/captures?days=366").status_code == 422
    assert client.get("/faces/captures?limit=501").status_code == 422


def test_review_rejects_a_bogus_value(env: tuple[TestClient, dict[str, int]]) -> None:
    client, ids = env
    assert (
        client.post(f"/faces/captures/{ids['a']}/review", json={"review": "banana"}).status_code
        == 400
    )


def test_scan_route_503s_when_disabled(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        face_capture=FaceCaptureSection(enabled=False),
    )
    client = TestClient(create_app(settings))
    assert client.post("/faces/captures/scan").status_code == 503


def test_routes_are_behind_the_auth_gate(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """Proves the router registered inside owned_routes, before the proxy catch-all."""
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=True
        ),
        face_capture=FaceCaptureSection(enabled=True, output_dir=tmp_path / "cap"),
    )
    client = TestClient(create_app(settings))
    for path in ("/faces/captures", "/faces/captures.json", "/faces/captures/1/full.jpg"):
        assert client.get(path).status_code in (401, 403), path


def test_settled_rows_do_not_crowd_visits_out_of_the_limit(
    env: tuple[TestClient, dict[str, int]],
) -> None:
    """The gate settles most sampled frames as no_face; a LIMIT applied before
    filtering to 'captured' used to hide older visits (5 of 43 shown)."""
    client, _ = env
    settings = client.app.state.settings  # type: ignore[attr-defined]
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        for off in range(300):
            conn.execute(
                "INSERT INTO face_captures (trigger_event_id, trigger_camera, trigger_label, "
                "trigger_start_ts, visit_key, capture_camera, offset_ms, frame_ts, status, "
                "created_at) VALUES ('d2', 'doorbell', 'person', ?, 'd2', 'gate-face', ?, ?, "
                "'no_face', 'now')",
                (1_787_500_000.0, off, 1_787_500_000.0),
            )
        conn.commit()
    finally:
        conn.close()
    visits = client.get("/faces/captures.json?days=365&limit=200").json()["visits"]
    assert [v["visit_key"] for v in visits] == ["d1"]


def test_face_box_and_best_frame(env: tuple[TestClient, dict[str, int]]) -> None:
    client, ids = env
    settings = client.app.state.settings  # type: ignore[attr-defined]
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "UPDATE face_captures SET face_box = '[0.1, 0.2, 0.3, 0.4]', face_score = 0.9 "
            "WHERE id = ?", (ids["b"],),
        )
        conn.execute(
            "UPDATE face_captures SET face_box = 'not json', face_score = 0.7 WHERE id = ?",
            (ids["a"],),
        )
        conn.commit()
    finally:
        conn.close()
    visit = client.get("/faces/captures.json?days=365").json()["visits"][0]
    assert visit["best"]["id"] == ids["b"]
    boxes = {c["id"]: c["face_box"] for c in visit["captures"]}
    assert boxes[ids["b"]] == [0.1, 0.2, 0.3, 0.4] and boxes[ids["a"]] is None
    html = client.get("/faces/captures?days=365").text
    assert 'data-box="[0.1, 0.2, 0.3, 0.4]"' in html
    # Filter tabs carry per-state visit counts.
    assert "To review" in html and '<span class="pp-n">1</span>' in html


def test_visit_links_to_identities_seen_on_the_capture_camera(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        face_capture=FaceCaptureSection(
            enabled=True, capture_camera="gate-face", trigger_cameras=["doorbell"],
            output_dir=tmp_path / "captures", event_window_s=30.0,
        ),
        face_enrich=FaceEnrichSection(cameras=["gate-face"], crops_dir=tmp_path / "crops"),
    )
    _seed(settings)
    t = 1_787_400_000.0
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        for name in ("bob", None, None):
            conn.execute(
                "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
                "last_seen_at) VALUES (?, x'00', 5, '', 0)", (name,),
            )
        for eid, cid, ts, cam, excluded in (
            ("in", 2, t + 10, "gate-face", None),       # unknown #2, in window
            ("named", 1, t - 20, "gate-face", None),    # bob, in window
            ("far", 3, t + 100, "gate-face", None),     # outside the window
            ("other-cam", 3, t, "doorbell", None),      # not a capture camera
            ("excl", 3, t, "gate-face", "2026-10-01"),  # excluded sighting
        ):
            conn.execute(
                "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                "status, processed_at, excluded_at) VALUES (?, ?, ?, ?, 'enriched', '', ?)",
                (eid, cam, ts, cid, excluded),
            )
        conn.commit()
    finally:
        conn.close()
    client = TestClient(create_app(settings))
    visit = client.get("/faces/captures.json?days=365").json()["visits"][0]
    assert [(i["cluster_id"], i["name"]) for i in visit["identities"]] == [(1, "bob"), (2, None)]
    html = client.get("/faces/captures?days=365").text
    assert "/enrich/clusters?cluster=1" in html and f"{handle_for(2)} · #2" in html
    assert "/enrich/clusters?cluster=3" not in html
