"""Tests for the routing decision trace log and endpoint (spec §7,
alerts-slice1 §A) -- durable sqlite (`push_decisions`), not an in-memory
ring buffer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import decision_trace
from marcellus.server import create_app


@pytest.fixture
def conn(sidecar_db_path: Path):
    c = db.open_sidecar(sidecar_db_path)
    decision_trace.reset_for_tests(c)
    yield c
    c.close()


def _make_entry(conn, **kw):
    defaults = dict(
        camera="back_garden",
        label="dog",
        subject="animal",
        zones=["yard"],
        place="yard",
        level="log",
        reasons=["routing_table"],
        event_id="evt-1",
    )
    defaults.update(kw)
    return decision_trace.append(conn, **defaults)


class TestAppendRules:
    def test_initial_decision_is_recorded(self, conn):
        entry = _make_entry(conn)
        assert entry["id"].startswith("dec-")
        assert entry["ts"].endswith("Z")
        assert decision_trace.recent(conn, 10) == [{**entry, "silenced": None}]

    def test_level_change_produces_second_entry(self, conn):
        _make_entry(conn, level="quiet", event_id="evt-1")
        _make_entry(conn, level="notify", event_id="evt-1")
        entries = decision_trace.recent(conn, 10)
        assert len(entries) == 2
        assert entries[0]["level"] == "notify"
        assert entries[1]["level"] == "quiet"

    def test_enrich_does_not_append(self, conn):
        """Enrich-only mutations should NOT be recorded — the caller
        (delivery_wire) is responsible for only calling append on
        CREATE/ESCALATE/DEESCALATE. This test verifies the contract
        by checking the log doesn't grow when we don't call append."""
        _make_entry(conn)
        assert len(decision_trace.recent(conn, 10)) == 1

    def test_recognition_relax_reason(self, conn):
        entry = _make_entry(conn, reasons=["recognition_relax"], level="quiet")
        assert entry["reasons"] == ["recognition_relax"]

    def test_zone_override_reason(self, conn):
        entry = _make_entry(conn, reasons=["zone_override"])
        assert entry["reasons"] == ["zone_override"]

    def test_quiet_hours_cap_reason(self, conn):
        entry = _make_entry(conn, reasons=["routing_table", "quiet_hours_cap"])
        assert "quiet_hours_cap" in entry["reasons"]

    def test_append_swallows_db_error(self):
        """A decision-log write failure must never raise into the push
        path -- it's deep in delivery_wire's per-event routing."""

        class _BoomConn:
            def execute(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")

        assert (
            decision_trace.append(
                _BoomConn(),
                **{
                    "camera": "x",
                    "label": "person",
                    "subject": "stranger",
                    "zones": [],
                    "place": "yard",
                    "level": "log",
                    "reasons": [],
                    "event_id": "evt-boom",
                },
            )
            == {}
        )


class TestServeCap:
    def test_serve_stays_bounded(self, conn):
        for i in range(250):
            _make_entry(conn, event_id=f"evt-{i}")
        entries = decision_trace.recent(conn, 9999)
        assert len(entries) == 200  # serve cap

    def test_all_rows_persist_past_serve_cap(self, conn):
        for i in range(250):
            _make_entry(conn, event_id=f"evt-{i}")
        count = conn.execute("SELECT COUNT(*) AS c FROM push_decisions").fetchone()["c"]
        assert count == 250


class TestRetentionPruning:
    def test_stale_rows_pruned_on_next_append(self, conn, monkeypatch):
        from marcellus.push import decision_trace as dt_mod

        conn.execute(
            "INSERT INTO push_decisions (ts, camera, label, subject, zones_csv, place, "
            "level, reasons_csv, event_id) VALUES "
            "('2020-01-01T00:00:00Z', 'old_cam', 'dog', 'animal', '', 'yard', 'log', '', 'stale-1')"
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM push_decisions WHERE event_id = 'stale-1'"
            ).fetchone()["c"]
            == 1
        )

        monkeypatch.setattr(dt_mod, "_last_prune_at", 0.0)
        _make_entry(conn, event_id="fresh-1")

        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM push_decisions WHERE event_id = 'stale-1'"
            ).fetchone()["c"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM push_decisions WHERE event_id = 'fresh-1'"
            ).fetchone()["c"]
            == 1
        )


class TestRecent:
    def test_newest_first(self, conn):
        _make_entry(conn, event_id="a")
        _make_entry(conn, event_id="b")
        entries = decision_trace.recent(conn, 10)
        assert entries[0]["event_id"] == "b"
        assert entries[1]["event_id"] == "a"

    def test_limit_respected(self, conn):
        for i in range(10):
            _make_entry(conn, event_id=f"evt-{i}")
        assert len(decision_trace.recent(conn, 3)) == 3

    def test_limit_capped_at_200(self, conn):
        for i in range(250):
            _make_entry(conn, event_id=f"evt-{i}")
        assert len(decision_trace.recent(conn, 9999)) == 200

    def test_empty_log(self, conn):
        assert decision_trace.recent(conn, 50) == []

    def test_before_cursor_pagination(self, conn):
        for i in range(5):
            _make_entry(conn, event_id=f"evt-{i}")
        first_page = decision_trace.recent(conn, 2)
        assert [e["event_id"] for e in first_page] == ["evt-4", "evt-3"]
        cursor = first_page[-1]["id"]
        second_page = decision_trace.recent(conn, 2, before=cursor)
        assert [e["event_id"] for e in second_page] == ["evt-2", "evt-1"]

    def test_card_key_filter(self, conn):
        _make_entry(conn, event_id="a", card_key="cam:package:t1")
        _make_entry(conn, event_id="b", card_key="cam:person:t2")
        _make_entry(conn, event_id="c", card_key="cam:package:t1")
        entries = decision_trace.recent(conn, 10, card_key="cam:package:t1")
        assert {e["event_id"] for e in entries} == {"a", "c"}


class TestEntryShape:
    def test_all_fields_present(self, conn):
        _make_entry(conn)
        entry = decision_trace.recent(conn, 10)[0]
        required = {
            "id",
            "ts",
            "camera",
            "label",
            "subject",
            "zones",
            "place",
            "level",
            "reasons",
            "event_id",
            "stage",
            "modifiers",
            "card_key",
            "mutation",
            "zone",
            "sound",
            "sent",
            "silenced",
        }
        assert required == set(entry.keys())

    def test_ts_is_iso_utc_with_z(self, conn):
        entry = _make_entry(conn)
        ts = entry["ts"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_id_is_unique(self, conn):
        a = _make_entry(conn, event_id="a")
        b = _make_entry(conn, event_id="b")
        assert a["id"] != b["id"]

    def test_no_sub_label_in_entry(self, conn):
        entry = _make_entry(conn)
        assert "sub_label" not in entry
        assert "identity" not in entry

    def test_stage_and_modifiers_recorded(self, conn):
        entry = _make_entry(
            conn,
            stage="table",
            modifiers=("nudge_up", "street_cap"),
            card_key="cam:animal:t1",
            mutation="create",
            zone="yard",
            sound=True,
            sent=1,
        )
        assert entry["stage"] == "table"
        assert entry["modifiers"] == ["nudge_up", "street_cap"]
        assert entry["card_key"] == "cam:animal:t1"
        assert entry["mutation"] == "create"
        assert entry["zone"] == "yard"
        assert entry["sound"] is True
        assert entry["sent"] == 1


class TestSilencedField:
    def test_matching_zone_silence_surfaced(self, conn):
        entry = _make_entry(conn, zone="yard", subject="animal", place="yard")
        conn.execute(
            "INSERT INTO push_silences (ts, card_key, kind, zone, subject, place, "
            "previous, applied) VALUES ('2026-01-01T00:00:00Z', '', 'zone_override', "
            "'yard', 'animal', 'yard', NULL, 'quiet')"
        )
        conn.commit()
        entries = decision_trace.recent(conn, 10)
        assert entries[0]["id"] == entry["id"]
        assert entries[0]["silenced"] == {"zone": "yard", "subject": "animal", "level": "quiet"}

    def test_unrelated_decision_not_silenced(self, conn):
        _make_entry(conn, zone="other_zone", subject="animal", place="yard")
        conn.execute(
            "INSERT INTO push_silences (ts, card_key, kind, zone, subject, place, "
            "previous, applied) VALUES ('2026-01-01T00:00:00Z', '', 'zone_override', "
            "'yard', 'animal', 'yard', NULL, 'quiet')"
        )
        conn.commit()
        entries = decision_trace.recent(conn, 10)
        assert entries[0]["silenced"] is None

    def test_no_silences_at_all_is_none(self, conn):
        _make_entry(conn)
        entries = decision_trace.recent(conn, 10)
        assert entries[0]["silenced"] is None


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(yaml.safe_dump({"cameras": {}}))
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
        ),
        push=PushSection(
            enabled=False,
            push_settings_path=str(tmp_path / "push_settings.json"),
        ),
    )
    return TestClient(create_app(settings))


class TestEndpoint:
    def test_decisions_returns_200_with_entries(self, client: TestClient, conn):
        _make_entry(conn, event_id="a")
        _make_entry(conn, event_id="b")
        resp = client.get("/v1/push/decisions?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["decisions"]) == 2
        assert body["decisions"][0]["event_id"] == "b"

    def test_decisions_limit_caps_at_200(self, client: TestClient, conn):
        for i in range(250):
            _make_entry(conn, event_id=f"evt-{i}")
        # The route's own `Query(..., le=200)` rejects anything above 200
        # before it ever reaches `decision_trace.recent`'s internal cap.
        resp = client.get("/v1/push/decisions?limit=9999")
        assert resp.status_code == 422
        resp = client.get("/v1/push/decisions?limit=200")
        assert resp.status_code == 200
        assert len(resp.json()["decisions"]) == 200

    def test_decisions_empty(self, client: TestClient):
        resp = client.get("/v1/push/decisions")
        assert resp.status_code == 200
        assert resp.json() == {"decisions": []}

    def test_decisions_card_key_filter(self, client: TestClient, conn):
        _make_entry(conn, event_id="a", card_key="cam:package:t1")
        _make_entry(conn, event_id="b", card_key="cam:person:t2")
        resp = client.get("/v1/push/decisions?card_key=cam:package:t1")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["event_id"] == "a"

    def test_capabilities_includes_decisions(self, client: TestClient):
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        assert resp.json()["decisions"] == {"enabled": True}

    def test_capabilities_advertises_attention_subjects(self, client: TestClient):
        from marcellus.push import policy_settings

        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        assert resp.json()["push"]["attention_subjects"] == list(policy_settings.SUBJECTS_V3)


class TestAnnotate:
    def test_annotate_patches_matching_entry(self, conn):
        entry = decision_trace.append(
            conn,
            camera="porch",
            label="package",
            subject="package",
            zones=["porch"],
            place="doors",
            level="quiet",
            reasons=["routing_table"],
            event_id="ev-la-1",
        )
        decision_trace.annotate(
            conn,
            "ev-la-1",
            family="package",
            la_started=True,
            la_reason="started",
        )
        entry = decision_trace.recent(conn, 10, card_key=None)[0]
        assert entry["family"] == "package"
        assert entry["la_started"] is True
        assert entry["la_reason"] == "started"

    def test_annotate_targets_newest_entry_for_event(self, conn):
        decision_trace.append(
            conn,
            camera="porch",
            label="package",
            subject="package",
            zones=[],
            place="doors",
            level="quiet",
            reasons=[],
            event_id="ev-la-2",
        )
        decision_trace.append(
            conn,
            camera="porch",
            label="package",
            subject="package",
            zones=[],
            place="doors",
            level="notify",
            reasons=[],
            event_id="ev-la-2",
        )
        decision_trace.annotate(
            conn,
            "ev-la-2",
            la_started=False,
            la_reason="device_not_la_capable",
        )
        entries = [e for e in decision_trace.recent(conn, 200) if e["event_id"] == "ev-la-2"]
        assert entries[0]["la_started"] is False
        assert "la_started" not in entries[-1]

    def test_annotate_unknown_event_is_a_noop(self, conn):
        decision_trace.annotate(conn, "ev-does-not-exist", family="package")

    def test_annotate_sent_and_sound(self, conn):
        decision_trace.append(
            conn,
            camera="porch",
            label="package",
            subject="package",
            zones=[],
            place="doors",
            level="notify",
            reasons=[],
            event_id="ev-sent-1",
        )
        decision_trace.annotate(conn, "ev-sent-1", sent=2, sound=True)
        entry = [e for e in decision_trace.recent(conn, 10) if e["event_id"] == "ev-sent-1"][0]
        assert entry["sent"] == 2
        assert entry["sound"] is True


class TestReasonsFor:
    def test_reasons_for_returns_newest(self, conn):
        decision_trace.append(
            conn,
            camera="c",
            label="x",
            subject="animal",
            zones=[],
            place="yard",
            level="log",
            reasons=["a"],
            event_id="ev-r",
        )
        decision_trace.append(
            conn,
            camera="c",
            label="x",
            subject="animal",
            zones=[],
            place="yard",
            level="notify",
            reasons=["b", "c"],
            event_id="ev-r",
        )
        assert decision_trace.reasons_for(conn, "ev-r") == ["b", "c"]

    def test_reasons_for_unknown_event(self, conn):
        assert decision_trace.reasons_for(conn, "does-not-exist") == []
