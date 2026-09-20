"""Tests for routing v2 — observable subjects, identity as modifier.

Covers:
1. Create-time routing ignores sub_label
2. Recognition deescalates silently
3. Unrecognized result is no-op
4. Off disables the modifier
5. Migration derivation
6. recognition_available reflects probe
7. Replay scenarios route sensibly under migrated table
"""
from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import card_store, ladder_policy, policy_settings
from marcellus.push.delivery_wire import (
    classify_subject,
    handle_delivery_event,
    handle_recognition_event,
)
from marcellus.push.ladder import Snapshot, evaluate_ladder
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport


def _device(token="tok1"):
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox",
    )


def _event(camera, track_id, label, *, zones=(), sub_labels=()):
    return ReviewEvent(
        review_id=f"r_{track_id}",
        camera=camera,
        severity="alert",
        labels=(label,),
        track_ids=(track_id,),
        zones=zones,
        sub_labels=sub_labels,
    )


# ── 1. Create-time routing ignores sub_label ───────────────────────────


def test_classify_subject_person_ignores_sub_label():
    ev = _event("cam", "t1", "person", sub_labels=("Sarah",))
    assert classify_subject(ev) == "person"


def test_classify_subject_car_returns_vehicle():
    ev = _event("cam", "t1", "car")
    assert classify_subject(ev) == "vehicle"


def test_classify_subject_dog_returns_animal():
    ev = _event("cam", "t1", "dog")
    assert classify_subject(ev) == "animal"


def test_classify_subject_v3_labels_get_their_own_subjects():
    # One alerts stack (2026-08-20): the labels that used to pick an LA
    # family off a `thing` card are subjects now.
    assert classify_subject(_event("cam", "t1", "package")) == "package"
    assert classify_subject(_event("cam", "t1", "waste_bin")) == "bin"
    assert classify_subject(_event("cam", "t1", "garage")) == "opening"
    # Unclaimed labels still fall back to `thing`.
    assert classify_subject(_event("cam", "t1", "umbrella")) == "thing"


@pytest.mark.asyncio
async def test_create_card_uses_person_not_known_when_sub_label_present(sidecar_db_path: Path):
    """sub_label at create time does NOT change the subject to 'known'."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)

    policy_settings.reset_for_tests()

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",), sub_labels=("Sarah",)),
        conn=conn, devices=[_device()], transport=transport, config=config, now=0.0,
    )

    rows = conn.execute("SELECT card_key FROM push_cards").fetchall()
    assert len(rows) == 1
    assert rows[0]["card_key"] == "doorbell:person:trk1"


# ── 2. Recognition deescalates silently ────────────────────────────────


@pytest.mark.asyncio
async def test_recognition_deescalates_notify_to_quiet(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["recognition"] = {"known_person": "relax_one", "known_vehicle": "relax_one"}
    policy_settings.apply_settings(settings)

    # Create a person card at notify (person + doors)
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[_device()], transport=transport, config=config, now=0.0,
    )
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card is not None
    assert card.level == "notify"

    transport.sent.clear()

    # Recognition arrives
    count = await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[_device()], transport=transport,
        config=config, label="person", now=10.0,
    )
    assert count == 1
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card.level == "quiet"

    # Card push is silent (no sound, passive interruption)
    card_pushes = [s for s in transport.sent if s.get("type") == "card"]
    for push in card_pushes:
        aps = push["payload"]["aps"]
        assert "sound" not in aps


@pytest.mark.asyncio
async def test_recognition_relax_to_quiet_from_urgent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["recognition"] = {"known_person": "relax_to_quiet", "known_vehicle": "off"}
    policy_settings.apply_settings(settings)

    # Create urgent card (person + off_limits)
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[_device()], transport=transport, config=config, now=0.0,
    )
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card.level == "urgent"

    transport.sent.clear()

    count = await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[_device()], transport=transport,
        config=config, label="person", now=10.0,
    )
    assert count == 1
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card.level == "quiet"


# ── 3. Unrecognized result is no-op ───────────────────────────────────


@pytest.mark.asyncio
async def test_recognition_no_sub_label_is_noop(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["recognition"] = {"known_person": "relax_one", "known_vehicle": "relax_one"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[_device()], transport=transport, config=config, now=0.0,
    )

    count = await handle_recognition_event(
        "doorbell", "trk1", "",
        conn=conn, devices=[_device()], transport=transport,
        config=config, label="person", now=10.0,
    )
    assert count == 0
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card.level == "notify"


# ── 4. Off disables the modifier ──────────────────────────────────────


@pytest.mark.asyncio
async def test_recognition_off_is_noop(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["recognition"] = {"known_person": "off", "known_vehicle": "off"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[_device()], transport=transport, config=config, now=0.0,
    )

    count = await handle_recognition_event(
        "doorbell", "trk1", "Sarah",
        conn=conn, devices=[_device()], transport=transport,
        config=config, label="person", now=10.0,
    )
    assert count == 0
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card.level == "notify"


# ── 5. Migration derivation ──────────────────────────────────────────


def test_migrate_v1_to_v2_person_inherits_stranger():
    v1 = {
        "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
         "private": "notify", "off_limits": "urgent"},
        "known": {"street": "log", "yard": "log", "doors": "quiet",
         "private": "log", "off_limits": "quiet"},
        "animal": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "log"},
        "thing": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "quiet"},
    }
    v2, recognition, _msg = policy_settings.migrate_v1_to_v2(v1)
    assert v2["person"] == v1["stranger"]
    assert v2["animal"] == v1["animal"]
    assert v2["thing"] == v1["thing"]


def test_migrate_v1_to_v2_vehicle_gets_thing_bumped_at_doors():
    v1 = {
        "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
         "private": "notify", "off_limits": "urgent"},
        "known": {"street": "log", "yard": "log", "doors": "quiet",
         "private": "log", "off_limits": "quiet"},
        "animal": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "log"},
        "thing": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "quiet"},
    }
    v2, _recognition, _msg = policy_settings.migrate_v1_to_v2(v1)
    levels = ladder_policy.LEVELS
    for place in ("doors", "off_limits"):
        thing_idx = levels.index(v1["thing"][place])
        vehicle_idx = levels.index(v2["vehicle"][place])
        assert vehicle_idx == min(thing_idx + 1, len(levels) - 1)


def test_migrate_v1_to_v2_recognition_mode():
    v1 = {
        "stranger": {"street": "log", "yard": "quiet", "doors": "notify",
         "private": "notify", "off_limits": "urgent"},
        "known": {"street": "log", "yard": "log", "doors": "quiet",
         "private": "log", "off_limits": "quiet"},
        "animal": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "log"},
        "thing": {"street": "log", "yard": "log", "doors": "log",
         "private": "log", "off_limits": "quiet"},
    }
    _v2, recognition, _msg = policy_settings.migrate_v1_to_v2(v1)
    assert recognition["known_person"] in ("relax_one", "relax_to_quiet")


# ── 6. recognition_available reflects probe ───────────────────────────


def test_probe_recognition_both_disabled(tmp_path: Path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("""
face_recognition:
  enabled: false
