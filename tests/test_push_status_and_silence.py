"""`GET /v1/push/status`, `POST /v1/push/silence`, `PUT /v1/push/overrides`
(alerts-slice1 §B/§C)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import card_store, decision_trace, policy_settings
from marcellus.push.cards import Card
from marcellus.server import create_app

CARD_KEY_ZONE = "doorbell:person:t-zone-1"
CARD_KEY_CELL = "backyard:animal:t-cell-1"


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _settings(
    frigate_db_path: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    **push_kwargs: Any,
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
                    }
                }
            }
        )
    )
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
        ),
        push=PushSection(
            enabled=False,
            push_settings_path=str(tmp_path / "push_settings.json"),
            **push_kwargs,
        ),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    return TestClient(create_app(settings))


@pytest.fixture
def sidecar_conn(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    yield conn
    conn.close()


def _make_zone_card(conn: Any) -> None:
    card_store.upsert_card(
        conn,
        Card(card_key=CARD_KEY_ZONE, level="notify", created_at=1.0, updated_at=2.0),
        subject_kind="person",
        place_class="doors",
        camera="doorbell",
        zone_name="front_door",
        label="person",
    )


def _make_outcome_cell_card(conn: Any) -> None:
    card_store.upsert_card(
        conn,
        Card(card_key=CARD_KEY_CELL, level="quiet", created_at=1.0, updated_at=2.0),
        subject_kind="animal",
        place_class="yard",
        camera="backyard",
        zone_name="",
        label="dog",
    )


class TestStatus:
    def test_status_with_no_data(self, client: TestClient):
        resp = client.get("/v1/push/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["frigate_available"] is False
        assert body["mqtt_connected"] is False
        assert body["last_review_at"] is None
        assert body["last_decision_at"] is None
        assert body["last_sent_at"] is None
        assert body["last_sent_level"] is None
        assert body["last_sent_card_key"] is None
        assert body["decisions_since_last_sent"] == 0
        assert body["paused_until"] is None
        assert body["devices"] == 0
        assert "now" in body

    def test_status_after_decision_and_send(self, client: TestClient, sidecar_conn):
        decision_trace.reset_for_tests(sidecar_conn)
        decision_trace.append(
            sidecar_conn,
            camera="doorbell",
            label="person",
            subject="person",
            zones=["front_door"],
            place="doors",
            level="notify",
            reasons=["table"],
            event_id="ev-1",
            card_key=CARD_KEY_ZONE,
            mutation="create",
            sound=True,
            sent=1,
        )
        decision_trace.append(
            sidecar_conn,
            camera="doorbell",
            label="person",
            subject="person",
            zones=["front_door"],
            place="doors",
            level="notify",
            reasons=["table"],
            event_id="ev-2",
            card_key=CARD_KEY_ZONE,
            mutation="escalate",
            sent=0,
        )
        sidecar_conn.commit()
        resp = client.get("/v1/push/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["last_decision_at"] is not None
        assert body["last_sent_at"] is not None
        assert body["last_sent_level"] == "notify"
        assert body["last_sent_card_key"] == CARD_KEY_ZONE
        assert body["decisions_since_last_sent"] == 1


class TestSilence:
    def test_silence_unknown_card_key_404(self, client: TestClient):
        resp = client.post("/v1/push/silence", json={"card_key": "does-not-exist"})
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "card_not_found"

    def test_silence_zone_scoped_card(self, client: TestClient, sidecar_conn):
        _make_zone_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_ZONE})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"]["kind"] == "zone_override"
        assert body["scope"]["zone"] == "front_door"
        assert body["scope"]["subject"] == "person"
        assert body["applied"] == "quiet"
        active = policy_settings.get_active()
        assert active["zone_overrides"]["front_door"]["person"] == "quiet"

    def test_silence_outcome_cell_card_no_zone(self, client: TestClient, sidecar_conn):
        _make_outcome_cell_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"]["kind"] == "outcome_cell"
        assert "zone" not in body["scope"]
        assert body["scope"]["subject"] == "animal"
        assert body["scope"]["place"] == "yard"
        assert body["applied"] == "quiet"
        active = policy_settings.get_active()
        assert active["outcomes"]["animal"]["yard"] == "glance"

    def test_silence_preserves_other_outcome_cells(self, client: TestClient, sidecar_conn):
        """Regression: silencing one outcome cell must not reset every other
        tuned cell to defaults (normalize_settings merges a partial
        `outcomes` doc over `default_settings()`, so a body that only
        contains the one silenced cell would wipe the rest)."""
        # Tune cell A (person/street) to a non-default level first.
        put = client.put(
            "/v1/push/settings",
            json={"outcomes": {"person": {"street": "alarm"}}},
        )
        assert put.status_code == 200
        assert policy_settings.get_active()["outcomes"]["person"]["street"] == "alarm"

        # Silence a different, no-zone card landing on cell B.
        _make_outcome_cell_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert resp.status_code == 200

        active = policy_settings.get_active()
        assert active["outcomes"]["animal"]["yard"] == "glance"  # cell B silenced
        assert active["outcomes"]["person"]["street"] == "alarm"  # cell A untouched
        # normalize_settings derives routing_table_v2 from outcomes -- confirm
        # it stayed in sync with both cells rather than reflecting only B.
        assert active["routing_table_v2"]["animal"]["yard"] == "quiet"
        assert active["routing_table_v2"]["person"]["street"] == "urgent"


class TestOverrides:
    def test_bad_kind_enum_422(self, client: TestClient):
        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "not_a_kind", "subject": "person"},
        )
        assert resp.status_code == 422

    def test_outcome_cell_null_level_422(self, client: TestClient):
        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "animal", "place": "yard", "level": None},
        )
        assert resp.status_code == 422

    def test_zone_override_removal_round_trips(self, client: TestClient):
        put1 = client.put(
            "/v1/push/overrides",
            json={
                "kind": "zone_override",
                "zone": "front_door",
                "subject": "person",
                "level": "quiet",
            },
        )
        assert put1.status_code == 200
        assert policy_settings.get_active()["zone_overrides"]["front_door"]["person"] == "quiet"

        put2 = client.put(
            "/v1/push/overrides",
            json={
                "kind": "zone_override",
                "zone": "front_door",
                "subject": "person",
                "level": None,
            },
        )
        assert put2.status_code == 200
        assert put2.json()["applied"] is None
        assert put2.json()["previous"] == "quiet"
        assert "front_door" not in policy_settings.get_active().get("zone_overrides", {})

    def test_outcome_cell_override_preserves_other_cells(self, client: TestClient):
        """Regression: PUT /overrides on one outcome cell must not reset
        every other tuned cell to defaults (same normalize_settings merge
        hazard as /silence)."""
        put = client.put(
            "/v1/push/settings",
            json={"outcomes": {"person": {"street": "alarm"}}},
        )
        assert put.status_code == 200

        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "animal", "place": "yard", "level": "quiet"},
        )
        assert resp.status_code == 200

        active = policy_settings.get_active()
        assert active["outcomes"]["animal"]["yard"] == "glance"
        assert active["outcomes"]["person"]["street"] == "alarm"
        assert active["routing_table_v2"]["person"]["street"] == "urgent"

    def test_idempotent_same_value_twice(self, client: TestClient):
        body = {
            "kind": "zone_override",
            "zone": "front_door",
            "subject": "person",
            "level": "quiet",
        }
        r1 = client.put("/v1/push/overrides", json=body)
        r2 = client.put("/v1/push/overrides", json=body)
        assert r1.status_code == 200
        assert r2.status_code == 200


def test_full_round_trip_silence_then_restore(client: TestClient, sidecar_conn):
    """Silence a card, confirm the ladder now routes it `quiet`, then
    `PUT /overrides` back to the previous level and confirm normal routing
    resumes."""
    _make_zone_card(sidecar_conn)
    from marcellus.push import ladder_policy
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    previous_level = evaluate_ladder(Snapshot(subject="person", place="doors", zone="front_door"))

    resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_ZONE})
    assert resp.status_code == 200
    assert ladder_policy.ZONE_OVERRIDES.get("front_door", {}).get("person") == "quiet"
    assert evaluate_ladder(Snapshot(subject="person", place="doors", zone="front_door")) == "quiet"

    restore = client.put(
        "/v1/push/overrides",
        json={
            "kind": "zone_override",
            "zone": "front_door",
            "subject": "person",
            "level": None if previous_level not in policy_settings.LEVELS else previous_level,
        },
    )
    assert restore.status_code == 200
    assert (
        evaluate_ladder(Snapshot(subject="person", place="doors", zone="front_door"))
        == previous_level
    )
