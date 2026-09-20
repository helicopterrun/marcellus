"""`GET /v1/push/card-for-event/{event_id}` -- the persisted push-card
outcome for a Frigate event id (event detail screen's "why did this alert
me (or not)")."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import card_store, decision_trace
from marcellus.push.cards import Card
from marcellus.server import create_app


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


@pytest.fixture(autouse=True)
def _reset_decision_trace(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    decision_trace.reset_for_tests(conn)
    conn.close()
    yield


def _upsert(sidecar_db_path: Path, card: Card, **ctx: str) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    card_store.upsert_card(conn, card, **ctx)
    conn.close()


def test_direct_card_key_match(client: TestClient, sidecar_db_path: Path) -> None:
    event_id = "1788069103.857271-1asei9"
    card = Card(
        card_key=f"gate-face:person:{event_id}",
        level="urgent",
        peak_level="urgent",
        created_at=100.0,
        updated_at=110.0,
        state_since_at=105.0,
        zone_override_hit=True,
    )
    _upsert(
        sidecar_db_path, card,
        subject_kind="person", place_class="doors", camera="gate-face",
        zone_name="front_door", zones=("front_door", "driveway"),
        label="person", family="visitor",
    )

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["card_key"] == f"gate-face:person:{event_id}"
    assert body["camera"] == "gate-face"
    assert body["subject_kind"] == "person"
    assert body["place_class"] == "doors"
    assert body["label"] == "person"
    assert body["family"] == "visitor"
    assert body["level"] == "urgent"
    assert body["peak_level"] == "urgent"
    assert body["zone_override_hit"] is True
    assert body["zone_name"] == "front_door"
    assert sorted(body["zones"]) == ["driveway", "front_door"]
    assert body["created_at"] == 100.0
    assert body["updated_at"] == 110.0
    assert body["state_since_at"] == 105.0
    assert body["handled"] is False
    assert body["resolved"] is False
    assert body["closed"] is False
    assert body["matched_via"] == "card_key"
    assert body["reasons"] == []


def test_alias_fallback(client: TestClient, sidecar_db_path: Path) -> None:
    event_id = "1788069999.111111-abcdef"
    card = Card(
        card_key="cam-a:stranger:trk-primary",
        level="notify", created_at=1.0, updated_at=2.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, card, subject_kind="stranger", camera="cam-a")

    conn = db.open_sidecar(sidecar_db_path)
    card_store.set_track_alias(conn, "cam-b", event_id, "cam-a:stranger:trk-primary", now=5.0)
    conn.close()

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["card_key"] == "cam-a:stranger:trk-primary"
    assert body["matched_via"] == "alias"


def test_404_when_absent(client: TestClient) -> None:
    r = client.get("/v1/push/card-for-event/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "card_not_found"


def test_zones_csv_parsing_blank(client: TestClient, sidecar_db_path: Path) -> None:
    event_id = "ev-blank-zones"
    card = Card(
        card_key=f"cam:animal:{event_id}", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, card, subject_kind="animal", camera="cam")

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.status_code == 200
    assert r.json()["zones"] == []


def test_zones_csv_parsing_multi(client: TestClient, sidecar_db_path: Path) -> None:
    event_id = "ev-multi-zones"
    card = Card(
        card_key=f"cam:animal:{event_id}", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(
        sidecar_db_path, card, subject_kind="animal", camera="cam",
        zone_name="yard", zones=("yard", "porch", "driveway"),
    )

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.status_code == 200
    assert sorted(r.json()["zones"]) == ["driveway", "porch", "yard"]


def test_zone_override_hit_is_real_bool(client: TestClient, sidecar_db_path: Path) -> None:
    event_id = "ev-override"
    card = Card(
        card_key=f"cam:person:{event_id}", level="urgent",
        created_at=1.0, updated_at=1.0, state_since_at=1.0, zone_override_hit=True,
    )
    _upsert(sidecar_db_path, card, subject_kind="person", camera="cam")

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    body = r.json()
    assert body["zone_override_hit"] is True
    assert isinstance(body["zone_override_hit"], bool)


def test_reasons_populated_when_in_decision_trace(
    client: TestClient, sidecar_db_path: Path
) -> None:
    event_id = "ev-with-reasons"
    card = Card(
        card_key=f"cam:person:{event_id}", level="urgent",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, card, subject_kind="person", camera="cam")
    conn = db.open_sidecar(sidecar_db_path)
    decision_trace.append(
        conn,
        camera="cam", label="person", subject="stranger", zones=["yard"],
        place="yard", level="urgent", reasons=["zone_override", "new_face"],
        event_id=event_id,
    )
    conn.close()

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.json()["reasons"] == ["zone_override", "new_face"]


def test_reasons_empty_when_not_in_decision_trace(
    client: TestClient, sidecar_db_path: Path
) -> None:
    event_id = "ev-no-reasons"
    card = Card(
        card_key=f"cam:person:{event_id}", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, card, subject_kind="person", camera="cam")

    r = client.get(f"/v1/push/card-for-event/{event_id}")
    assert r.json()["reasons"] == []


def test_like_wildcard_characters_do_not_cross_match(
    client: TestClient, sidecar_db_path: Path
) -> None:
    """An event id containing `%` or `_` (SQL LIKE wildcards) must only
    match its own row, never some unrelated row that happens to satisfy the
    wildcard pattern if the query were (mis)implemented with LIKE."""
    decoy = Card(
        card_key="cam:person:1XYZ9", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, decoy, subject_kind="person", camera="cam")

    weird_event_id = "1%9"  # LIKE pattern "cam:person:1%9" would hit the decoy
    target = Card(
        card_key=f"cam:person:{weird_event_id}", level="urgent",
        created_at=2.0, updated_at=2.0, state_since_at=2.0,
    )
    _upsert(sidecar_db_path, target, subject_kind="person", camera="cam")

    r = client.get(f"/v1/push/card-for-event/{weird_event_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["card_key"] == f"cam:person:{weird_event_id}"

    underscore_event_id = "1_9"
    decoy2 = Card(
        card_key="cam:person:1Z9", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    _upsert(sidecar_db_path, decoy2, subject_kind="person", camera="cam")
    target2 = Card(
        card_key=f"cam:person:{underscore_event_id}", level="notify",
        created_at=3.0, updated_at=3.0, state_since_at=3.0,
    )
    _upsert(sidecar_db_path, target2, subject_kind="person", camera="cam")

    r2 = client.get(f"/v1/push/card-for-event/{underscore_event_id}")
    assert r2.status_code == 200
    assert r2.json()["card_key"] == f"cam:person:{underscore_event_id}"