lpr:
  enabled: false
""")
    result = policy_settings.probe_recognition_available(str(config_file))
    assert result == {"faces": False, "plates": False}


def test_probe_recognition_faces_enabled(tmp_path: Path):
    config_file = tmp_path / "config.yml"
    config_file.write_text("""
face_recognition:
  enabled: true
lpr:
  enabled: false
""")
    result = policy_settings.probe_recognition_available(str(config_file))
    assert result == {"faces": True, "plates": False}


def test_probe_recognition_missing_config():
    result = policy_settings.probe_recognition_available("/nonexistent/config.yml")
    assert result == {"faces": False, "plates": False}


# ── 7. Replay scenario: v2 table routes sensibly ─────────────────────


def _apply_v2_defaults():
    settings = policy_settings.default_settings()
    policy_settings.apply_settings(settings)


def test_v2_table_person_at_doors_is_notify():
    _apply_v2_defaults()
    level = evaluate_ladder(Snapshot(subject="person", place="doors"))
    assert level == "notify"


def test_v2_table_vehicle_at_yard_is_quiet():
    _apply_v2_defaults()
    level = evaluate_ladder(Snapshot(subject="vehicle", place="yard"))
    assert level == "quiet"


def test_v2_table_animal_at_off_limits_is_quiet():
    _apply_v2_defaults()
    level = evaluate_ladder(Snapshot(subject="animal", place="off_limits"))
    assert level == "quiet"


def test_v2_table_thing_at_off_limits_is_quiet():
    _apply_v2_defaults()
    level = evaluate_ladder(Snapshot(subject="thing", place="off_limits"))
    assert level == "quiet"


def test_relaxed_level_relax_one_from_notify():
    from marcellus.push.delivery_wire import _relaxed_level
    assert _relaxed_level("notify", "relax_one") == "quiet"


def test_relaxed_level_relax_one_from_log_is_none():
    from marcellus.push.delivery_wire import _relaxed_level
    assert _relaxed_level("log", "relax_one") is None


def test_relaxed_level_relax_to_quiet_from_urgent():
    from marcellus.push.delivery_wire import _relaxed_level
    assert _relaxed_level("urgent", "relax_to_quiet") == "quiet"


def test_relaxed_level_off_returns_none():
    from marcellus.push.delivery_wire import _relaxed_level
    assert _relaxed_level("notify", "off") is None
