"""Routing v2 — observable subjects, identity as modifier.

Tests the create-time routing table (person/vehicle/animal/thing),
recognition deescalation, identity display, migration, capability
probe, and backward compatibility.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import policy_settings, store
from marcellus.push.delivery_wire import (
    _relaxed_level,
    classify_subject,
    handle_delivery_event,
    handle_recognition_event,
)
from marcellus.push.ladder import Snapshot, evaluate_ladder
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport


def _device(
    token: str = "tok1", *, push_to_start: str = "pts1", frequent_pushes_enabled: bool = True,
) -> Device:
    # Defaults to the fast (3s) LA update cadence: this file's timing
    # assumptions predate Phase A's two-tier pacing (15s default absent the
    # flag).
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", push_to_start_token=push_to_start,
        min_severity="detection", frequent_pushes_enabled=frequent_pushes_enabled,
    )


def _event(
    camera: str = "doorbell",
    track_id: str = "trk1",
    label: str = "person",
    zones: tuple[str, ...] = ("pool",),
    sub_labels: tuple[str, ...] = (),
) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=(label,), track_ids=(track_id,), zones=zones,
        sub_labels=sub_labels,
    )


def _card_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if "payload" in r and not r.get("live_activity")]


def _la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


# ── 1. Create-time routing uses observable subjects ───────────────────────

class TestClassifySubject:
    def test_person_label_returns_person(self):
        assert classify_subject(_event(label="person")) == "person"

    def test_person_with_sub_label_still_returns_person(self):
        assert classify_subject(_event(label="person", sub_labels=("Sarah",))) == "person"

    def test_car_returns_vehicle(self):
        assert classify_subject(_event(label="car")) == "vehicle"

    def test_motorcycle_returns_vehicle(self):
        assert classify_subject(_event(label="motorcycle")) == "vehicle"

    def test_dog_returns_animal(self):
        assert classify_subject(_event(label="dog")) == "animal"

    def test_package_returns_package(self):
        assert classify_subject(_event(label="package")) == "package"


class TestV2TableRouting:
    def test_person_at_doors_is_notify(self):
        policy_settings.apply_settings(policy_settings.default_settings())
        snap = Snapshot(subject="person", place="doors", label="person")
        assert evaluate_ladder(snap) == "notify"

    def test_vehicle_at_off_limits_is_notify(self):
        policy_settings.apply_settings(policy_settings.default_settings())
        snap = Snapshot(subject="vehicle", place="off_limits", label="car")
        assert evaluate_ladder(snap) == "notify"

    def test_person_at_off_limits_is_urgent(self):
        policy_settings.apply_settings(policy_settings.default_settings())
        snap = Snapshot(subject="person", place="off_limits", label="person")
        assert evaluate_ladder(snap) == "urgent"

    def test_animal_at_yard_is_quiet(self):
        policy_settings.apply_settings(policy_settings.default_settings())
        snap = Snapshot(subject="animal", place="yard", label="dog")
        assert evaluate_ladder(snap) == "quiet"


@pytest.mark.asyncio
async def test_create_time_routing_ignores_sub_label(sidecar_db_path: Path):
    """A person with a sub_label routes at the person row's tier,
    not some relaxed tier — identity only modifies via recognition."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",), sub_labels=("Sarah",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sends = _card_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "urgent"


# ── 2. Recognition deescalates silently ──────────────────────────────────

class TestRelaxedLevel:
    def test_relax_one_from_urgent(self):
        assert _relaxed_level("urgent", "relax_one") == "notify"

    def test_relax_one_from_notify(self):
        assert _relaxed_level("notify", "relax_one") == "quiet"

    def test_relax_one_from_quiet(self):
        assert _relaxed_level("quiet", "relax_one") == "log"

    def test_relax_one_from_log_returns_none(self):
        assert _relaxed_level("log", "relax_one") is None

    def test_relax_to_quiet_from_urgent(self):
        assert _relaxed_level("urgent", "relax_to_quiet") == "quiet"

    def test_relax_to_quiet_from_notify(self):
        assert _relaxed_level("notify", "relax_to_quiet") == "quiet"

    def test_relax_to_quiet_from_quiet_returns_none(self):
        assert _relaxed_level("quiet", "relax_to_quiet") is None

    def test_off_returns_none(self):
        assert _relaxed_level("urgent", "off") is None


@pytest.mark.asyncio
async def test_recognition_deescalates_silently(sidecar_db_path: Path):
    """Recognition relax_one: person at urgent → deescalate to notify.
    The deescalate mutation is silent: no sound, card push passive."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["recognition"] = {"known_person": "relax_one", "known_vehicle": "relax_one"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    assert _card_sends(transport)[0]["payload"]["level"] == "urgent"

    transport2 = LogTransport()
    result = await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[device], transport=transport2, config=config,
        label="person", now=5.0,
    )
    assert result == 1
    sends = _card_sends(transport2)
    assert len(sends) == 1
    aps = sends[0]["payload"]["aps"]
    assert sends[0]["payload"]["level"] == "notify"
    assert sends[0]["payload"]["mutation"] == "deescalate"
    assert "sound" not in aps
    assert aps["interruption-level"] == "passive"


@pytest.mark.asyncio
async def test_recognition_la_update_no_alert(sidecar_db_path: Path):
    """Recognition deescalate LA update: no alert dict, no sound."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)
    card_key = "doorbell:person:trk1"

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["recognition"] = {"known_person": "relax_to_quiet", "known_vehicle": "relax_one"}
    settings["routing_table_v2"]["person"]["doors"] = "notify"
    policy_settings.apply_settings(settings)

    # Create at doors (notify) — qualifies for person LA family.
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [s for s in _la_sends(transport) if s["event"] == "start"]
    assert len(starts) == 1

    row = conn.execute(
        "SELECT activity_id FROM push_activities WHERE apns_token = ?",
        (device.apns_token,),
    ).fetchone()
    assert row is not None, f"no activity row for {card_key}"
    store.attach_activity_token(
        conn, activity_id=row["activity_id"],
        apns_token=device.apns_token, situation_id=store.DEVICE_SITUATION_ID,
        track_id=store.DEVICE_TRACK_ID, token="perAct1",
    )

    transport2 = LogTransport()
    await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[device], transport=transport2, config=config,
        label="person", now=5.0,
    )
    la = _la_sends(transport2)
    assert len(la) >= 1
    # Device-scoped LA: relaxing to "quiet" drops the card below LA
    # eligibility, so this now closes the aggregate activity ("end") rather
    # than sending a card-specific "update". Either way it must stay silent:
    # no alert, no sound.
    update = [s for s in la if s["event"] in ("update", "end")][-1]
    assert "alert" not in update["payload"]["aps"]
    assert "sound" not in update["payload"]["aps"]


