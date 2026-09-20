"""`GET`/`PUT /v1/push/settings` (Elsinore Phase 4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import policy_settings
from marcellus.server import create_app


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _settings(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, **push_kwargs: Any,
) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(
        yaml.safe_dump(
            {
                "cameras": {
                    "doorbell": {
                        "zones": {
                            "front_door": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                    "street": {
                        "zones": {
                            "nw_49th_st": {"coordinates": "0,0,1,0,1,1,0,1"},
                            "sidewalk": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                    "backyard": {
                        "zones": {
                            "garage": {"coordinates": "0,0,1,0,1,1,0,1"},
                        }
                    },
                }
            }
        )
    )
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp_path / "push_settings.json"), **push_kwargs,
        ),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    app = create_app(settings)
    return TestClient(app)


def test_get_returns_defaults_when_no_settings_file_exists(client: TestClient, tmp_path: Path):
    resp = client.get("/v1/push/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"] == policy_settings.default_settings()
    # And creates the file so GET and the routing engine never disagree.
    assert (tmp_path / "push_settings.json").exists()


def test_get_available_zones_from_frigate_config(client: TestClient):
    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["cameras"] == ["doorbell"]
    assert zones["front_door"]["guessed_class"] == "doors"
    assert zones["sidewalk"]["cameras"] == ["street"]
    assert zones["sidewalk"]["guessed_class"] == "street"
    assert zones["nw_49th_st"]["cameras"] == ["street"]
    assert zones["nw_49th_st"]["guessed_class"] == "street"  # via its camera, not its own name


def test_get_available_openings(client: TestClient):
    resp = client.get("/v1/push/settings")
    openings = set(resp.json()["available_openings"])
    assert "front_door" in openings
    assert "garage" in openings
    assert "sidewalk" not in openings


def test_put_persists_and_is_reflected_in_subsequent_get(client: TestClient, tmp_path: Path):
    new_settings = policy_settings.default_settings()
    new_settings["zone_classes"]["front_door"] = "doors"
    new_settings["routing_table"]["thing"]["yard"] = "urgent"
    # Retired boolean: accepted on the wire, ignored, derived on read.
    new_settings["live_activities"]["package"] = False

    put_resp = client.put("/v1/push/settings", json=new_settings)
    assert put_resp.status_code == 200
    body = put_resp.json()
    assert body["ok"] is True
    assert isinstance(body["rev"], int)

    get_resp = client.get("/v1/push/settings")
    stored = get_resp.json()["settings"]
    assert stored["zone_classes"] == {"front_door": "doors"}
    assert stored["routing_table"]["thing"]["yard"] == "urgent"
    assert stored["live_activities"]["package"] is True

    on_disk = json.loads((tmp_path / "push_settings.json").read_text())
    assert on_disk["routing_table"]["thing"]["yard"] == "urgent"


def test_put_round_trips_all_seven_subjects(client: TestClient):
    new_settings = policy_settings.default_settings()
    new_settings["outcomes"]["package"]["doors"] = "notify"
    new_settings["outcomes"]["opening"]["yard"] = "off"

    assert client.put("/v1/push/settings", json=new_settings).status_code == 200
    stored = client.get("/v1/push/settings").json()["settings"]
    assert set(stored["outcomes"]) == set(policy_settings.SUBJECTS_V3)
    assert stored["outcomes"]["package"]["doors"] == "notify"
    assert stored["outcomes"]["opening"]["yard"] == "off"
    # v2 stays in lockstep, off spelling as log.
    assert stored["routing_table_v2"]["package"]["doors"] == "notify"
    assert stored["routing_table_v2"]["opening"]["yard"] == "log"


def test_four_subject_put_preserves_stored_v3_rows(client: TestClient):
    """An older app's PUT (its Codable only knows the classic four subjects)
    must not reset the V3 rows the user tuned from a newer build."""
    tuned = policy_settings.default_settings()
    tuned["outcomes"]["package"]["yard"] = "alarm"
    assert client.put("/v1/push/settings", json=tuned).status_code == 200

    old_body = policy_settings.default_settings()
    for subject in policy_settings.SUBJECTS_V3_EXTRA:
        del old_body["outcomes"][subject]
        del old_body["routing_table_v2"][subject]
    assert client.put("/v1/push/settings", json=old_body).status_code == 200

    stored = client.get("/v1/push/settings").json()["settings"]
    assert stored["outcomes"]["package"]["yard"] == "alarm"
    assert stored["routing_table_v2"]["package"]["yard"] == "urgent"


def test_outcomes_unknown_subject_still_400s(client: TestClient):
    bad = policy_settings.default_settings()
    bad["outcomes"]["drone"] = {"yard": "notify"}
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400
    assert "unknown subject" in json.dumps(resp.json())


def test_put_persists_zone_overrides_and_get_returns_them(client: TestClient):
    new_settings = policy_settings.default_settings()
    new_settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}

    put_resp = client.put("/v1/push/settings", json=new_settings)
    assert put_resp.status_code == 200

    stored = client.get("/v1/push/settings").json()["settings"]
    assert stored["zone_overrides"] == {"front_entry_person": {"thing": "notify"}}


def test_put_rejects_invalid_zone_override(client: TestClient):
    bad = policy_settings.default_settings()
    bad["zone_overrides"] = {"driveway": {"thing": "screaming"}}
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400
    assert any("zone_overrides.driveway.thing" in d for d in resp.json()["detail"]["detail"])


def test_put_cleans_up_empty_zone_override_entries(client: TestClient):
    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"driveway": {}}
    resp = client.put("/v1/push/settings", json=settings)
    assert resp.status_code == 200

    stored = client.get("/v1/push/settings").json()["settings"]
    assert stored["zone_overrides"] == {}


def test_put_applies_immediately_to_the_routing_engine(client: TestClient):
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    # The outcomes table is the authority when present (merged ladder,
    # 2026-08-16) -- a new client edits it; the legacy levels are derived.
    new_settings = policy_settings.default_settings()
    new_settings["outcomes"]["thing"]["yard"] = "alarm"
    client.put("/v1/push/settings", json=new_settings)

    assert evaluate_ladder(Snapshot(subject="thing", place="yard")) == "urgent"

    # An old client body (no outcomes key) still applies via derivation.
    legacy = policy_settings.default_settings()
    del legacy["outcomes"]
    legacy["routing_table_v2"]["thing"]["yard"] = "urgent"
    client.put("/v1/push/settings", json=legacy)
    assert evaluate_ladder(Snapshot(subject="thing", place="yard")) == "urgent"


def test_put_rejects_invalid_level(client: TestClient):
    bad = policy_settings.default_settings()
    bad["routing_table"]["stranger"]["doors"] = "screaming"
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400
    body = resp.json()["detail"]
    assert body["error"] == "invalid_settings"
    assert any("routing_table.stranger.doors" in d for d in body["detail"])


def test_put_rejects_unknown_subject(client: TestClient):
    bad = policy_settings.default_settings()
    bad["routing_table"]["ghost"] = {p: "log" for p in policy_settings.PLACES}
    resp = client.put("/v1/push/settings", json=bad)
    assert resp.status_code == 400


def test_put_unknown_top_level_field_does_not_fail(client: TestClient):
    ok = policy_settings.default_settings()
    ok["a_future_field"] = {"whatever": True}
    resp = client.put("/v1/push/settings", json=ok)
    assert resp.status_code == 200


def test_get_includes_friendly_names_and_reloads_them(
    client: TestClient, tmp_path: Path,
):
    """`friendly_name` rides along in available_zones, and editing Frigate's
    config shows up on the next GET without a sidecar restart."""
    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["friendly_name"] is None

    config = tmp_path / "frigate-config.yml"
    doc = yaml.safe_load(config.read_text())
    doc["cameras"]["doorbell"]["zones"]["front_door"]["friendly_name"] = "Front Door"
    config.write_text(yaml.safe_dump(doc))

    resp = client.get("/v1/push/settings")
    zones = {z["zone"]: z for z in resp.json()["available_zones"]}
    assert zones["front_door"]["friendly_name"] == "Front Door"


def test_get_includes_available_cameras(client: TestClient):
    resp = client.get("/v1/push/settings")
    assert resp.json()["available_cameras"] == ["backyard", "doorbell", "street"]


def test_put_round_trips_zone_page_fields(client: TestClient):
    """The /zones page PUTs the whole doc with classes, overrides, and an
    explicit camera_neighbors map."""
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["zone_classes"] = {"front_door": "doors", "garage": "off_limits"}
    doc["zone_overrides"] = {"sidewalk": {"person": "notify"}}
    doc["camera_neighbors"] = {"doorbell": ["street"]}

    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["zone_classes"] == {"front_door": "doors", "garage": "off_limits"}
    assert saved["zone_overrides"] == {"sidewalk": {"person": "notify"}}
    assert saved["camera_neighbors"] == {"doorbell": ["street"]}

    # An app-style PUT that omits camera_neighbors must not wipe it...
    app_doc = {k: v for k, v in doc.items() if k != "camera_neighbors"}
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_neighbors"] == {"doorbell": ["street"]}

    # ...but the page sending an explicit empty map clears it.
    doc["camera_neighbors"] = {}
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_neighbors"] == {}


def test_zones_page_renders(client: TestClient):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Camera neighbors" in resp.text
    assert "/static/js/zones.js" in resp.text


def test_put_round_trips_camera_calibration_fields(client: TestClient):
    """The /cameras page PUTs camera_headings (unit vectors, renormalized)
    and camera_layout (0..1 positions); app-style PUTs omitting them stay
    sticky."""
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_headings"] = {"doorbell": {"dx": 3.0, "dy": 4.0}}  # not unit length
    doc["camera_layout"] = {"doorbell": {"x": 0.25, "y": 0.75}}

    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_headings"] == {"doorbell": {"dx": 0.6, "dy": 0.8}}
    assert saved["camera_layout"] == {"doorbell": {"x": 0.25, "y": 0.75}}

    app_doc = {
        k: v for k, v in doc.items() if k not in ("camera_headings", "camera_layout")
    }
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]
    assert saved["camera_headings"] == {"doorbell": {"dx": 0.6, "dy": 0.8}}
    assert saved["camera_layout"] == {"doorbell": {"x": 0.25, "y": 0.75}}


def test_put_rejects_malformed_camera_calibration(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_headings"] = {"doorbell": {"dx": 0, "dy": 0}}  # zero vector
    assert client.put("/v1/push/settings", json=doc).status_code == 400
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_layout"] = {"doorbell": {"x": 1.5, "y": 0.5}}  # out of range
    assert client.put("/v1/push/settings", json=doc).status_code == 400


def test_map_page_renders(client: TestClient):
    resp = client.get("/map")
    assert resp.status_code == 200
    assert "/static/js/mapedit/main.js" in resp.text
    assert client.get("/cameras").status_code == 200


def test_camera_layout_accepts_azimuth_and_fov(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["camera_layout"] = {
        "doorbell": {"x": 0.2, "y": 0.3, "azimuth": 365.0, "fov": 90},
        "street": {"x": 0.5, "y": 0.5},  # position-only stays valid
    }
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]["camera_layout"]
    assert saved["doorbell"] == {"x": 0.2, "y": 0.3, "azimuth": 5.0, "fov": 90.0}
    assert saved["street"] == {"x": 0.5, "y": 0.5}

    doc["camera_layout"] = {"doorbell": {"x": 0.2, "y": 0.3, "fov": 5}}  # fov too narrow
    assert client.put("/v1/push/settings", json=doc).status_code == 400


def test_secure_area_round_trips_and_clears(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["secure_area"] = {"x0": 0.8, "y0": 0.1, "x1": 0.2, "y1": 0.6}
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    saved = client.get("/v1/push/settings").json()["settings"]["secure_area"]
    # Corners normalize to top-left / bottom-right.
    assert saved == {"x0": 0.2, "y0": 0.1, "x1": 0.8, "y1": 0.6}

    # Omitting the key (app-style PUT) keeps it...
    app_doc = {k: v for k, v in doc.items() if k != "secure_area"}
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["secure_area"] == saved

    # ...explicit null clears it.
    doc["secure_area"] = None
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["secure_area"] is None

    # Malformed rejects.
    doc["secure_area"] = {"x0": 0.2, "y0": 0.1, "x1": 1.4, "y1": 0.6}
    assert client.put("/v1/push/settings", json=doc).status_code == 400


def test_map_scale_ft_round_trips_and_clears(client: TestClient):
    doc = client.get("/v1/push/settings").json()["settings"]
    doc["map_scale_ft"] = 120.0
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["map_scale_ft"] == 120.0

    app_doc = {k: v for k, v in doc.items() if k != "map_scale_ft"}
    assert client.put("/v1/push/settings", json=app_doc).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["map_scale_ft"] == 120.0

    doc["map_scale_ft"] = None
    assert client.put("/v1/push/settings", json=doc).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["map_scale_ft"] is None

    doc["map_scale_ft"] = -5
    assert client.put("/v1/push/settings", json=doc).status_code == 400


def test_camera_optics_sticky_across_app_shaped_put(client: TestClient):
    optics_doc = {"street": {"hfov": 115.0, "mount_ft": 35.0, "tilt_deg": 22.0, "faces": "S"}}
    resp = client.put("/v1/push/settings", json={"camera_optics": optics_doc})
    assert resp.status_code == 200
    # An iOS-shaped PUT (Codable drops keys it doesn't know) must not wipe it.
    resp = client.put("/v1/push/settings", json={"mute_sounds": True})
    assert resp.status_code == 200
    body = client.get("/v1/push/settings").json()
    assert body["settings"]["camera_optics"] == optics_doc
    # And placement_deployments mirrors the settings-backed table.
    assert body["placement_deployments"] == optics_doc
    # An explicit dict replaces (including deleting a camera).
    resp = client.put("/v1/push/settings", json={"camera_optics": {}})
    assert resp.status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["camera_optics"] == {}


def test_floorplan_key_sticky_and_nullable(client: TestClient):
    fp = {"ext": "png", "w": 800, "h": 600,
          "calibration": {"x0": 0.1, "y0": 0.5, "x1": 0.9, "y1": 0.5, "length_ft": 40.0}}
    assert client.put("/v1/push/settings", json={"floorplan": fp}).status_code == 200
    # Absent key: sticky.
    assert client.put("/v1/push/settings", json={"mute_sounds": False}).status_code == 200
    got = client.get("/v1/push/settings").json()["settings"]["floorplan"]
    assert got["ext"] == "png" and got["calibration"]["length_ft"] == 40.0
    # Explicit null: clears.
    assert client.put("/v1/push/settings", json={"floorplan": None}).status_code == 200
    assert client.get("/v1/push/settings").json()["settings"]["floorplan"] is None


def test_put_rejects_malformed_camera_optics(client: TestClient):
    resp = client.put(
        "/v1/push/settings",
        json={"camera_optics": {"street": {"hfov": 400, "mount_ft": 35, "tilt_deg": 22}}},
    )
    assert resp.status_code == 400


def _refresh_client(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, upstream_yaml: Any,
) -> TestClient:
    import httpx

    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    # The tests point config_path at a sidecar-owned snapshot, which is the
    # one deployment shape allowed to opt in to refresh overwrites.
    settings.frigate.config_refresh_enabled = True
    app = create_app(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/config/raw"
        if isinstance(upstream_yaml, int):
            return httpx.Response(upstream_yaml, text="nope")
        return httpx.Response(200, text=upstream_yaml)

    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app)


def test_config_refresh_writes_changed_snapshot(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    new_yaml = (
        "cameras:\n  renamed-cam:\n    zones:\n      new_zone:\n"
        "        coordinates: 0,0,1,0,1,1,0,1\n"
    )
    client = _refresh_client(frigate_db_path, sidecar_db_path, tmp_path, new_yaml)
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"changed": True, "cameras": ["renamed-cam"]}
    assert (tmp_path / "frigate-config.yml").read_text() == new_yaml
    # Second call: identical upstream -> no-op.
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.json()["changed"] is False


def test_config_refresh_rejects_non_config_response(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    client = _refresh_client(frigate_db_path, sidecar_db_path, tmp_path, "<html>login</html>")
    before = (tmp_path / "frigate-config.yml").read_text()
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.status_code == 502
    assert (tmp_path / "frigate-config.yml").read_text() == before  # untouched


def test_config_refresh_propagates_upstream_denial(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    client = _refresh_client(frigate_db_path, sidecar_db_path, tmp_path, 401)
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.status_code == 502


def test_config_refresh_refused_by_default(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    """`config_refresh_enabled` defaults off: on prod, config_path is
    Frigate's live config.yml and this endpoint must never touch it."""
    client = TestClient(create_app(_settings(frigate_db_path, sidecar_db_path, tmp_path)))
    before = (tmp_path / "frigate-config.yml").read_text()
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.status_code == 403
    assert (tmp_path / "frigate-config.yml").read_text() == before


