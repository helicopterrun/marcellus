"""Attention ladder v3 (`docs/ladder-v3.md`): the spec's own unit tests (§8).

Settings schema + derivation + legacy translation (§2), the v2 -> v3
migration incl. the owner's file (§3.3), the engine order (§4), delivery per
level incl. the Glance fallback and the ephemeral rule (§5), the
registration default (§5.4) and the capability flag (§2.7). The silence /
overrides translation lives in `test_push_status_and_silence.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import ladder_policy, live_activities, policy_settings, store
from marcellus.push.cards import Card
from marcellus.push.delivery import (
    _device_eligible,
    advance_card,
    build_card_payload,
    send_card_mutation,
    should_push,
)
from marcellus.push.ladder import Snapshot, evaluate_ladder_explained
from marcellus.push.models import Device
from marcellus.push.transport import LogTransport
from marcellus.server import create_app

PS = policy_settings


@pytest.fixture(autouse=True)
def _reset_policy():
    PS.reset_for_tests()
    yield
    PS.reset_for_tests()


def _v3_doc() -> dict:
    return PS.default_settings()


def _legacy_doc() -> dict:
    """What an older (v2) client PUTs: no `ladder`."""
    doc = PS.default_settings()
    del doc["ladder"]
    return doc


# -- §2.1 schema validation --------------------------------------------------


class TestLadderValidation:
    def test_defaults_are_valid(self):
        assert PS.validate_settings(_v3_doc()) == []

    def test_core_row_must_be_complete(self):
        doc = _v3_doc()
        del doc["ladder"]["vehicle"]["doors"]
        errors = PS.validate_settings(doc)
        assert any("ladder.vehicle.doors is required" in e for e in errors)

    def test_core_row_must_be_present(self):
        doc = _v3_doc()
        del doc["ladder"]["thing"]
        assert any("ladder.thing" in e for e in PS.validate_settings(doc))

    def test_extra_rows_are_optional(self):
        doc = _v3_doc()
        for subject in PS.SUBJECTS_V3_EXTRA:
            del doc["ladder"][subject]
        assert PS.validate_settings(doc) == []

    @pytest.mark.parametrize(
        "mutate,needle",
        [
            (lambda d: d["ladder"].__setitem__("drone", d["ladder"]["thing"]), "unknown subject"),
            (lambda d: d["ladder"]["person"].__setitem__("roof", "ignore"), "unknown place"),
            (lambda d: d["ladder"]["person"].__setitem__("doors", "notify"), "must be one of"),
            (lambda d: d["ladder"]["person"].__setitem__("doors", "quiet"), "must be one of"),
        ],
    )
    def test_unknown_subject_place_or_value_is_an_error(self, mutate, needle):
        doc = _v3_doc()
        mutate(doc)
        assert any(needle in e for e in PS.validate_settings(doc))

    def test_ladder_body_ignores_stale_legacy_keys(self):
        doc = _v3_doc()
        doc["outcomes"]["person"]["doors"] = "bogus"
        assert PS.validate_settings(doc) == []
        assert PS.normalize_settings(doc)["outcomes"]["person"]["doors"] == "glance"


# -- §2.2 derivation ---------------------------------------------------------


class TestDerivation:
    def test_each_level_derives_old_words_and_levels(self):
        doc = _v3_doc()
        doc["ladder"]["vehicle"] = {
            "street": "ignore", "yard": "silent", "doors": "glance", "off_limits": "urgent",
        }
        merged = PS.normalize_settings(doc)
        assert merged["outcomes"]["vehicle"] == {
            "street": "off", "yard": "notify", "doors": "glance", "private": "notify",
            "off_limits": "alarm",
        }
        assert merged["routing_table_v2"]["vehicle"] == {
            "street": "log", "yard": "notify", "doors": "quiet", "private": "notify",
            "off_limits": "urgent",
        }

    def test_ignore_is_log_for_person_off_for_everything_else(self):
        merged = PS.normalize_settings(_v3_doc())
        assert merged["outcomes"]["person"]["street"] == "log"
        for subject in ("vehicle", "animal", "thing", "package", "opening"):
            assert merged["outcomes"][subject]["street"] == "off"

    def test_legacy_routing_table_is_left_as_stored(self):
        doc = _v3_doc()
        doc["routing_table"]["stranger"]["doors"] = "urgent"
        assert PS.normalize_settings(doc)["routing_table"]["stranger"]["doors"] == "urgent"

    def test_version_is_3(self):
        assert PS.normalize_settings(_v3_doc())["v"] == 3


# -- §2.3 legacy PUT translation ---------------------------------------------


class TestLegacyTranslation:
    def test_outcomes_translate_into_ladder(self):
        doc = _legacy_doc()
        doc["outcomes"]["person"] = {
            "street": "off", "yard": "log", "doors": "notify", "private": "log",
            "off_limits": "alarm",
        }
        doc["outcomes"]["vehicle"]["doors"] = "glance"
        merged = PS.normalize_settings(doc)
        assert merged["ladder"]["person"] == {
            "street": "ignore", "yard": "ignore", "doors": "silent", "off_limits": "urgent",
        }
        assert merged["ladder"]["vehicle"]["doors"] == "glance"

    def test_private_merges_into_yard_louder_wins(self):
        doc = _legacy_doc()
        doc["outcomes"]["animal"]["yard"] = "notify"
        doc["outcomes"]["animal"]["private"] = "glance"
        assert PS.normalize_settings(doc)["ladder"]["animal"]["yard"] == "glance"
        doc["outcomes"]["animal"]["yard"] = "alarm"
        assert PS.normalize_settings(doc)["ladder"]["animal"]["yard"] == "urgent"

    def test_routing_table_v2_only_body(self):
        doc = _legacy_doc()
        del doc["outcomes"]
        doc["routing_table_v2"]["person"] = {
            "street": "log", "yard": "quiet", "doors": "notify", "private": "quiet",
            "off_limits": "urgent",
        }
        assert PS.normalize_settings(doc)["ladder"]["person"] == {
            "street": "ignore", "yard": "glance", "doors": "silent", "off_limits": "urgent",
        }

    def test_silent_round_trips_losslessly_through_an_old_client(self):
        doc = _v3_doc()
        doc["ladder"]["package"]["doors"] = "silent"
        served = PS.normalize_settings(doc)
        assert served["outcomes"]["package"]["doors"] == "notify"
        echoed = {k: v for k, v in served.items() if k != "ladder"}
        assert PS.normalize_settings(echoed)["ladder"] == served["ladder"]

    def test_ladder_wins_over_legacy_keys(self):
        doc = _v3_doc()
        doc["outcomes"]["person"]["doors"] = "alarm"
        assert PS.normalize_settings(doc)["ladder"]["person"]["doors"] == "glance"

    def test_omitted_extra_rows_keep_the_stored_row(self):
        stored = _v3_doc()
        stored["ladder"]["bin"]["street"] = "ignore"
        PS.apply_settings(PS.normalize_settings(stored))
        doc = _legacy_doc()
        for subject in PS.SUBJECTS_V3_EXTRA:
            del doc["outcomes"][subject]
            del doc["routing_table_v2"][subject]
        assert PS.normalize_settings(doc)["ladder"]["bin"]["street"] == "ignore"

    def test_zone_overrides_notify_becomes_silent(self):
        doc = _v3_doc()
        doc["zone_overrides"] = {"porch": {"person": "notify", "vehicle": "quiet"}}
        assert PS.validate_settings(doc) == []
        merged = PS.normalize_settings(doc)
        assert merged["zone_overrides"] == {"porch": {"person": "silent", "vehicle": "quiet"}}

    def test_zone_overrides_still_reject_off(self):
        doc = _v3_doc()
        doc["zone_overrides"] = {"porch": {"person": "off"}}
        assert PS.validate_settings(doc)

    def test_zone_classes_private_reads_as_yard(self):
        doc = _v3_doc()
        doc["zone_classes"] = {"garden": "private"}
        assert PS.validate_settings(doc) == []
        assert PS.normalize_settings(doc)["zone_classes"] == {"garden": "yard"}

    def test_guess_zone_class_back_of_house_is_yard(self):
        for name in ("back_walkway", "side_gate_area", "rear", "alley", "fence_line"):
            if "gate" in name:
                continue  # doors wins for gates
            assert PS.guess_zone_class(name) == "yard"

    def test_quiet_hours_cap_quiet_is_stored_as_hold_urgent(self):
        doc = _v3_doc()
        doc["quiet_hours"] = {"start": "22:00", "end": "07:00", "mode": "cap_quiet"}
        assert PS.validate_settings(doc) == []
        assert PS.normalize_settings(doc)["quiet_hours"]["mode"] == "hold_urgent"
        assert PS.is_quiet_hours(doc, 23 * 60) == (True, "hold_urgent")

    def test_unknown_live_activity_keys_are_preserved(self):
        doc = _v3_doc()
        doc["live_activities"]["summary_row"] = True
        assert PS.normalize_settings(doc)["live_activities"]["summary_row"] is True


# -- §3 migration ------------------------------------------------------------


def _owner_file() -> dict:
    """§3.3: the owner's stored v2 document."""
    loud = {"street": "glance", "yard": "glance", "doors": "glance", "private": "glance",
            "off_limits": "glance"}
    quiet_street = dict(loud, street="log")
    off = {p: "off" for p in loud}
    outcomes = {
        "person": dict(quiet_street), "animal": dict(quiet_street),
        "package": dict(quiet_street),
        "vehicle": dict(off), "thing": dict(off), "bin": dict(off), "opening": dict(off),
    }
    table_v2 = {
        s: {p: PS.OUTCOME_TO_LEVEL[o] for p, o in row.items()} for s, row in outcomes.items()
    }
    return {
        "v": 2,
        "rev": 7,
        "outcomes": outcomes,
        "routing_table_v2": table_v2,
        "zone_classes": {"garden": "private", "front_door": "doors"},
        "zone_overrides": {"back_walkway": {"person": "quiet"},
                           "parking_area": {"person": "quiet"}},
        "quiet_hours": {"start": "08:00", "end": "22:00", "mode": "cap_quiet"},
        "live_activities": {"delivery": "la_first", "la_only": False, "summary_row": True},
        "mute_sounds": True,
        "camera_optics": {},
        "zone_names": {"garden": "the garden"},
    }