@pytest.mark.asyncio
async def test_unrecognized_result_is_noop(sidecar_db_path: Path):
    """An unrecognized result (face ran, matched nobody) changes nothing."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # No sub_label — nothing to recognize. (In practice this path isn't
    # called without a sub_label, but verify the guard works.)
    transport2 = LogTransport()
    result = await handle_recognition_event(
        "doorbell", "trk1", "",
        conn=conn, devices=[device], transport=transport2, config=config,
        label="person", now=5.0,
    )
    assert result == 0
    assert _card_sends(transport2) == []


@pytest.mark.asyncio
async def test_recognition_off_disables_modifier(sidecar_db_path: Path):
    """recognition.known_person=off disables the deescalation modifier."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["recognition"] = {"known_person": "off", "known_vehicle": "off"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    transport2 = LogTransport()
    result = await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[device], transport=transport2, config=config,
        label="person", now=5.0,
    )
    assert result == 0
    assert _card_sends(transport2) == []


# ── 3. Identity display ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_identity_in_copy_on_enrich(sidecar_db_path: Path):
    """When a sub_label exists on the review message, the card copy
    shows the identity: 'Sarah · at Pool'."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",), sub_labels=("Sarah",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5.0,
    )
    sends = _card_sends(transport)
    enrich = sends[-1]["payload"]
    assert "Sarah" in enrich["primary"]


# ── 4. Migration ────────────────────────────────────────────────────────

class TestMigration:
    def test_migration_person_row_from_stranger(self):
        legacy = {
            "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
                         "private": "notify", "off_limits": "urgent"},
            "known": {"street": "log", "yard": "log", "doors": "quiet",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        v2, recognition, log_msg = policy_settings.migrate_v1_to_v2(legacy)
        assert v2["person"] == legacy["stranger"]

    def test_migration_vehicle_bumps_thing_at_doors_and_off_limits(self):
        legacy = {
            "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
                         "private": "notify", "off_limits": "urgent"},
            "known": {"street": "log", "yard": "log", "doors": "quiet",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        v2, _, _ = policy_settings.migrate_v1_to_v2(legacy)
        assert v2["vehicle"]["doors"] == "quiet"  # log bumped one → quiet
        assert v2["vehicle"]["off_limits"] == "notify"  # quiet bumped one → notify
        assert v2["vehicle"]["street"] == "log"  # unchanged

    def test_migration_animal_copies(self):
        legacy = {
            "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
                         "private": "notify", "off_limits": "urgent"},
            "known": {"street": "log", "yard": "log", "doors": "quiet",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        v2, _, _ = policy_settings.migrate_v1_to_v2(legacy)
        assert v2["animal"] == legacy["animal"]

    def test_migration_thing_copies(self):
        legacy = {
            "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
                         "private": "notify", "off_limits": "urgent"},
            "known": {"street": "log", "yard": "log", "doors": "quiet",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        v2, _, _ = policy_settings.migrate_v1_to_v2(legacy)
        assert v2["thing"] == legacy["thing"]

    def test_migration_known_person_relax_one_inference(self):
        legacy = {
            "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
                         "private": "notify", "off_limits": "urgent"},
            "known": {"street": "log", "yard": "log", "doors": "quiet",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        _, recognition, _ = policy_settings.migrate_v1_to_v2(legacy)
        assert recognition["known_person"] == "relax_one"

    def test_migration_known_person_off_when_rows_close(self):
        legacy = {
            "stranger": {"street": "log", "yard": "log", "doors": "quiet",
                         "private": "quiet", "off_limits": "quiet"},
            "known": {"street": "log", "yard": "log", "doors": "log",
                      "private": "quiet", "off_limits": "quiet"},
            "animal": {"street": "log", "yard": "quiet", "doors": "quiet",
                       "private": "quiet", "off_limits": "quiet"},
            "thing": {"street": "log", "yard": "log", "doors": "log",
                      "private": "log", "off_limits": "quiet"},
        }
        _, recognition, _ = policy_settings.migrate_v1_to_v2(legacy)
        assert recognition["known_person"] == "off"

    def test_migration_from_live_table(self):
        """The live production table should produce a sensible v2."""
        live = {
            "animal": {"doors": "log", "off_limits": "log", "private": "log",
                       "street": "log", "yard": "log"},
            "known": {"doors": "quiet", "off_limits": "quiet", "private": "log",
                      "street": "log", "yard": "log"},
            "stranger": {"doors": "notify", "off_limits": "urgent", "private": "notify",
                         "street": "log", "yard": "log"},
            "thing": {"doors": "quiet", "off_limits": "quiet", "private": "log",
                      "street": "log", "yard": "log"},
        }
        v2, recognition, log_msg = policy_settings.migrate_v1_to_v2(live)
        assert v2["person"] == live["stranger"]
        assert v2["vehicle"]["doors"] == "notify"  # quiet bumped → notify
        assert v2["vehicle"]["off_limits"] == "notify"  # quiet bumped → notify
        assert recognition["known_person"] == "relax_one"
        assert "routing v2 migration" in log_msg

    def test_startup_triggers_migration(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        legacy = {
            "v": 1,
            "routing_table": dict(policy_settings.DEFAULT_ROUTING_TABLE),
            "zone_classes": {},
            "zone_overrides": {},
            "live_activities": {"person": True, "package": True, "bins": True, "openings": True,
                                "opening_picks": [], "alert_all_changes": False, "la_only": False},
            "mute_sounds": True,
            "quiet_hours": None,
        }
        with settings_path.open("w") as f:
            json.dump(legacy, f)

        result = policy_settings.startup(settings_path)
        assert "routing_table_v2" in result
        # The V3 seeding runs in the same startup, so all seven rows exist.
        assert set(result["routing_table_v2"]) == set(policy_settings.SUBJECTS_V3)
        assert "recognition" in result

        reloaded = json.loads(settings_path.read_text())
        assert "routing_table_v2" in reloaded

    def test_startup_no_double_migration(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        settings = policy_settings.default_settings()
        with settings_path.open("w") as f:
            json.dump(settings, f)

        result = policy_settings.startup(settings_path)
        assert result["routing_table_v2"] == settings["routing_table_v2"]


# ── 5. Capability probe ─────────────────────────────────────────────────

class TestRecognitionAvailable:
    def test_faces_and_plates_disabled(self, tmp_path):
        import yaml
        config = {"face_recognition": {"enabled": False}, "lpr": {"enabled": False}}
        p = tmp_path / "config.yml"
        with p.open("w") as f:
            yaml.dump(config, f)
        result = policy_settings.probe_recognition_available(p)
        assert result == {"faces": False, "plates": False}

    def test_faces_enabled(self, tmp_path):
        import yaml
        config = {"face_recognition": {"enabled": True}, "lpr": {"enabled": False}}
        p = tmp_path / "config.yml"
        with p.open("w") as f:
            yaml.dump(config, f)
        result = policy_settings.probe_recognition_available(p)
        assert result == {"faces": True, "plates": False}

    def test_missing_config_returns_false(self, tmp_path):
        result = policy_settings.probe_recognition_available(tmp_path / "missing.yml")
        assert result == {"faces": False, "plates": False}


# ── 6. Settings shape ───────────────────────────────────────────────────

class TestSettingsShape:
    def test_default_settings_has_v2_table(self):
        s = policy_settings.default_settings()
        assert set(s["routing_table_v2"]) == {
            "person", "vehicle", "animal", "thing", "package", "bin", "opening",
        }
        assert set(s["outcomes"]) == set(s["routing_table_v2"])

    def test_default_settings_has_recognition(self):
        s = policy_settings.default_settings()
        assert s["recognition"] == {"known_person": "relax_one", "known_vehicle": "relax_one"}

    def test_validate_v2_table_rejects_bad_level(self):
        s = policy_settings.default_settings()
        s["routing_table_v2"]["person"]["doors"] = "scream"
        errors = policy_settings.validate_settings(s)
        assert any("routing_table_v2.person.doors" in e for e in errors)

    def test_validate_recognition_rejects_bad_mode(self):
        s = policy_settings.default_settings()
        s["recognition"]["known_person"] = "yolo"
        errors = policy_settings.validate_settings(s)
        assert any("recognition.known_person" in e for e in errors)

    def test_normalize_fills_missing_v2_fields(self):
        data = {"routing_table": dict(policy_settings.DEFAULT_ROUTING_TABLE)}
        result = policy_settings.normalize_settings(data)
        assert "routing_table_v2" in result
        assert "recognition" in result

    def test_zone_overrides_accept_v2_subjects(self):
        s = policy_settings.default_settings()
        s["zone_overrides"] = {"pool": {"person": "urgent", "vehicle": "notify"}}
        errors = policy_settings.validate_settings(s)
        assert errors == []
