"""routes/observations.py: /v1/observations list + detail JSON shapes
(docs/encounters.md "Observations")."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.observations import Direction
from marcellus.encounters.store import upsert_atom
from marcellus.server import create_app


@pytest.fixture
def settings(tmp_path: Path, frigate_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _seed(settings: Settings, atom_id: str, *, camera: str = "alley-wide", offset: float = -100.0,
          label: str = "person", direction: Direction | None = None) -> str:
    conn = db.open_sidecar(settings.sidecar.db_path)
    now = time.time()
    try:
        atom = Atom(
            atom_id=atom_id,
            camera=camera,
            start_time=now + offset,
            end_time=now + offset + 10,
            labels=(label,),
            zones=(),
            event_ids=(f"ev-{atom_id}",),
            sub_labels=(),
            severity="alert",
        )
        return upsert_atom(
            conn, atom, LinkDecision(None, "new", 1.0), now, direction=direction
        )
    finally:
        conn.close()


def test_list_returns_seeded_observation(settings: Settings, client: TestClient) -> None:
    _seed(settings, "a1", direction=Direction("front_garden", "shed", "out:shed", None, "zones"))
    resp = client.get("/v1/observations")
    assert resp.status_code == 200
    body = resp.json()
    assert "t" in body
    ids = [o["id"] for o in body["observations"]]
    assert "a1" in ids
    obs = next(o for o in body["observations"] if o["id"] == "a1")
    assert obs["direction"] == "out:shed"
    assert obs["dir_source"] == "zones"
    assert obs["camera"] == "alley-wide"


def test_list_filters_by_camera_and_label(settings: Settings, client: TestClient) -> None:
    _seed(settings, "a1", camera="alley-wide", label="person")
    _seed(settings, "a2", camera="shed", label="car")

    resp = client.get("/v1/observations?cameras=shed")
    ids = [o["id"] for o in resp.json()["observations"]]
    assert ids == ["a2"]

    resp = client.get("/v1/observations?labels=person")
    ids = [o["id"] for o in resp.json()["observations"]]
    assert ids == ["a1"]


def test_detail_includes_encounter_and_neighbours(settings: Settings, client: TestClient) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    now = time.time()
    try:
        enc_id = None
        for i, off in enumerate([-300.0, -200.0, -100.0]):
            atom = Atom(
                atom_id=f"m{i}",
                camera="alley-wide",
                start_time=now + off,
                end_time=now + off + 10,
                labels=("person",),
                zones=(),
                event_ids=(f"ev-m{i}",),
                sub_labels=(),
                severity="alert",
            )
            decision = LinkDecision(enc_id, "same_camera" if enc_id else "new", 1.0)
            enc_id = upsert_atom(conn, atom, decision, now)
    finally:
        conn.close()

    resp = client.get("/v1/observations/m1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["observation"]["id"] == "m1"
    assert body["encounter"]["id"]
    assert body["neighbours"]["prev"]["id"] == "m0"
    assert body["neighbours"]["next"]["id"] == "m2"

    resp0 = client.get("/v1/observations/m0")
    assert resp0.json()["neighbours"]["prev"] is None


def test_detail_404_for_unknown_atom(client: TestClient) -> None:
    resp = client.get("/v1/observations/does-not-exist")
    assert resp.status_code == 404