class TestMigration:
    def test_owner_file_expected_result(self, tmp_path: Path):
        path = tmp_path / "push_settings.json"
        path.write_text(json.dumps(_owner_file()))
        settings = PS.startup(path)

        ig, gl = "ignore", "glance"
        for subject in ("person", "animal", "package"):
            assert settings["ladder"][subject] == {
                "street": ig, "yard": gl, "doors": gl, "off_limits": gl,
            }, subject
        for subject in ("vehicle", "thing", "bin", "opening"):
            assert set(settings["ladder"][subject].values()) == {ig}, subject
        assert settings["zone_classes"] == {"garden": "yard", "front_door": "doors"}
        assert settings["zone_overrides"] == {
            "back_walkway": {"person": "quiet"}, "parking_area": {"person": "quiet"},
        }
        assert settings["quiet_hours"] == {"start": "08:00", "end": "22:00",
                                           "mode": "hold_urgent"}
        assert settings["v"] == 3
        # Everything else carried over.
        assert settings["zone_names"] == {"garden": "the garden"}
        assert settings["live_activities"]["summary_row"] is True
        assert settings["mute_sounds"] is True

        on_disk = json.loads(path.read_text())
        assert on_disk["v"] == 3
        assert on_disk["ladder"] == settings["ladder"]
        # Applied: a vehicle anywhere is an off cell; a person in the garden
        # (yard) is Glance.
        assert ("vehicle", "yard") in ladder_policy.OFF_CELLS
        assert ladder_policy.TABLE["person"]["yard"] == "quiet"

    def test_backup_is_the_untouched_v2_file(self, tmp_path: Path):
        path = tmp_path / "push_settings.json"
        original = json.dumps(_owner_file())
        path.write_text(original)
        PS.startup(path)
        backup = tmp_path / "push_settings.json.v2.bak"
        assert backup.read_text() == original

    def test_existing_backup_is_never_overwritten(self, tmp_path: Path):
        path = tmp_path / "push_settings.json"
        path.write_text(json.dumps(_owner_file()))
        backup = tmp_path / "push_settings.json.v2.bak"
        backup.write_text("older backup")
        PS.startup(path)
        assert backup.read_text() == "older backup"

    def test_idempotent_a_v3_file_is_left_alone(self, tmp_path: Path):
        path = tmp_path / "push_settings.json"
        path.write_text(json.dumps(_owner_file()))
        first = PS.startup(path)
        after_first = path.read_text()
        (tmp_path / "push_settings.json.v2.bak").unlink()

        PS.reset_for_tests()
        second = PS.startup(path)
        assert path.read_text() == after_first
        assert second["ladder"] == first["ladder"]
        assert not (tmp_path / "push_settings.json.v2.bak").exists()

    def test_user_edit_after_migration_survives_restart(self, tmp_path: Path):
        path = tmp_path / "push_settings.json"
        path.write_text(json.dumps(_owner_file()))
        PS.startup(path)
        doc = json.loads(path.read_text())
        doc["ladder"]["vehicle"]["doors"] = "urgent"
        path.write_text(json.dumps(doc))
        PS.reset_for_tests()
        assert PS.startup(path)["ladder"]["vehicle"]["doors"] == "urgent"


