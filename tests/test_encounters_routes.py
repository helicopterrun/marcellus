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


def _seed_atom(settings: Settings, atom_id: str, *, start_offset: float = -100.0) -> str:
    conn = db.open_sidecar(settings.sidecar.db_path)
    now = time.time()
    try:
        atom = Atom(
            atom_id=atom_id,
            camera="alley-wide",
            start_time=now + start_offset,
            end_time=now + start_offset + 10,
            labels=("person",),
            zones=(),
            event_ids=(f"ev-{atom_id}",),
            sub_labels=(),
            severity="alert",
        )
        return upsert_atom(conn, atom, LinkDecision(None, "new", 1.0), now)
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


# --------------------------------------------------------------------------
# Human decisions: split / pin / merge / undo
# --------------------------------------------------------------------------


def test_split_atom_route_redirects_to_new_encounter(
    client: TestClient, settings: Settings
) -> None:
    enc_id = _seed_atom(settings, "s1")
    resp = client.post(f"/encounters/{enc_id}/atoms/s1/split", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/encounters/")
    assert f"/encounters/{enc_id}" not in location


def test_split_atom_route_unknown_atom_404(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_atom(settings, "s1")
    resp = client.post(f"/encounters/{enc_id}/atoms/no-such-atom/split", follow_redirects=False)
    assert resp.status_code == 404


def test_pin_atom_route_happy_path(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "p1", start_offset=-200.0)
    enc_b = _seed_atom(settings, "p2", start_offset=-10.0)
    resp = client.post(
        f"/encounters/{enc_b}/atoms/p2/pin",
        data={"target": enc_a},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/encounters/{enc_a}?msg=Pinned%20p2%20here"


def test_pin_atom_route_unknown_target_400(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "p1")
    resp = client.post(
        f"/encounters/{enc_a}/atoms/p1/pin",
        data={"target": "no-such-encounter"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_merge_encounters_route_happy_path(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "m1", start_offset=-200.0)
    enc_b = _seed_atom(settings, "m2", start_offset=-10.0)
    resp = client.post(f"/encounters/{enc_a}/merge", data={"source": enc_b}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/encounters/{enc_a}?msg=")

    detail = client.get(f"/encounters/{enc_a}")
    assert detail.status_code == 200
    assert "m2" in detail.text


def test_undo_decisions_route(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_atom(settings, "u1")
    resp = client.post(f"/encounters/{enc_id}/atoms/u1/undo", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/encounters/{enc_id}?msg=")


def test_v1_split_atom(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_atom(settings, "s1")
    resp = client.post(f"/v1/encounters/{enc_id}/atoms/s1/split")
    assert resp.status_code == 200
    body = resp.json()
    assert body["encounter"]["id"] != enc_id
    assert body["members"][0]["atom_id"] == "s1"


def test_v1_pin_atom(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "p1", start_offset=-200.0)
    enc_b = _seed_atom(settings, "p2", start_offset=-10.0)
    resp = client.post(f"/v1/encounters/{enc_b}/atoms/p2/pin", json={"target": enc_a})
    assert resp.status_code == 200
    body = resp.json()
    assert body["encounter"]["id"] == enc_a
    atom_ids = {m["atom_id"] for m in body["members"]}
    assert atom_ids == {"p1", "p2"}


def test_v1_pin_atom_missing_target_400(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "p1")
    resp = client.post(f"/v1/encounters/{enc_a}/atoms/p1/pin", json={})
    assert resp.status_code == 400


def test_v1_merge_encounters(client: TestClient, settings: Settings) -> None:
    enc_a = _seed_atom(settings, "m1", start_offset=-200.0)
    enc_b = _seed_atom(settings, "m2", start_offset=-10.0)
    resp = client.post(f"/v1/encounters/{enc_a}/merge", json={"source": enc_b})
    assert resp.status_code == 200
    body = resp.json()
    atom_ids = {m["atom_id"] for m in body["members"]}
    assert atom_ids == {"m1", "m2"}


def test_v1_undo_decisions(client: TestClient, settings: Settings) -> None:
    enc_id = _seed_atom(settings, "u1")
    resp = client.post(f"/v1/encounters/{enc_id}/atoms/u1/undo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["encounter"]["id"] == enc_id


def test_decision_routes_require_auth(tmp_path: Path, frigate_db_path: Path) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras: {}\n")
    gated_settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=tmp_path / "sidecar.db", bind_port=5001, require_frigate_auth=True
        ),
    )
    enc_id = _seed_atom(gated_settings, "a1")
    gated_client = TestClient(create_app(gated_settings))
    for path, kwargs in (
        (f"/encounters/{enc_id}/atoms/a1/split", {}),
        (f"/encounters/{enc_id}/atoms/a1/pin", {"data": {"target": enc_id}}),
        (f"/encounters/{enc_id}/merge", {"data": {"source": enc_id}}),
        (f"/encounters/{enc_id}/atoms/a1/undo", {}),
        (f"/v1/encounters/{enc_id}/atoms/a1/split", {}),
        (f"/v1/encounters/{enc_id}/atoms/a1/pin", {"json": {"target": enc_id}}),
        (f"/v1/encounters/{enc_id}/merge", {"json": {"source": enc_id}}),
        (f"/v1/encounters/{enc_id}/atoms/a1/undo", {}),
    ):
        resp = gated_client.post(path, follow_redirects=False, **kwargs)
        assert resp.status_code in (401, 403), path
