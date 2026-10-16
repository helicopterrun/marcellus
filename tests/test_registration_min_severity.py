"""Contract: registration `min_severity` gates which ladder levels reach a device.

`_device_eligible` maps card level -> severity (quiet/Glance -> "detection")
and requires it to be >= the device's stored `min_severity`. The registration
model defaults `min_severity` to "alert", and every PUT overwrites the stored
value, so a client that OMITS the field gets Glance (quiet) cards dropped
(`la_reason=device_not_la_capable`). Clients that want Glance must send
`min_severity="detection"` explicitly; the ladder is the real routing
authority downstream.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.push.delivery import _device_eligible
from marcellus.server import create_app


@pytest.fixture
def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
    )


def _register(settings: Settings, token: str, extra: dict):
    client = TestClient(create_app(settings))
    body = {"bundle_id": "com.x", "environment": "sandbox", "cameras": [], **extra}
    assert client.put(f"/v1/push/devices/{token}", json=body).status_code == 200
    conn = db.open_sidecar(str(settings.sidecar.db_path))
    try:
        return store.get_device(conn, token)
    finally:
        conn.close()


def _eligible(device, level: str) -> bool:
    return _device_eligible(device, camera="gate-face", labels=("person",), card_level=level)


def test_omitting_min_severity_drops_glance(_settings: Settings) -> None:
    """Documents the trap: no min_severity => Glance ("quiet") never delivers."""
    device = _register(_settings, "tok-omit", {})
    assert device.min_severity == "alert"
    assert _eligible(device, "quiet") is False
    assert _eligible(device, "notify") is True


def test_detection_min_severity_admits_glance(_settings: Settings) -> None:
    device = _register(_settings, "tok-det", {"min_severity": "detection"})
    assert device.min_severity == "detection"
    assert _eligible(device, "quiet") is True
    assert _eligible(device, "notify") is True
    assert _eligible(device, "log") is False
