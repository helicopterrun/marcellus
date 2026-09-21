"""routes/encounters.py: HTML pages 200, /v1 JSON shapes validate against
the wire models, adjacency endpoint (docs/encounters.md "Tests" section)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.store import upsert_atom
from marcellus.models.wire import EncounterResponse, EncountersResponse
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


def _seed_encounter(settings: Settings) -> str:
    conn = db.open_sidecar(settings.sidecar.db_path)
    now = time.time()
    try:
        atom = Atom(
            atom_id="r1",
            camera="alley-wide",
            start_time=now - 100,
            end_time=now - 90,
            labels=("person",),
            zones=("back_walkway",),
            event_ids=("ev1",),
            sub_labels=(),
            severity="alert",
        )
        enc_id = upsert_atom(conn, atom, LinkDecision(None, "new", 1.0), now)
        return enc_id
    finally:
        conn.close()


def test_encounters_page_200(client: TestClient, settings: Settings) -> None:
    _seed_encounter(settings)
    resp = client.get("/encounters")
    assert resp.status_code == 200
    assert "Encounters" in resp.text or "encounter" in resp.text.lower()


def test_encounters_page_empty_200(client: TestClient) -> None:
    resp = client.get("/encounters")
    assert resp.status_code == 200


def test_encounters_page_renders_adjacency_section(client: TestClient) -> None:
    resp = client.get("/encounters")
    assert resp.status_code == 200
    assert "Adjacency" in resp.text
    # alley-wide/shed share the "back_walkway" zone in the fixture config.
    assert "alley-wide" in resp.text
    assert "shed" in resp.text
    assert "back_walkway" in resp.text
    assert 'href="/settings#encounters"' in resp.text


def test_encounter_detail_page_200(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_encounter(settings)
    resp = client.get(f"/encounters/{enc_id}")
    assert resp.status_code == 200


def test_encounter_detail_page_404(client: TestClient) -> None:
    resp = client.get("/encounters/no-such-id")
    assert resp.status_code == 404


def test_v1_encounters_list_shape(client: TestClient, settings: Settings) -> None:
    _seed_encounter(settings)
    resp = client.get("/v1/encounters")
    assert resp.status_code == 200
    body = resp.json()
    EncountersResponse.model_validate(body)
    assert len(body["encounters"]) == 1
    assert body["encounters"][0]["cameras"] == ["alley-wide"]


def test_v1_encounters_list_camera_filter(client: TestClient, settings: Settings) -> None:
    _seed_encounter(settings)
    resp = client.get("/v1/encounters", params={"camera": "no-such-camera"})
    assert resp.status_code == 200
    assert resp.json()["encounters"] == []


def test_v1_encounter_detail_shape(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_encounter(settings)
    resp = client.get(f"/v1/encounters/{enc_id}")
    assert resp.status_code == 200
    body = resp.json()
    EncounterResponse.model_validate(body)
    assert body["encounter"]["id"] == enc_id
    assert len(body["members"]) == 1
    assert body["members"][0]["atom_id"] == "r1"


def test_v1_encounter_detail_404(client: TestClient) -> None:
    resp = client.get("/v1/encounters/no-such-id")
    assert resp.status_code == 404


def test_v1_encounters_adjacency(client: TestClient) -> None:
    resp = client.get("/v1/encounters/adjacency")
    assert resp.status_code == 200
    body = resp.json()
    assert "cameras" in body and "edges" in body
    # alley-wide and shed both define a back_walkway zone in the seeded config.
    assert "alley-wide" in body["cameras"]
    assert "shed" in body["cameras"]
    pair = {tuple(sorted((e["a"], e["b"]))) for e in body["edges"]}
    assert ("alley-wide", "shed") in pair
