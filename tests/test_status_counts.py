"""`/status.json`'s additive `counts` block (Wave 6B-2): one query/attribute
per key, degrading to zeros on an empty DB rather than a 500."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.faces import enrich
from marcellus.push.stats import STATS
from marcellus.server import create_app


@pytest.fixture(autouse=True)
def _reset_stats():
    STATS.reset()
    yield
    STATS.reset()


@pytest.fixture
def settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


_COUNTS_KEYS = {
    "uptime_s",
    "face_enrich",
    "face_clusters",
    "face_captures_pending",
    "push_24h",
    "live_activities_open",
    "auth",
    "triage_labels",
}


def test_status_json_carries_every_counts_key_with_right_types_on_empty_db(
    client: TestClient,
) -> None:
    resp = client.get("/status.json")
    assert resp.status_code == 200
    counts = resp.json()["counts"]
    assert counts.keys() >= _COUNTS_KEYS
    assert isinstance(counts["uptime_s"], (int, float))
    assert counts["face_clusters"] == {"named": 0, "unnamed": 0}
    assert counts["face_captures_pending"] == 0
    assert counts["live_activities_open"] == 0
    assert counts["triage_labels"] == {"count": 0, "last_labeled_at": None}
    assert counts["auth"]["cache"] == 0
    assert counts["auth"]["login_rate_limit_rejections"] == 0


def test_status_page_renders_counters_card(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Counters" in resp.text


def test_counts_reflect_seeded_face_and_triage_rows(
    settings: Settings, client: TestClient
) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES ('alice', ?, 1, '', 0)",
            (enrich.pack_embedding([1.0, 0.0]),),
        )
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES (NULL, ?, 1, '', 0)",
            (enrich.pack_embedding([0.0, 1.0]),),
        )
        conn.execute(
            "INSERT INTO face_enrichments (event_id, camera, event_start_ts, status, "
            "processed_at) VALUES ('e1', 'cam', 0, 'enriched', '')"
        )
        conn.execute(
            "INSERT INTO face_enrichments (event_id, camera, event_start_ts, status, "
            "processed_at, excluded_at) VALUES ('e2', 'cam', 0, 'enriched', '', ?)",
            ("2026-01-01T00:00:00+00:00",),
        )
        conn.execute(
            "INSERT INTO triage_labels (event_id, label, labeled_at) "
            "VALUES ('e1', 'tp', '2026-08-31T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()

    counts = client.get("/status.json").json()["counts"]
    assert counts["face_clusters"] == {"named": 1, "unnamed": 1}
    assert counts["face_enrich"]["enriched"] == 1  # excluded row not counted
    assert counts["face_enrich"]["excluded"] == 1
    assert counts["triage_labels"]["count"] == 1
    assert counts["triage_labels"]["last_labeled_at"] == "2026-08-31T00:00:00Z"


def test_counts_reflect_open_live_activities(settings: Settings, client: TestClient) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO push_activities (activity_id, apns_token, situation_id, "
            "track_id, created_at, ended_at) VALUES ('a1', 't', 's', 'trk', ?, NULL)",
            (time.time(),),
        )
        conn.execute(
            "INSERT INTO push_activities (activity_id, apns_token, situation_id, "
            "track_id, created_at, ended_at) VALUES ('a2', 't', 's', 'trk2', ?, ?)",
            (time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    counts = client.get("/status.json").json()["counts"]
    assert counts["live_activities_open"] == 1
