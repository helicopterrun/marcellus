"""`GET`/`PUT /v1/tuning` (settings-dial spec Part A5)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import tuning
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.server import create_app


@pytest.fixture(autouse=True)
def _isolated_snapshot():
    tuning.reset_for_tests()
    yield
    tuning.reset_for_tests()


def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path,
            bind_port=5001,
            require_frigate_auth=False,
            tuning_path=str(tmp_path / "tuning.json"),
        ),
    )


@pytest.fixture
def settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    s = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    tuning.snapshot_startup(s)
    return s


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)


def test_get_shape(client: TestClient):
    resp = client.get("/v1/tuning")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["rev"], int)
    assert isinstance(body["knobs"], list)
    assert isinstance(body["pending_restart"], list)
    assert {s["name"] for s in body["sections"]} == set(tuning._SECTION_MODELS) | {""}
    keys = {k["key"] for k in body["knobs"]}
    assert "scrub.cell_w" in keys
    assert "push.relay_key" in keys


def test_put_happy_path_mutates_settings_in_place(client: TestClient, settings: Settings):
    get_resp = client.get("/v1/tuning")
    rev = get_resp.json()["rev"]

    put_resp = client.put("/v1/tuning", json={"rev": rev, "overrides": {"scrub.cell_w": 400}})
    assert put_resp.status_code == 200
    body = put_resp.json()
    # No override file existed yet, so both GET's implicit rev (1) and the
    # first write's new rev are 1 -- same as push_settings' read_rev/save_settings.
    assert body["rev"] == 1
    assert settings.scrub.cell_w == 400

    row = next(k for k in body["knobs"] if k["key"] == "scrub.cell_w")
    assert row["value"] == 400
    assert row["source"] == "override"


def test_put_rev_conflict(client: TestClient):
    resp = client.put("/v1/tuning", json={"rev": 999, "overrides": {}})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "stale_rev"


def test_put_invalid_returns_400(client: TestClient):
    resp = client.put("/v1/tuning", json={"rev": 1, "overrides": {"scrub.format": "png"}})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_tuning"


def test_put_non_editable_key_returns_400(client: TestClient):
    resp = client.put("/v1/tuning", json={"rev": 1, "overrides": {"frigate.base_url": "http://x"}})
    assert resp.status_code == 400


def test_put_removal_reverts_to_base(client: TestClient, settings: Settings):
    rev = client.get("/v1/tuning").json()["rev"]
    put1 = client.put("/v1/tuning", json={"rev": rev, "overrides": {"scrub.cell_w": 400}})
    assert settings.scrub.cell_w == 400
    rev2 = put1.json()["rev"]

    # Send an empty overrides set: the previously-set key is removed.
    put2 = client.put("/v1/tuning", json={"rev": rev2, "overrides": {}})
    assert put2.status_code == 200
    assert settings.scrub.cell_w == 320  # back to the pydantic default


def test_pending_restart_lists_non_live_not_live(client: TestClient):
    rev = client.get("/v1/tuning").json()["rev"]
    resp = client.put(
        "/v1/tuning",
        json={
            "rev": rev,
            "overrides": {"scrub.cell_w": 999, "scrub.retention_days": 30},
        },
    )
    assert resp.status_code == 200
    pending = resp.json()["pending_restart"]
    assert "scrub.cell_w" in pending
    assert "scrub.retention_days" not in pending


def test_log_level_put_changes_root_logger(client: TestClient):
    rev = client.get("/v1/tuning").json()["rev"]
    resp = client.put("/v1/tuning", json={"rev": rev, "overrides": {"log_level": "ERROR"}})
    assert resp.status_code == 200
    assert logging.getLogger().level == logging.ERROR
    logging.getLogger().setLevel(logging.NOTSET)


def test_put_writes_file(client: TestClient, settings: Settings):
    rev = client.get("/v1/tuning").json()["rev"]
    client.put("/v1/tuning", json={"rev": rev, "overrides": {"scrub.cell_w": 401}})
    path = tuning.overrides_path(settings)
    on_disk = json.loads(path.read_text())
    assert on_disk["scrub.cell_w"] == 401


def test_get_and_put_return_overrides_dict(client: TestClient):
    get_body = client.get("/v1/tuning").json()
    assert get_body["overrides"] == {}

    rev = get_body["rev"]
    put_body = client.put(
        "/v1/tuning", json={"rev": rev, "overrides": {"scrub.cell_w": 402}}
    ).json()
    assert put_body["overrides"] == {"scrub.cell_w": 402}

    # A fresh GET after the PUT reflects the stored overrides too.
    get_body2 = client.get("/v1/tuning").json()
    assert get_body2["overrides"] == {"scrub.cell_w": 402}


def test_put_dict_int_override_round_trips_only_user_set_keys(client: TestClient):
    rev = client.get("/v1/tuning").json()["rev"]

    put_body = client.put(
        "/v1/tuning",
        json={"rev": rev, "overrides": {"encounters.gap_s": {"person": 30}}},
    ).json()
    assert put_body["overrides"]["encounters.gap_s"] == {"person": 30}

    row = next(k for k in put_body["knobs"] if k["key"] == "encounters.gap_s")
    # The effective (displayed) value is the merged default+override dict,
    # but the stored override -- what the client must seed its local
    # editable set from -- carries only the key the user actually set.
    assert row["value"].get("person") == 30
    assert set(put_body["overrides"]["encounters.gap_s"]) == {"person"}

    get_body = client.get("/v1/tuning").json()
    assert get_body["overrides"]["encounters.gap_s"] == {"person": 30}


def test_put_adjacency_normalises_pairs(client: TestClient, settings: Settings):
    get_resp = client.get("/v1/tuning")
    rev = get_resp.json()["rev"]
    put_resp = client.put(
        "/v1/tuning",
        json={
            "rev": rev,
            "overrides": {"encounters.adjacency": [["shed", "alley-wide"]]},
        },
    )
    assert put_resp.status_code == 200
    assert settings.encounters.adjacency == [["alley-wide", "shed"]]


def test_put_adjacency_rejects_self_pair(client: TestClient):
    get_resp = client.get("/v1/tuning")
    rev = get_resp.json()["rev"]
    put_resp = client.put(
        "/v1/tuning",
        json={"rev": rev, "overrides": {"encounters.adjacency": [["shed", "shed"]]}},
    )
    assert put_resp.status_code == 400
    assert put_resp.json()["detail"]["error"] == "invalid_tuning"


def test_put_adjacency_rejects_unknown_camera(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
):
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras:\n  alley-wide: {}\n  shed: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path,
            bind_port=5001,
            require_frigate_auth=False,
            tuning_path=str(tmp_path / "tuning.json"),
        ),
    )
    tuning.snapshot_startup(settings)
    client = TestClient(create_app(settings))
    get_resp = client.get("/v1/tuning")
    rev = get_resp.json()["rev"]
    put_resp = client.put(
        "/v1/tuning",
        json={"rev": rev, "overrides": {"encounters.adjacency": [["alley-wide", "nope"]]}},
    )
    assert put_resp.status_code == 400
    assert put_resp.json()["detail"]["error"] == "invalid_tuning"
