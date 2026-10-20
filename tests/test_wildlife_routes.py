"""Route tests for /wildlife."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FrigateSection,
    Settings,
    SidecarSection,
    WildlifeCamera,
    WildlifeSection,
)
from marcellus.frigate_api import FrigateClient, FrigatePlusError
from marcellus.server import create_app


def _jpeg() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 20, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _seed(settings: Settings) -> dict[str, int]:
    """One pending visit with two hits, plus a hit whose path escapes output_dir."""
    out = Path(settings.wildlife.output_dir) / "2026-10-17"
    out.mkdir(parents=True, exist_ok=True)
    (out / "a.crop.jpg").write_bytes(_jpeg())
    (out / "a.frame.jpg").write_bytes(_jpeg())
    now = time.time()
    conn = db.open_sidecar(settings.sidecar.db_path)
    ids: dict[str, int] = {}
    try:
        cur = conn.execute(
            "INSERT INTO wildlife_visits (camera, start_ts, end_ts, hits, created_at) "
            "VALUES ('gate-walkway', ?, ?, 3, 'now')",
            (now - 600, now - 560),
        )
        ids["visit"] = int(cur.lastrowid or 0)
        for tag, crop, frame, sug, dt in (
            ("a", "2026-10-17/a.crop.jpg", "2026-10-17/a.frame.jpg", "squirrel", 0),
            ("b", None, None, "squirrel", 20),
            ("evil", "../../../etc/passwd", None, "rodent", 40),
        ):
            cur = conn.execute(
                "INSERT INTO wildlife_boxes (camera, segment_ts, frame_ts, x1, y1, x2, y2, "
                "det_score, verdict, reject_score, suggestion, suggestion_score, top_class, "
                "top_score, visit_id, crop_path, frame_path, created_at) "
                "VALUES ('gate-walkway', ?, ?, .1, .1, .2, .2, .6, 'animal', .05, ?, .9, "
                "'eastern gray squirrel', .7, ?, ?, ?, 'now')",
                (now - 600 + dt, now - 600 + dt, sug, ids["visit"], crop, frame),
            )
            ids[tag] = int(cur.lastrowid or 0)
        conn.commit()
    finally:
        conn.close()
    return ids


@pytest.fixture
def env(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> tuple[TestClient, dict[str, int], Settings]:
    settings = Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000", db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        wildlife=WildlifeSection(
            enabled=False,  # startup check_inputs would flag the missing models
            timezone="America/Los_Angeles",
            cameras={"gate-walkway": WildlifeCamera()},
            output_dir=tmp_path / "wildlife",
        ),
    )
    ids = _seed(settings)
    return TestClient(create_app(settings)), ids, settings


def test_page_renders_visit_and_scoreboard(
    env: tuple[TestClient, dict[str, int], Settings]
) -> None:
    client, ids, _ = env
    r = client.get("/wildlife")
    assert r.status_code == 200
    assert "gate-walkway" in r.text
    assert "probably a <b>squirrel</b>" in r.text
    assert f"/wildlife/boxes/{ids['a']}/crop.jpg" in r.text
    assert "missed share" in r.text


def test_json_lists_hits_and_aggregates_suggestion(
    env: tuple[TestClient, dict[str, int], Settings]
) -> None:
    client, _, _ = env
    data = client.get("/wildlife.json").json()
    assert len(data["visits"]) == 1
    v = data["visits"][0]
    assert v["suggestion"] == "squirrel"
    assert [h["offset_s"] for h in v["hits"]] == [0.0, 20.0, 40.0]
    assert data["scoreboard"][0]["camera"] == "gate-walkway"


def test_images_are_served_by_row_id_and_contained(
    env: tuple[TestClient, dict[str, int], Settings]
) -> None:
    client, ids, _ = env
    assert client.get(f"/wildlife/boxes/{ids['a']}/crop.jpg").status_code == 200
    assert client.get(f"/wildlife/boxes/{ids['a']}/frame.jpg").status_code == 200
    assert client.get(f"/wildlife/boxes/{ids['b']}/crop.jpg").status_code == 404
    assert client.get(f"/wildlife/boxes/{ids['evil']}/crop.jpg").status_code == 404


def test_review_hides_visit_from_pending(
    env: tuple[TestClient, dict[str, int], Settings]
) -> None:
    client, ids, _ = env
    r = client.post(f"/wildlife/visits/{ids['visit']}/review", json={"review": "discarded"})
    assert r.status_code == 200
    assert client.get("/wildlife.json").json()["visits"] == []
    assert len(client.get("/wildlife.json?review=discarded").json()["visits"]) == 1
    assert client.post("/wildlife/visits/999/review", json={"review": "kept"}).status_code == 404
    bad = client.post(f"/wildlife/visits/{ids['visit']}/review", json={"review": "maybe"})
    assert bad.status_code == 400


def test_send_to_plus_marks_hit_and_keeps_visit(
    env: tuple[TestClient, dict[str, int], Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ids, settings = env
    calls: list[tuple[str, float]] = []

    def fake(self: FrigateClient, camera: str, frame_ts: float, *, timeout: float) -> str:
        calls.append((camera, frame_ts))
        return "Successfully submitted image."

    monkeypatch.setattr(FrigateClient, "submit_plus_frame", fake)
    r = client.post(f"/wildlife/boxes/{ids['a']}/plus")
    assert r.status_code == 200
    assert calls and calls[0][0] == "gate-walkway"
    again = client.post(f"/wildlife/boxes/{ids['a']}/plus")
    assert again.status_code == 409

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        hit = conn.execute(
            "SELECT plus_status FROM wildlife_boxes WHERE id = ?", (ids["a"],)
        ).fetchone()
        visit = conn.execute(
            "SELECT review FROM wildlife_visits WHERE id = ?", (ids["visit"],)
        ).fetchone()
    finally:
        conn.close()
    assert hit["plus_status"] == "sent"
    assert visit["review"] == "kept"


def test_send_to_plus_failure_is_recorded_and_retryable(
    env: tuple[TestClient, dict[str, int], Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, ids, settings = env

    def fail(self: FrigateClient, camera: str, frame_ts: float, *, timeout: float) -> str:
        raise FrigatePlusError("submit_plus_frame: Recording not found at 1", 404)

    monkeypatch.setattr(FrigateClient, "submit_plus_frame", fail)
    r = client.post(f"/wildlife/boxes/{ids['b']}/plus")
    assert r.status_code == 502
    assert "Recording not found" in r.json()["detail"]["message"]
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT plus_status FROM wildlife_boxes WHERE id = ?", (ids["b"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["plus_status"] == "error"
    assert client.post("/wildlife/boxes/424242/plus").status_code == 404
