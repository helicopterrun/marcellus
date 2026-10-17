"""routes/topology.py: /v1/topology and /v1/cameras/{camera}/neighbours,
plus a check that /v1/encounters/adjacency stays byte-identical."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.transitions import TransitionConfig, learn
from marcellus.server import create_app


@pytest.fixture
def settings(tmp_path: Path, frigate_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text(
        "cameras:\n"
        "  alley-wide:\n"
        "    zones:\n"
        "      back_walkway:\n"
        "        coordinates: '0,0,1,0,1,1,0,1'\n"
        "  shed:\n"
        "    zones:\n"
        "      back_walkway:\n"
        "        coordinates: '0,0,1,0,1,1,0,1'\n"
    )
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _learn_some(settings: Settings) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        now = time.time()
        adj = Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})}))
        cfg = TransitionConfig(
            min_samples=1, max_sample_s=180.0, default={"p10": 2.0, "p50": 15.0, "p90": 60.0}
        )
        learn(conn, adjacency=adj, cfg=cfg, window_s=86400.0, now=now)
    finally:
        conn.close()


def test_topology_endpoint(client: TestClient, settings: Settings) -> None:
    _learn_some(settings)
    resp = client.get("/v1/topology")
    assert resp.status_code == 200
    body = resp.json()
    assert "alley-wide" in body["cameras"]
    assert "shed" in body["cameras"]
    edge = next(e for e in body["edges"] if {e["a"], e["b"]} == {"alley-wide", "shed"})
    assert "alley-wide>shed" in edge["transitions"]
    assert "shed>alley-wide" in edge["transitions"]
    fwd = edge["transitions"]["alley-wide>shed"]
    assert "person" in fwd
    assert fwd["person"]["source"] == "default"
    assert edge["overlap"] is False
    assert edge["overlap_source"] == "default"
    assert set(edge["overlap_stats"]) == {"concurrent", "samples"}
    assert set(edge) == {
        "a",
        "b",
        "zones",
        "source",
        "transitions",
        "overlap",
        "overlap_source",
        "overlap_stats",
    }


def test_camera_neighbours_endpoint(client: TestClient, settings: Settings) -> None:
    _learn_some(settings)
    resp = client.get("/v1/cameras/alley-wide/neighbours")
    assert resp.status_code == 200
    body = resp.json()
    assert body["camera"] == "alley-wide"
    assert any(n["camera"] == "shed" for n in body["neighbours"])
    shed = next(n for n in body["neighbours"] if n["camera"] == "shed")
    assert "person" in shed["transitions"]
    assert shed["overlap"] is False
    assert shed["overlap_source"] == "default"


def test_camera_neighbours_404_unknown_camera(client: TestClient) -> None:
    resp = client.get("/v1/cameras/nonexistent/neighbours")
    assert resp.status_code == 404


def test_adjacency_endpoint_unchanged(client: TestClient) -> None:
    resp = client.get("/v1/encounters/adjacency")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"cameras", "edges"}
    for edge in body["edges"]:
        assert set(edge.keys()) == {"a", "b", "zones", "source"}


def _edge(client: TestClient) -> dict:
    body = client.get("/v1/topology").json()
    return next(e for e in body["edges"] if {e["a"], e["b"]} == {"alley-wide", "shed"})


def test_read_time_config_overrides(settings: Settings) -> None:
    _learn_some(settings)
    settings.encounters.overlap = [["shed", "alley-wide"]]
    client = TestClient(create_app(settings))
    edge = _edge(client)
    assert (edge["overlap"], edge["overlap_source"]) == (True, "config")
    nb = client.get("/v1/cameras/alley-wide/neighbours").json()["neighbours"][0]
    assert (nb["overlap"], nb["overlap_source"]) == (True, "config")

    settings.encounters.not_overlap = [["alley-wide", "shed"]]
    edge = _edge(TestClient(create_app(settings)))
    assert (edge["overlap"], edge["overlap_source"]) == (False, "config")


def test_read_time_uses_stored_row(client: TestClient, settings: Settings) -> None:
    _learn_some(settings)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "UPDATE camera_overlap SET overlap=1, source='learned', concurrent=9, samples=10"
        )
        conn.commit()
    finally:
        conn.close()
    edge = _edge(client)
    assert edge["overlap"] is True and edge["overlap_source"] == "learned"
    assert edge["overlap_stats"] == {"concurrent": 9, "samples": 10}
