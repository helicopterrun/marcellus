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


def _ladder_body(subject: str, place: str, value: str) -> dict[str, Any]:
    """A complete settings body with one ladder cell changed (the core four
    subjects must be sent whole; a partial ladder is a 400)."""
    body = policy_settings.default_settings()
    body["ladder"][subject][place] = value
    return body


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
        # v3 §5.7: silence applies Quiet = engine level `silent`.
        assert body["applied"] == "silent"
        assert body["previous"] is None  # no override existed before
        active = policy_settings.get_active()
        assert active["zone_overrides"]["front_door"]["person"] == "silent"

    def test_silence_outcome_cell_card_no_zone(self, client: TestClient, sidecar_conn):
        _make_outcome_cell_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert resp.status_code == 200
        body = resp.json()
        assert body["scope"]["kind"] == "outcome_cell"
        assert "zone" not in body["scope"]
        assert body["scope"]["subject"] == "animal"
        assert body["scope"]["place"] == "yard"
        assert body["applied"] == "silent"
        assert body["previous"] == "quiet"  # default animal/yard = Glance
        active = policy_settings.get_active()
        # The ladder is authoritative; the legacy outcomes view is derived
        # (silent -> notify).
        assert active["ladder"]["animal"]["yard"] == "silent"
        assert active["outcomes"]["animal"]["yard"] == "notify"

    def test_silence_preserves_other_outcome_cells(self, client: TestClient, sidecar_conn):
        """Regression: silencing one outcome cell must not reset every other
        tuned cell to defaults (normalize_settings merges a partial
        `ladder` doc over `default_settings()`, so a body that only
        contains the one silenced cell would wipe the rest)."""
        # Tune cell A (person/street) to a non-default level first.
        put = client.put(
            "/v1/push/settings",
            json=_ladder_body("person", "street", "urgent"),
        )
        assert put.status_code == 200
        assert policy_settings.get_active()["ladder"]["person"]["street"] == "urgent"

        # Silence a different, no-zone card landing on cell B.
        _make_outcome_cell_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert resp.status_code == 200

        active = policy_settings.get_active()
        assert active["ladder"]["animal"]["yard"] == "silent"  # cell B silenced
        assert active["ladder"]["person"]["street"] == "urgent"  # cell A untouched
        # normalize_settings derives outcomes / routing_table_v2 from the
        # ladder -- confirm they stayed in sync with both cells.
        assert active["outcomes"]["animal"]["yard"] == "notify"
        assert active["routing_table_v2"]["animal"]["yard"] == "notify"
        assert active["outcomes"]["person"]["street"] == "alarm"
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
            json=_ladder_body("person", "street", "urgent"),
        )
        assert put.status_code == 200

        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "animal", "place": "yard", "level": "quiet"},
        )
        assert resp.status_code == 200

        active = policy_settings.get_active()
        assert active["ladder"]["animal"]["yard"] == "glance"
        assert active["ladder"]["person"]["street"] == "urgent"
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
    """Silence a card, confirm the ladder now routes it `silent`, then
    `PUT /overrides` back to the previous level and confirm normal routing
    resumes."""
    _make_zone_card(sidecar_conn)
    from marcellus.push import ladder_policy
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    previous_level = evaluate_ladder(Snapshot(subject="person", place="doors", zone="front_door"))

    resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_ZONE})
    assert resp.status_code == 200
    assert ladder_policy.ZONE_OVERRIDES.get("front_door", {}).get("person") == "silent"
    assert evaluate_ladder(Snapshot(subject="person", place="doors", zone="front_door")) == "silent"

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


class TestV3Silence:
    """Ladder v3 §5.7: silence applies `silent`; overrides edit the ladder."""

    def test_zone_override_card_applies_silent(self, client: TestClient, sidecar_conn):
        _make_zone_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_ZONE})
        assert resp.status_code == 200
        assert resp.json()["applied"] == "silent"
        active = policy_settings.get_active()
        assert active["zone_overrides"]["front_door"]["person"] == "silent"
        # The ladder cell itself is untouched by a zone-scoped silence.
        assert active["ladder"]["person"]["doors"] == "glance"

    def test_outcome_cell_card_sets_ladder_silent_and_derives_notify(
        self, client: TestClient, sidecar_conn,
    ):
        _make_outcome_cell_card(sidecar_conn)
        resp = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert resp.status_code == 200
        assert resp.json()["applied"] == "silent"
        active = policy_settings.get_active()
        assert active["ladder"]["animal"]["yard"] == "silent"
        assert active["outcomes"]["animal"]["yard"] == "notify"

    def test_put_override_notify_is_stored_and_returned_as_silent(self, client: TestClient):
        resp = client.put(
            "/v1/push/overrides",
            json={
                "kind": "zone_override",
                "zone": "front_door",
                "subject": "person",
                "level": "notify",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == "silent"
        active = policy_settings.get_active()
        assert active["zone_overrides"]["front_door"]["person"] == "silent"

    def test_outcome_cell_off_sets_ladder_ignore(self, client: TestClient):
        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "animal", "place": "yard", "level": "off"},
        )
        assert resp.status_code == 200
        # Non-person Ignore reads back as `off` (suppressed).
        assert resp.json()["applied"] == "off"
        active = policy_settings.get_active()
        assert active["ladder"]["animal"]["yard"] == "ignore"
        assert active["outcomes"]["animal"]["yard"] == "off"

    def test_outcome_cell_off_for_person_reports_log(self, client: TestClient):
        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "person", "place": "yard", "level": "off"},
        )
        assert resp.status_code == 200
        assert resp.json()["applied"] == "log"
        assert policy_settings.get_active()["ladder"]["person"]["yard"] == "ignore"

    @pytest.mark.parametrize("level", ["glance", "alarm"])
    def test_outcome_cell_rejects_outcome_words(self, client: TestClient, level: str):
        resp = client.put(
            "/v1/push/overrides",
            json={"kind": "outcome_cell", "subject": "animal", "place": "yard", "level": level},
        )
        assert resp.status_code == 422
        # Nothing was written.
        assert policy_settings.get_active()["ladder"]["animal"]["yard"] == "glance"

    def test_silence_previous_restores_ladder_cell_via_overrides(
        self, client: TestClient, sidecar_conn,
    ):
        # Start from an Ignore cell: a non-person `previous` is "off".
        put = client.put(
            "/v1/push/settings",
            json=_ladder_body("animal", "yard", "ignore"),
        )
        assert put.status_code == 200
        assert policy_settings.get_active()["ladder"]["animal"]["yard"] == "ignore"

        _make_outcome_cell_card(sidecar_conn)
        silenced = client.post("/v1/push/silence", json={"card_key": CARD_KEY_CELL})
        assert silenced.status_code == 200
        body = silenced.json()
        assert body["previous"] == "off"
        assert policy_settings.get_active()["ladder"]["animal"]["yard"] == "silent"

        restore = client.put(
            "/v1/push/overrides",
            json={
                "kind": "outcome_cell",
                "subject": "animal",
                "place": "yard",
                "level": body["previous"],
            },
        )
        assert restore.status_code == 200
        assert policy_settings.get_active()["ladder"]["animal"]["yard"] == "ignore"