# -- §4 engine ---------------------------------------------------------------


def _apply(ladder_overrides: dict | None = None, zone_overrides: dict | None = None) -> None:
    doc = _v3_doc()
    for subject, row in (ladder_overrides or {}).items():
        doc["ladder"][subject].update(row)
    doc["zone_overrides"] = zone_overrides or {}
    PS.apply_settings(PS.normalize_settings(doc))


def _eval(**kw):
    return evaluate_ladder_explained(Snapshot(**kw))


class TestEngine:
    def test_levels_order(self):
        assert ladder_policy.LEVELS == ("log", "silent", "quiet", "urgent")

    def test_non_person_ignore_is_suppressed_and_never_escalates(self):
        _apply()
        r = _eval(subject="vehicle", place="street", night=True, nobody_home=True)
        assert (r.level, r.stage) == ("suppressed", "off_cell")

    def test_person_ignore_is_log_and_nudgeable(self):
        _apply()
        assert _eval(subject="person", place="street").level == "log"
        assert _eval(subject="person", place="street", night=True).level == "silent"

    def test_worry_nudge_stops_at_glance(self):
        _apply()
        r = _eval(subject="person", place="yard", night=True, nobody_home=True)
        assert r.level == "quiet"
        assert "nudge_up" not in r.modifiers

    def test_silent_cell_nudges_up_to_glance(self):
        _apply({"person": {"doors": "silent"}})
        assert _eval(subject="person", place="doors", night=True).level == "quiet"

    def test_urgent_cell_stays_urgent_on_worry_and_calm_lowers_it(self):
        _apply()
        assert _eval(subject="person", place="off_limits", night=True).level == "urgent"
        r = _eval(subject="person", place="off_limits", known_role=True)
        assert r.level == "quiet" and "nudge_down" in r.modifiers

    def test_child_hazard_floor_is_glance(self):
        _apply({"person": {"yard": "ignore"}})
        r = _eval(subject="person", place="yard", child_hazard_zone=True)
        assert r.level == "quiet" and "child_hazard_floor" in r.modifiers

    def test_zone_override_returns_directly(self):
        _apply(zone_overrides={"porch": {"person": "silent"}})
        r = _eval(subject="person", place="off_limits", zone="porch", night=True)
        assert (r.level, r.stage) == ("silent", "zone_override")

    def test_legacy_notify_zone_override_reads_as_silent(self):
        ladder_policy.set_zone_overrides({"porch": {"person": "notify"}})
        try:
            assert _eval(subject="person", place="yard", zone="porch").level == "silent"
        finally:
            ladder_policy.set_zone_overrides({})

    def test_street_cap_still_quiet(self):
        _apply({"person": {"street": "urgent"}})
        r = _eval(subject="person", place="street")
        assert r.level == "quiet" and "street_cap" in r.modifiers

    def test_private_place_reads_as_yard(self):
        _apply({"animal": {"yard": "urgent"}})
        assert _eval(subject="animal", place="private").level == "urgent"


