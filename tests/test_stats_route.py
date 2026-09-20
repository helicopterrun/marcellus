"""`GET /v1/stats` and `status_json`'s `push_stats` key (wave 2A)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push.stats import STATS
from marcellus.server import create_app


@pytest.fixture(autouse=True)
def _reset_stats():
    STATS.reset()
    yield
    STATS.reset()


@pytest.fixture
def app(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> FastAPI:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_v1_stats_returns_snapshot_and_derived_fields(client: TestClient) -> None:
    resp = client.get("/v1/stats")
    assert resp.status_code == 200
    body = resp.json()
    # `STATS.snapshot()`'s own keys.
    assert "counters" in body
    assert "gauges" in body
    assert "uptime_s" in body
    # Derived conveniences (spec §5).
    assert body["relay"]["breaker_open"] is False
    assert "depth" in body["queue"]
    assert body["queue"]["max"] == Settings().push.mqtt_queue_max


def test_v1_stats_reflects_a_counter_bump(client: TestClient) -> None:
    STATS.incr("relay.send.ok")
    resp = client.get("/v1/stats")
    assert resp.json()["counters"]["relay.send.ok"] == 1


def test_v1_stats_breaker_open_reflects_gauge(client: TestClient) -> None:
    STATS.gauge("relay.breaker.state", 1)
    resp = client.get("/v1/stats")
    assert resp.json()["relay"]["breaker_open"] is True


def test_status_json_carries_push_stats(client: TestClient) -> None:
    resp = client.get("/status.json")
    assert resp.status_code == 200
    body = resp.json()
    assert "push_stats" in body
    assert "counters" in body["push_stats"]
    assert "gauges" in body["push_stats"]


def test_status_page_renders_with_push_disabled(client: TestClient) -> None:
    # push.enabled defaults to False; the status page must not 500 or 404
    # when the "Push pipeline" block's guard condition is false.
    resp = client.get("/")
    assert resp.status_code == 200


def test_status_page_renders_push_pipeline_block_when_enabled(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    from marcellus.config import PushSection

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        push=PushSection(enabled=True),
    )
    resp = TestClient(create_app(settings)).get("/")
    assert resp.status_code == 200
    assert "Push pipeline" in resp.text