def test_config_refresh_keeps_backup_of_replaced_snapshot(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    new_yaml = "cameras:\n  cam:\n    zones: {}\n"
    client = _refresh_client(frigate_db_path, sidecar_db_path, tmp_path, new_yaml)
    before = (tmp_path / "frigate-config.yml").read_text()
    resp = client.post("/v1/push/frigate-config/refresh")
    assert resp.status_code == 200
    assert (tmp_path / "frigate-config.yml.bak").read_text() == before


def test_stale_rev_conflicts_instead_of_clobbering(client: TestClient):
    """Two tabs edit the same document: the second save with the old rev must
    409, not silently overwrite the first."""
    rev = client.get("/v1/push/settings").json()["rev"]
    doc = policy_settings.default_settings()

    first = client.put("/v1/push/settings", json={**doc, "rev": rev})
    assert first.status_code == 200
    new_rev = first.json()["rev"]
    assert new_rev != rev

    second = client.put("/v1/push/settings", json={**doc, "rev": rev})
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "stale_settings_rev"

    # A client that never sends rev (the iOS app) keeps working.
    legacy = client.put("/v1/push/settings", json=doc)
    assert legacy.status_code == 200


def test_stale_rev_still_conflicts_after_restart(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
):
    """The rev lives in the settings file, not process memory: a tab holding a
    pre-restart rev must still 409 against a sidecar restarted (redeployed)
    in between, instead of the counter resetting and re-admitting it."""
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    doc = policy_settings.default_settings()

    client = TestClient(create_app(settings))
    old_rev = client.get("/v1/push/settings").json()["rev"]
    saved = client.put("/v1/push/settings", json={**doc, "rev": old_rev})
    assert saved.status_code == 200
    current_rev = saved.json()["rev"]

    # "Restart": a fresh app process over the same settings file.
    policy_settings.reset_for_tests()
    restarted = TestClient(create_app(settings))
    assert restarted.get("/v1/push/settings").json()["rev"] == current_rev

    stale = restarted.put("/v1/push/settings", json={**doc, "rev": old_rev})
    assert stale.status_code == 409
    assert stale.json()["detail"]["error"] == "stale_settings_rev"

    fresh = restarted.put("/v1/push/settings", json={**doc, "rev": current_rev})
    assert fresh.status_code == 200