class TestHoldUrgent:
    def test_urgent_is_held_at_glance(self):
        _apply({"person": {"doors": "urgent"}})
        r = _eval(subject="person", place="doors", quiet_hours_hold=True)
        assert r.level == "quiet"
        assert r.modifiers[-1] == "quiet_hours_hold"

    def test_off_limits_is_never_held(self):
        _apply()
        assert _eval(subject="person", place="off_limits", quiet_hours_hold=True).level == "urgent"

    def test_safety_is_never_held(self):
        _apply()
        r = _eval(subject="person", place="yard", ai_flagged=True, quiet_hours_hold=True)
        assert (r.level, r.stage) == ("urgent", "safety")

    def test_applies_after_caps_and_to_zone_overrides(self):
        _apply(zone_overrides={"porch": {"person": "urgent"}})
        r = _eval(subject="person", place="doors", zone="porch", quiet_hours_hold=True)
        assert (r.level, r.stage) == ("quiet", "zone_override")
        assert "quiet_hours_hold" in r.modifiers

    def test_lower_levels_untouched(self):
        _apply({"person": {"doors": "silent"}})
        r = _eval(subject="person", place="doors", quiet_hours_hold=True)
        assert r.level == "silent" and "quiet_hours_hold" not in r.modifiers


# -- §5 delivery -------------------------------------------------------------


