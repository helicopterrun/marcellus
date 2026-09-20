"""`POST`/`GET`/`DELETE /v1/push/floorplan` — the layout map's background
image (camera onboarding + floorplan feature)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.push import policy_settings
from marcellus.server import create_app

from .test_push_settings_routes import _settings

# A real 1x1 PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(
        frigate_db_path, sidecar_db_path, tmp_path,
        floorplan_path=str(tmp_path / "floorplan"),
    )
    app = create_app(settings)
    return TestClient(app)


def test_upload_get_delete_round_trip(client: TestClient, tmp_path: Path):
    resp = client.post("/v1/push/floorplan", content=PNG_1X1)
    assert resp.status_code == 200
    fp = resp.json()["floorplan"]
    assert (fp["ext"], fp["w"], fp["h"], fp["calibration"]) == ("png", 1, 1, None)
    assert (tmp_path / "floorplan.png").read_bytes() == PNG_1X1

    got = client.get("/v1/push/floorplan")
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == PNG_1X1

    # Persisted into the settings document too.
    on_disk = json.loads((tmp_path / "push_settings.json").read_text())
    assert on_disk["floorplan"]["ext"] == "png"
    assert client.get("/v1/push/settings").json()["settings"]["floorplan"]["w"] == 1

    assert client.delete("/v1/push/floorplan").status_code == 200
    assert not (tmp_path / "floorplan.png").exists()
    assert client.get("/v1/push/floorplan").status_code == 404
    assert client.get("/v1/push/settings").json()["settings"]["floorplan"] is None


def test_upload_rejects_non_image(client: TestClient):
    resp = client.post("/v1/push/floorplan", content=b"<svg>not a raster</svg>")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "bad_floorplan"


def test_upload_rejects_oversize(client: TestClient):
    blob = b"\x89PNG\r\n\x1a\n" + b"0" * (10 * 1024 * 1024)
    resp = client.post("/v1/push/floorplan", content=blob)
    assert resp.status_code == 413


def test_new_upload_resets_calibration(client: TestClient):
    assert client.post("/v1/push/floorplan", content=PNG_1X1).status_code == 200
    cal = {"x0": 0.1, "y0": 0.5, "x1": 0.9, "y1": 0.5, "length_ft": 40.0}
    fp = client.get("/v1/push/settings").json()["settings"]["floorplan"]
    assert client.put(
        "/v1/push/settings", json={"floorplan": {**fp, "calibration": cal}},
    ).status_code == 200
    # Re-upload: the old line's coordinates no longer mean anything.
    assert client.post("/v1/push/floorplan", content=PNG_1X1).status_code == 200
    fp = client.get("/v1/push/settings").json()["settings"]["floorplan"]
    assert fp["calibration"] is None


def test_get_404_when_never_uploaded(client: TestClient):
    assert client.get("/v1/push/floorplan").status_code == 404
