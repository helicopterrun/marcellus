"""M-1: `/v1/capabilities`'s `unifi_protect` block and `GET
/v1/protect/status`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection, UnifiProtectSection
from marcellus.push import store
from marcellus.server import create_app


@pytest.fixture
def _base_settings_kwargs(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> dict:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return {
        "frigate": FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        "sidecar": SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    }


def test_capabilities_protect_disabled_by_default(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    body = client.get("/v1/capabilities").json()["unifi_protect"]
    assert body == {
        "enabled": False,
        "cameras": [],
        "lcd_message": False,
        "ring_snapshot": False,
    }


def test_capabilities_protect_enabled_no_poll_yet(_base_settings_kwargs: dict) -> None:
    settings = Settings(
        **_base_settings_kwargs,
        unifi_protect=UnifiProtectSection(
            enabled=True,
            console_url="https://console.test",
            api_key="k",
            cameras={"cam-1": "front_door"},
        ),
    )
    client = TestClient(create_app(settings))
    body = client.get("/v1/capabilities").json()["unifi_protect"]
    assert body["enabled"] is True
    assert body["cameras"] == ["front_door"]
    # No lifespan running under TestClient's default (context-managed only
    # on `with`), so no protect_subscriber is attached -- lcd_message stays
    # False, same as "polled yet" being False.
    assert body["lcd_message"] is False
    assert body["ring_snapshot"] is False


def test_protect_status_disabled_feature(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    r = client.get("/v1/protect/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_protect_status_enabled_but_not_started(_base_settings_kwargs: dict) -> None:
    settings = Settings(
        **_base_settings_kwargs,
        unifi_protect=UnifiProtectSection(
            enabled=True,
            console_url="https://console.test",
            api_key="k",
            cameras={"cam-1": "front_door"},
        ),
    )
    client = TestClient(create_app(settings))
    r = client.get("/v1/protect/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "starting": True}


def test_protect_status_reports_last_ring_from_push_card_sends(
    _base_settings_kwargs: dict,
) -> None:
    settings = Settings(
        **_base_settings_kwargs,
        unifi_protect=UnifiProtectSection(
            enabled=True,
            console_url="https://console.test",
            api_key="k",
            cameras={"cam-1": "front_door"},
        ),
    )
    app = create_app(settings)

    class _FakeSubscriber:
        def status(self) -> dict:
            return {
                "enabled": True,
                "connected": True,
                "console_version": "7.2.105",
                "last_poll_at": 100.0,
                "last_poll_error": None,
                "cameras": [
                    {
                        "protect_id": "cam-1",
                        "frigate_camera": "front_door",
                        "name": "Front Door",
                        "model": "camera",
                        "state": "CONNECTED",
                        "has_lcd": True,
                        "checked_at": 100.0,
                    }
                ],
                "mapped_cameras_connected": True,
            }

        async def aclose(self) -> None:
            return None

    conn = store  # module import already; use db.open_sidecar directly
    from marcellus import db as _db

    sidecar_conn = _db.open_sidecar(str(settings.sidecar.db_path))
    conn.record_card_send(
        sidecar_conn,
        apns_token="tok-1",
        card_key="doorbell:front_door",
        mutation="ring",
        sent_at=12345.0,
    )
    sidecar_conn.close()

    with TestClient(app) as client:
        client.app.state.protect_subscriber = _FakeSubscriber()  # type: ignore[attr-defined]
        r = client.get("/v1/protect/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["console_version"] == "7.2.105"
    assert body["cameras"][0]["last_ring_at"] == 12345.0