def _card(level: str, *, peak: str | None = None, zone_hit: bool = False) -> Card:
    return Card(
        card_key="cam:person:t1", level=level, peak_level=peak or level,
        created_at=0.0, updated_at=0.0, state_since_at=0.0, zone_override_hit=zone_hit,
    )


def _payload(card: Card, mutation: str = "create", sound: bool = False) -> dict:
    return build_card_payload(
        card, mutation, sound=sound, subject_kind="person", place_class="doors",
        label="person", camera="cam", zone_name="front_door", glyph="person.detected",
        primary="Person", secondary="at the door", event_ts=0.0,
    )


def _dev(token: str, *, la: bool) -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="b", environment="sandbox",
        min_severity="detection", push_to_start_token="pts" if la else "",
    )


class TestDeliveryPerLevel:
    def test_pushable_and_sounded_sets(self):
        assert [lvl for lvl in ladder_policy.LEVELS if should_push(lvl)] == ["silent", "urgent"]
        card, _m, sound = advance_card(None, "silent", card_key="k", now=0.0)
        assert sound is False
        card, _m, sound = advance_card(None, "urgent", card_key="k", now=0.0)
        assert sound is True

    def test_silent_card_is_passive_with_card_silent_category(self):
        payload = _payload(_card("silent"))
        assert payload["aps"]["interruption-level"] == "passive"
        assert payload["aps"]["category"] == "card.silent"
        assert "sound" not in payload["aps"]

    def test_urgent_card_is_time_sensitive(self):
        payload = _payload(_card("urgent"), sound=True)
        assert payload["aps"]["interruption-level"] == "time-sensitive"
        assert payload["aps"]["category"] == "card.urgent"
        assert payload["aps"]["sound"]

    def test_silent_never_starts_a_live_activity(self):
        for subject, place in (("person", "doors"), ("person", "off_limits"),
                               ("package", "yard")):
            assert live_activities.classify_family(
                subject_kind=subject, label=subject, place_class=place, level="silent",
            ) is None

    def test_person_family_starts_at_glance_or_urgent(self):
        for level in ("quiet", "urgent"):
            assert live_activities.classify_family(
                subject_kind="person", label="person", place_class="doors", level=level,
            ) == live_activities.PERSON

    def test_relevance_and_content_state_level(self):
        state = live_activities.build_content_state(
            level="silent", mutation="resolve", glyph="g", primary="p", secondary="",
            elapsed_seconds=0, card_key="k", thumbnail_handle=None, thumbnail_revision=1,
        )
        assert state["level"] == "log"
        assert live_activities._RELEVANCE_SCORE == {"urgent": 1.0, "quiet": 0.5, "log": 0.25}

    def test_severity_eligibility(self):
        detection = _dev("a", la=True)
        alert = Device(apns_token="b", device_id="d_b", bundle_id="b",
                       environment="sandbox", min_severity="alert")
        for level in ("silent", "quiet"):
            assert _device_eligible(detection, camera="c", labels=(), card_level=level)
            assert not _device_eligible(alert, camera="c", labels=(), card_level=level)
        assert _device_eligible(alert, camera="c", labels=(), card_level="urgent")


