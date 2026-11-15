"""Zones with `enabled: false` (Frigate 0.18+) drop out of every surface."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.encounters.adjacency import build_adjacency
from marcellus.push import policy_settings
from marcellus.push.decision import parse_review_message
from marcellus.push.ladder import Snapshot, base_level, evaluate_ladder
from marcellus.routes import scrub as scrub_routes
from marcellus.server import create_app
from marcellus.zones import is_disabled, load_camera_zones

SQUARE = "0.3,0.6,0.7,0.6,0.7,0.9,0.3,0.9"
OPTICS = {"hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0}
LAYOUT = {"x": 0.5, "y": 0.5, "azimuth": 0.0, "fov": 90.0}


def _write_config(path: Path, *, disabled: bool = True) -> Path:
    path.write_text(yaml.safe_dump({
        "cameras": {
            "cam": {"zones": {
                "walkway": {"coordinates": SQUARE},
                "front_door": {"coordinates": SQUARE, "enabled": not disabled},
            }},
            "other": {"zones": {
                "front_door": {"coordinates": SQUARE, "enabled": not disabled},
                "walkway": {"coordinates": SQUARE},
            }},
        }
    }))
    return path


@pytest.fixture(autouse=True)
def _isolated_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


@pytest.fixture
def client(tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    config = _write_config(tmp_path / "frigate.yml")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=config, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        push=PushSection(enabled=False, push_settings_path=str(tmp_path / "push_settings.json")),
    )
    return TestClient(create_app(settings))


def test_loader_drops_disabled_and_include_disabled_keeps_them(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "c.yml")
    assert [z["name"] for z in load_camera_zones(cfg)["cam"]] == ["walkway"]
    assert is_disabled("front_door") and not is_disabled("walkway")
    kept = load_camera_zones(cfg, include_disabled=True)["cam"]
    assert {z["name"]: z["enabled"] for z in kept} == {"walkway": True, "front_door": False}


def test_zone_enabled_on_another_camera_is_not_disabled(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump({"cameras": {
        "a": {"zones": {"z": {"coordinates": SQUARE, "enabled": False}}},
        "b": {"zones": {"z": {"coordinates": SQUARE}}},
    }}))
    load_camera_zones(cfg)
    assert not is_disabled("z")


def test_017_config_without_flag_has_every_zone(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump({"cameras": {"a": {"zones": {"z": {"coordinates": SQUARE}}}}}))
    assert [z["name"] for z in load_camera_zones(cfg)["a"]] == ["z"]


def test_reenabling_restores_the_zone(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yml"
    _write_config(cfg)
    load_camera_zones(cfg)
    _write_config(cfg, disabled=False)
    assert {z["name"] for z in load_camera_zones(cfg)["cam"]} == {"walkway", "front_door"}
    assert not is_disabled("front_door")


def test_push_settings_available_zones_and_openings(client: TestClient) -> None:
    body = client.get("/v1/push/settings").json()
    assert {z["zone"] for z in body["available_zones"]} == {"walkway"}
    assert "front_door" not in body["available_openings"]


def test_stored_zone_settings_survive_but_are_hidden(client: TestClient) -> None:
    active = dict(policy_settings.get_active())
    active["zone_overrides"] = {"front_door": {"person": "urgent"}}
    active["zone_classes"] = {"front_door": "doors"}
    policy_settings.apply_settings(active)
    body = client.get("/v1/push/settings").json()
    # Kept in the stored document (re-enable restores it) ...
    assert body["settings"]["zone_overrides"]["front_door"] == {"person": "urgent"}
    # ... but not offered as a zone, and not shown in the settings page.
    assert "front_door" not in {z["zone"] for z in body["available_zones"]}
    page = client.get("/settings")
    assert page.status_code == 200
    assert "front_door" not in page.text


def test_ladder_ignores_override_of_disabled_zone(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "c.yml")
    active = dict(policy_settings.get_active())
    active["zone_overrides"] = {
        "front_door": {"stranger": "urgent"},
        "walkway": {"stranger": "urgent"},
    }
    policy_settings.apply_settings(active)
    snap = {"subject": "stranger", "place": "street"}
    assert evaluate_ladder(Snapshot(zone="front_door", **snap)) == "urgent"  # enabled for now
    load_camera_zones(cfg)
    assert evaluate_ladder(Snapshot(zone="front_door", **snap)) != "urgent"
    assert base_level("stranger", "street", "front_door")[1] is False
    assert evaluate_ladder(Snapshot(zone="walkway", **snap)) == "urgent"


def test_review_events_drop_disabled_zone_names(tmp_path: Path) -> None:
    load_camera_zones(_write_config(tmp_path / "c.yml"))
    payload: dict[str, Any] = {
        "type": "new",
        "after": {
            "id": "r1", "camera": "cam", "severity": "alert",
            "start_time": 1.0, "end_time": None,
            "data": {"objects": ["person"], "zones": ["front_door", "walkway"],
                     "detections": ["e1"], "sub_labels": [], "audio": []},
        },
    }
    event = parse_review_message(payload)
    assert event is not None
    assert event.zones == ("walkway",)


def test_encounter_adjacency_ignores_disabled_zones(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path / "c.yml")
    adj = build_adjacency(load_camera_zones(cfg), extra=[], removed=[])
    assert adj.shared_zones[frozenset(("cam", "other"))] == ("walkway",)
    adj_all = build_adjacency(load_camera_zones(cfg, include_disabled=True), extra=[], removed=[])
    assert adj_all.shared_zones[frozenset(("cam", "other"))] == ("front_door", "walkway")


def test_zone_map_excludes_disabled(client: TestClient) -> None:
    active = dict(policy_settings.get_active())
    active.update({
        "camera_optics": {"cam": dict(OPTICS)},
        "camera_layout": {"cam": dict(LAYOUT)},
        "map_scale_ft": 200.0,
    })
    policy_settings.apply_settings(active)
    names = [(z["camera"], z["name"]) for z in client.get("/v1/push/map/zones").json()["zones"]]
    assert names == [("cam", "walkway")]


async def test_motion_fetch_scale_is_rounded_to_whole_seconds() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["scale"])
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            http_client=http,
            settings=SimpleNamespace(frigate=SimpleNamespace(base_url="http://frigate.test:5000")),
        )))
        values, unavailable = await scrub_routes._fetch_and_aggregate_motion(
            request, "cam", 1000.0, 1010.0, 2.6  # type: ignore[arg-type]
        )
        await scrub_routes._fetch_and_aggregate_motion(
            request, "cam", 1000.0, 1010.0, 0.3  # type: ignore[arg-type]
        )
    assert seen == ["3", "1"]
    assert unavailable is False
    assert len(values) == 4
