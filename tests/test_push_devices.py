from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_register_device(client: TestClient) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={
            "bundle_id": "com.pondhouse.Elsinore",
            "environment": "sandbox",
            "app_version": "0.3.0",
            "cameras": ["doorbell", "garden"],
            "labels": ["person", "car"],
            "min_severity": "alert",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["device_id"].startswith("d_")


def test_register_is_idempotent_on_token(client: TestClient) -> None:
    r1 = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["doorbell"]},
    )
    r2 = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["garden"]},
    )
    assert r1.json()["device_id"] == r2.json()["device_id"]


def test_register_overwrites_filters_not_duplicates(
    client: TestClient, sidecar_db_path: Path
) -> None:
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["doorbell"]},
    )
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox", "cameras": ["garden"]},
    )
    from marcellus import db

    conn = db.open_sidecar(sidecar_db_path)
    try:
        rows = conn.execute("SELECT * FROM push_devices").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    device = store.list_devices(db.open_sidecar(sidecar_db_path))[0]
    assert device.cameras == ("garden",)


def test_environment_required_and_validated(client: TestClient) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "production"},
    )
    assert r.status_code == 422


def test_unregister_device(client: TestClient) -> None:
    client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox"},
    )
    r = client.delete("/v1/push/devices/tok-abc123")
    assert r.status_code == 200
    assert r.json() == {"unregistered": True}


def test_unregister_unknown_token_is_still_200(client: TestClient) -> None:
    r = client.delete("/v1/push/devices/never-registered")
    assert r.status_code == 200
    assert r.json() == {"unregistered": True}


def test_register_frequent_pushes_enabled_true_round_trips(
    client: TestClient, sidecar_db_path: Path
) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={
            "bundle_id": "com.x", "environment": "sandbox",
            "frequent_pushes_enabled": True,
        },
    )
    assert r.status_code == 200
    from marcellus import db

    device = store.list_devices(db.open_sidecar(sidecar_db_path))[0]
    assert device.frequent_pushes_enabled is True


def test_register_frequent_pushes_enabled_defaults_false(
    client: TestClient, sidecar_db_path: Path
) -> None:
    r = client.put(
        "/v1/push/devices/tok-abc123",
        json={"bundle_id": "com.x", "environment": "sandbox"},
    )
    assert r.status_code == 200
    from marcellus import db

    device = store.list_devices(db.open_sidecar(sidecar_db_path))[0]
    assert device.frequent_pushes_enabled is False


def test_capabilities_reports_push_disabled_by_default(client: TestClient) -> None:
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()["push"]
    assert body["enabled"] is False
    assert body["transport"] == "mock"
    # attention_subjects rides along regardless of enabled state.
    assert "package" in body["attention_subjects"]