async def _send(tmp_path: Path, card: Card, mutation: str, devices: list[Device]):
    conn = db.open_sidecar(tmp_path / "marcellus.db")
    transport = LogTransport()
    fallback: list[str] = []
    try:
        await send_card_mutation(
            conn, transport, devices, card, mutation, _payload(card, mutation),
            camera="cam", subject_kind="person", now=0.0, fallback_out=fallback,
        )
    finally:
        conn.close()
    return {s["device_id"]: s["payload"] for s in transport.sent}, fallback


class TestGlanceDelivery:
    @pytest.mark.asyncio
    async def test_glance_pushes_no_card_to_la_capable_devices(self, sidecar_db_path, tmp_path):
        sent, fallback = await _send(tmp_path, _card("quiet"), "create", [_dev("a", la=True)])
        assert sent == {} and fallback == []

    @pytest.mark.asyncio
    async def test_glance_falls_back_to_a_silent_card(self, sidecar_db_path, tmp_path):
        mac = _dev("mac", la=False)
        mac_no_la = Device(apns_token="m2", device_id="d_m2", bundle_id="b",
                           environment="sandbox", min_severity="detection",
                           push_to_start_token="pts", la_capable=False)
        sent, fallback = await _send(
            tmp_path, _card("quiet"), "create", [_dev("a", la=True), mac, mac_no_la],
        )
        assert set(sent) == {"d_mac", "d_m2"}
        assert sorted(fallback) == ["d_m2", "d_mac"]
        for payload in sent.values():
            assert payload["level"] == "silent"
            assert payload["aps"]["category"] == "card.silent"
            assert payload["aps"]["interruption-level"] == "passive"
            assert "sound" not in payload["aps"]

    @pytest.mark.asyncio
    async def test_glance_fallback_resolve_is_kept(self, sidecar_db_path, tmp_path):
        sent, _ = await _send(
            tmp_path, _card("quiet"), "resolve", [_dev("a", la=True), _dev("mac", la=False)],
        )
        assert sent["d_a"]["ephemeral"] is True
        assert sent["d_mac"]["ephemeral"] is False
        assert sent["d_mac"]["aps"]["category"] == "card.silent"

    @pytest.mark.asyncio
    async def test_silent_card_goes_to_every_device(self, sidecar_db_path, tmp_path):
        sent, fallback = await _send(
            tmp_path, _card("silent"), "create", [_dev("a", la=True), _dev("mac", la=False)],
        )
        assert set(sent) == {"d_a", "d_mac"} and fallback == []


