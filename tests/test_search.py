"""`GET /v1/events/search` -- structured event search over frigate.db."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.server import create_app


def _add_event(
    db_path: Path,
    *,
    eid: str,
    camera: str,
    label: str,
    sub_label: str | None,
    dt: float,
    score: float,
    zones_list: list[str],
    has_clip: int = 1,
    has_snap: int = 1,
) -> None:
    conn = sqlite3.connect(db_path)
    now = time.time()
    data = json.dumps({"score": score, "top_score": score, "box": [0.1, 0.2, 0.3, 0.4]})
    conn.execute(
        "INSERT INTO event (id, camera, label, sub_label, start_time, end_time, score, "
        "top_score, area, ratio, zones, data, has_clip, has_snapshot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid, camera, label, sub_label, now + dt, now + dt + 30, score, score,
            5000.0, 1.5, json.dumps(zones_list), data, has_clip, has_snap,
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    # Extra seed events beyond conftest's five, for camera/zone/sub_label/score variety.
    _add_event(
        frigate_db_path, eid="s1", camera="gate-walkway", label="person",
        sub_label="known_person", dt=-100, score=0.95, zones_list=["driveway"],
    )
    _add_event(
        frigate_db_path, eid="s2", camera="gate-walkway", label="person",
        sub_label=None, dt=-200, score=0.4, zones_list=["driveway", "yard"],
    )
    _add_event(
        frigate_db_path, eid="s3", camera="gate-walkway", label="car",
        sub_label=None, dt=-300, score=0.6, zones_list=["yard"],
    )
    _add_event(
        frigate_db_path, eid="s4", camera="street-overview", label="person",
        sub_label=None, dt=-400, score=0.8, zones_list=[],
        has_snap=0,
    )

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_no_filters_returns_events(client: TestClient) -> None:
    r = client.get("/v1/events/search")
    assert r.status_code == 200
    body = r.json()
    assert len(body) > 0
    assert len(body) <= 50


def test_filter_by_camera(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"cameras": "gate-walkway"})
    assert r.status_code == 200
    body = r.json()
    assert body
    assert all(e["camera"] == "gate-walkway" for e in body)


def test_filter_by_label(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"labels": "person"})
    body = r.json()
    assert body
    assert all(e["label"] == "person" for e in body)


def test_filter_by_zone(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"zones": "driveway"})
    body = r.json()
    assert body
    for e in body:
        assert "driveway" in e["zones"]


def test_camera_and_label_combination(client: TestClient) -> None:
    r = client.get(
        "/v1/events/search", params={"cameras": "gate-walkway", "labels": "person"}
    )
    body = r.json()
    assert body
    assert all(e["camera"] == "gate-walkway" and e["label"] == "person" for e in body)
    ids = {e["id"] for e in body}
    assert "s3" not in ids  # car, not person


def test_time_range_filter(client: TestClient) -> None:
    now = time.time()
    r = client.get(
        "/v1/events/search",
        params={"after": now - 250, "before": now - 50},
    )
    body = r.json()
    assert body
    for e in body:
        assert now - 250 <= e["start_time"] <= now - 50


def test_limit_respected(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"limit": 2})
    body = r.json()
    assert len(body) == 2


def test_q_substring_match(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"q": "person"})
    body = r.json()
    assert body
    assert all(e["label"] == "person" for e in body)


def test_has_snapshot_filter(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"has_snapshot": "true"})
    body = r.json()
    assert body
    assert all(e["has_snapshot"] is True for e in body)
    ids = {e["id"] for e in body}
    assert "s4" not in ids


def test_min_score_filter(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"min_score": 0.9})
    body = r.json()
    assert body
    ids = {e["id"] for e in body}
    assert "s1" in ids
    assert "s2" not in ids
    assert "s3" not in ids


def test_response_shape(client: TestClient) -> None:
    r = client.get("/v1/events/search", params={"cameras": "gate-walkway", "limit": 1})
    body = r.json()
    assert len(body) == 1
    ev = body[0]
    for key in (
        "id", "camera", "label", "sub_label", "zones", "start_time", "end_time",
        "has_clip", "has_snapshot", "data", "search_distance", "search_source",
    ):
        assert key in ev
    assert ev["search_source"] == "structured"
    assert ev["search_distance"] is None
    assert isinstance(ev["zones"], list)
