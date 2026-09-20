"""`GET /v1/events/{event_id}/related` -- other cameras that saw this object."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import card_store
from marcellus.push.cards import Card
from marcellus.server import create_app


def _add_event(
    db_path: Path,
    *,
    eid: str,
    camera: str,
    label: str,
    start: float,
    end: float | None,
    score: float = 0.9,
) -> None:
    conn = sqlite3.connect(db_path)
    data = json.dumps({"score": score, "top_score": score, "box": [0.1, 0.2, 0.3, 0.4]})
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, "
        "area, ratio, zones, data, has_clip, has_snapshot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, camera, label, start, end, score, score, 5000.0, 1.5, "[]", data, 1, 1),
    )
    conn.commit()
    conn.close()


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


def test_linked_only_match_found(client: TestClient, sidecar_db_path: Path) -> None:
    now = time.time()
    frigate_db = client.app.state.settings.frigate.db_path  # type: ignore[attr-defined]
    _add_event(frigate_db, eid="front-1", camera="front", label="person", start=now, end=now + 5)
    # Far away in time so it can never match via overlap -- only the alias link.
    _add_event(
        frigate_db, eid="back-1", camera="back", label="person",
        start=now + 10_000, end=now + 10_005,
    )

    card = Card(
        card_key="front:person:front-1", level="notify",
        created_at=now, updated_at=now, state_since_at=now,
    )
    conn = db.open_sidecar(sidecar_db_path)
    card_store.upsert_card(conn, card, subject_kind="person", camera="front")
    card_store.set_track_alias(conn, "back", "back-1", "front:person:front-1", now=now)
    conn.close()

    r = client.get("/v1/events/front-1/related")
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == "front-1"
    assert len(body["related"]) == 1
    item = body["related"][0]
    assert item["event_id"] == "back-1"
    assert item["camera"] == "back"
    assert item["source"] == "linked"
    assert item["label"] == "person"
    assert item["start_time"] == now + 10_000
    assert item["end_time"] == now + 10_005
    assert item["score"] == pytest.approx(0.9)


def test_overlap_only_match_found(client: TestClient) -> None:
    now = time.time()
    frigate_db = client.app.state.settings.frigate.db_path  # type: ignore[attr-defined]
    _add_event(frigate_db, eid="e-main", camera="front", label="car", start=now, end=now + 10)
    # Starts 15s after e-main ends -- inside the +/-20s pad.
    _add_event(
        frigate_db, eid="e-overlap", camera="side", label="car", start=now + 25, end=now + 30
    )

    r = client.get("/v1/events/e-main/related")
    assert r.status_code == 200
    body = r.json()
    ids = {e["event_id"] for e in body["related"]}
    assert "e-overlap" in ids
    match = next(e for e in body["related"] if e["event_id"] == "e-overlap")
    assert match["source"] == "overlap"
    assert match["camera"] == "side"


def test_same_camera_event_excluded(client: TestClient, sidecar_db_path: Path) -> None:
    now = time.time()
    frigate_db = client.app.state.settings.frigate.db_path  # type: ignore[attr-defined]
    _add_event(frigate_db, eid="m1", camera="front", label="car", start=now, end=now + 10)
    # Same camera, same label, overlapping time -- must be excluded.
    _add_event(frigate_db, eid="m2", camera="front", label="car", start=now + 2, end=now + 8)

    # Also alias m2 onto the same card as m1 to confirm same-camera exclusion
    # holds for the linked path too.
    card = Card(
        card_key="front:car:m1", level="notify",
        created_at=now, updated_at=now, state_since_at=now,
    )
    conn = db.open_sidecar(sidecar_db_path)
    card_store.upsert_card(conn, card, subject_kind="car", camera="front")
    card_store.set_track_alias(conn, "front", "m2", "front:car:m1", now=now)
    conn.close()

    r = client.get("/v1/events/m1/related")
    assert r.status_code == 200
    body = r.json()
    ids = {e["event_id"] for e in body["related"]}
    assert "m2" not in ids


def test_unknown_event_id_returns_404(client: TestClient) -> None:
    r = client.get("/v1/events/does-not-exist/related")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "event_not_found"


def test_sorted_by_start_time(client: TestClient) -> None:
    now = time.time()
    frigate_db = client.app.state.settings.frigate.db_path  # type: ignore[attr-defined]
    _add_event(frigate_db, eid="q-main", camera="front", label="dog", start=now, end=now + 5)
    _add_event(frigate_db, eid="q-b", camera="side", label="dog", start=now + 15, end=now + 18)
    _add_event(frigate_db, eid="q-a", camera="back", label="dog", start=now - 15, end=now - 12)

    r = client.get("/v1/events/q-main/related")
    body = r.json()
    starts = [e["start_time"] for e in body["related"]]
    assert starts == sorted(starts)