class TestEphemeralRule:
    @pytest.mark.parametrize(
        "peak,zone_hit,ephemeral",
        [
            ("log", False, True),
            ("quiet", False, True),
            ("silent", False, False),
            ("urgent", False, False),
            ("quiet", True, False),
        ],
    )
    @pytest.mark.asyncio
    async def test_resolve_ephemeral(self, sidecar_db_path, tmp_path, peak, zone_hit, ephemeral):
        card = _card(peak, peak=peak, zone_hit=zone_hit)
        sent, _ = await _send(tmp_path, card, "resolve", [_dev("a", la=True)])
        if peak == "log":
            assert sent == {}  # a log card is below every device's severity
            return
        assert sent["d_a"]["ephemeral"] is ephemeral


# -- §5.4 registration default, §2.7 capability ------------------------------


def _client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        push=PushSection(enabled=False, push_settings_path=str(tmp_path / "push_settings.json")),
    )
    return TestClient(create_app(settings))


def test_registration_default_min_severity_is_detection(
    frigate_db_path, sidecar_db_path, tmp_path
):
    with _client(frigate_db_path, sidecar_db_path, tmp_path) as client:
        r = client.put(
            "/v1/push/devices/tok-default",
            json={"bundle_id": "com.pondhouse.Elsinore", "environment": "sandbox"},
        )
        assert r.status_code == 200, r.text
    conn = db.open_sidecar(sidecar_db_path)
    try:
        device = store.get_device(conn, "tok-default")
    finally:
        conn.close()
    assert device is not None and device.min_severity == "detection"


def test_capabilities_advertise_ladder_version_3(frigate_db_path, sidecar_db_path, tmp_path):
    with _client(frigate_db_path, sidecar_db_path, tmp_path) as client:
        body = client.get("/v1/capabilities").json()
    assert body["push"]["ladder_version"] == 3


def test_settings_get_serves_ladder_and_derived_views(frigate_db_path, sidecar_db_path, tmp_path):
    with _client(frigate_db_path, sidecar_db_path, tmp_path) as client:
        doc = client.get("/v1/push/settings").json()["settings"]
        assert doc["v"] == 3
        assert set(doc["ladder"]) == set(PS.SUBJECTS_V3)
        assert set(doc["outcomes"]["person"]) == set(PS.LEGACY_PLACES)
        bad = dict(doc, ladder={**doc["ladder"], "person": {"street": "loud"}})
        r = client.put("/v1/push/settings", json=bad)
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_settings"


# -- §2.1 default: the Balanced preset, and every fallback agrees ------------


BALANCED = {
    "person": ("ignore", "glance", "glance", "urgent"),
    "vehicle": ("ignore", "silent", "silent", "glance"),
    "animal": ("ignore", "silent", "silent", "glance"),
    "package": ("ignore", "silent", "glance", "glance"),
    "opening": ("ignore", "silent", "glance", "urgent"),
    "bin": ("ignore", "ignore", "silent", "silent"),
    "thing": ("ignore", "ignore", "silent", "silent"),
}


def test_default_ladder_is_the_balanced_preset():
    ladder = PS.default_settings()["ladder"]
    assert {s: tuple(row[p] for p in PS.PLACES) for s, row in ladder.items()} == BALANCED


def test_builtin_engine_table_and_off_cells_agree_with_the_default():
    table, off = PS.engine_table(PS.default_settings())
    for subject, row in table.items():
        assert ladder_policy.TABLE[subject] == row, subject
    assert ladder_policy.TABLE["stranger"] == table["person"]
    assert off == set(ladder_policy.DEFAULT_OFF_CELLS)


def test_legacy_default_constants_agree_with_the_default():
    derived_outcomes, derived_v2 = PS.derive_legacy_views(PS.default_settings()["ladder"])
    for subject in PS.SUBJECTS_V3_EXTRA:
        assert PS.DEFAULT_EXTRA_OUTCOMES[subject] == derived_outcomes[subject]
    for subject in PS.SUBJECTS_V2:
        assert PS.DEFAULT_ROUTING_TABLE_V2[subject] == derived_v2[subject]
    assert PS.DEFAULT_ROUTING_TABLE["stranger"] == derived_v2["person"]
