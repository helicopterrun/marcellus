"""Unit tests for `push/policy_settings.py` (Elsinore Phase 4): defaults,
validation, the zone-name guessing heuristic, persistence, and the wiring
that applies a settings document to the live routing engine.
"""

from __future__ import annotations

import json
from pathlib import Path

from marcellus.push import ladder_policy, policy_settings


def test_default_routing_table_matches_the_brief_exactly():
    assert policy_settings.default_settings()["routing_table"] == {
        "stranger": {
            "street": "log", "yard": "quiet", "doors": "notify",
            "private": "notify", "off_limits": "urgent",
        },
        "known": {
            "street": "log", "yard": "log", "doors": "quiet",
            "private": "quiet", "off_limits": "quiet",
        },
        "animal": {
            "street": "log", "yard": "quiet", "doors": "quiet",
            "private": "quiet", "off_limits": "quiet",
        },
        "thing": {
            "street": "log", "yard": "log", "doors": "log",
            "private": "log", "off_limits": "quiet",
        },
    }


def test_default_settings_shape():
    settings = policy_settings.default_settings()
    assert settings["v"] == policy_settings.SETTINGS_VERSION
    assert settings["zone_classes"] == {}
    assert settings["zone_overrides"] == {}
    assert settings["live_activities"] == {
        "package": True, "bins": True, "openings": True, "person": True,
        "person_restricted": True, "opening_picks": [],
        "delivery": "la_first", "alert_all_changes": False, "la_only": False,
    }
    assert settings["mute_sounds"] is True
    assert settings["quiet_hours"] is None


# -- validation ---------------------------------------------------------


def _valid() -> dict:
    return policy_settings.default_settings()


def test_valid_default_settings_pass_validation():
    assert policy_settings.validate_settings(_valid()) == []


def test_rejects_invalid_level():
    data = _valid()
    data["routing_table"]["stranger"]["doors"] = "screaming"
    errors = policy_settings.validate_settings(data)
    assert any("routing_table.stranger.doors" in e for e in errors)


def test_rejects_unknown_subject():
    data = _valid()
    data["routing_table"]["ghost"] = {p: "log" for p in policy_settings.PLACES}
    errors = policy_settings.validate_settings(data)
    assert any("unknown subject" in e for e in errors)


def test_rejects_missing_place_in_routing_table():
    data = _valid()
    del data["routing_table"]["stranger"]["doors"]
    errors = policy_settings.validate_settings(data)
    assert any("routing_table.stranger.doors" in e for e in errors)


def test_rejects_invalid_zone_class():
    data = _valid()
    data["zone_classes"] = {"driveway": "not_a_place"}
    errors = policy_settings.validate_settings(data)
    assert any("zone_classes.driveway" in e for e in errors)


def test_rejects_invalid_subject_in_zone_overrides():
    data = _valid()
    data["zone_overrides"] = {"driveway": {"ghost_subject": "urgent"}}
    errors = policy_settings.validate_settings(data)
    assert any("zone_overrides.driveway" in e and "ghost_subject" in e for e in errors)


def test_rejects_invalid_level_in_zone_overrides():
    data = _valid()
    data["zone_overrides"] = {"driveway": {"thing": "screaming"}}
    errors = policy_settings.validate_settings(data)
    assert any("zone_overrides.driveway.thing" in e for e in errors)


def test_zone_overrides_allow_unknown_zone_names():
    # The user may configure a zone before it appears in Frigate.
    data = _valid()
    data["zone_overrides"] = {"not_yet_a_real_zone": {"thing": "urgent"}}
    assert policy_settings.validate_settings(data) == []


def test_tolerates_unknown_live_activity_family():
    # Spec §9 / dual-read rule: unknown keys in live_activities are ignored,
    # never rejected — a newer app must be able to PUT against an older
    # sidecar without the whole settings save failing.
    data = _valid()
    data["live_activities"]["robots"] = True
    errors = policy_settings.validate_settings(data)
    assert not errors


def test_rejects_non_bool_family_toggle():
    data = _valid()
    data["live_activities"]["package"] = "yes"
    errors = policy_settings.validate_settings(data)
    assert any("live_activities.package" in e for e in errors)


def test_unknown_top_level_field_is_ignored():
    data = _valid()
    data["some_future_field"] = {"whatever": True}
    assert policy_settings.validate_settings(data) == []


# -- zone-name guessing heuristic ----------------------------------------


def test_guess_street_patterns():
    for name in ("nw_49th_street", "county_road", "sidewalk", "curbside", "highway_view"):
        assert policy_settings.guess_zone_class(name) == "street"


def test_guess_yard_patterns():
    for name in ("driveway", "front_porch", "garden_path", "front_yard", "parking_lot"):
        assert policy_settings.guess_zone_class(name) == "yard"


def test_guess_doors_patterns():
    for name in ("front_door", "side_gate", "main_entry", "back_entrance", "kitchen_window"):
        assert policy_settings.guess_zone_class(name) == "doors"


def test_guess_private_patterns():
    for name in ("backyard", "side_yard", "rear_lot", "the_alley", "fence_line"):
        assert policy_settings.guess_zone_class(name) == "private"


def test_guess_street_pattern_wins_over_the_coincidental_private_substring():
    # "sidewalk" is a literal street pattern; it also happens to contain
    # "side" (a private pattern) as a substring -- street must win.
    assert policy_settings.guess_zone_class("sidewalk") == "street"


def test_guess_off_limits_patterns():
    for name in ("pool_area", "garden_shed", "equipment_room", "restricted_zone"):
        assert policy_settings.guess_zone_class(name) == "off_limits"


def test_guess_defaults_to_yard_for_unrecognized_name():
    assert policy_settings.guess_zone_class("zone_47") == "yard"


def test_guess_prefers_specific_pattern_over_broad_one():
    # Contains both a yard hint ("front") and a doors hint ("entry") --
    # doors wins (design doc's own example).
    assert policy_settings.guess_zone_class("front_entry_person") == "doors"


def test_guess_falls_back_to_camera_name():
    assert policy_settings.guess_zone_class("nw_49th_st", cameras=("street",)) == "street"
    assert policy_settings.guess_zone_class("zone_47", cameras=("driveway",)) == "yard"


# -- persistence ----------------------------------------------------------


def test_load_settings_returns_defaults_when_file_absent(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    assert policy_settings.load_settings(path) == policy_settings.default_settings()


def test_load_settings_returns_defaults_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    path.write_text("{not json")
    assert policy_settings.load_settings(path) == policy_settings.default_settings()


def test_save_then_load_round_trips(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    data = policy_settings.default_settings()
    data["zone_classes"]["driveway"] = "yard"
    data["routing_table"]["thing"]["doors"] = "quiet"
    policy_settings.save_settings(path, data)

    loaded = policy_settings.load_settings(path)
    assert loaded["zone_classes"] == {"driveway": "yard"}
    assert loaded["routing_table"]["thing"]["doors"] == "quiet"


def test_load_settings_fills_in_missing_fields_from_an_older_partial_file(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    path.write_text('{"v": 1, "zone_classes": {"driveway": "yard"}}')
    loaded = policy_settings.load_settings(path)
    assert loaded["routing_table"] == policy_settings.DEFAULT_ROUTING_TABLE
    assert loaded["zone_classes"] == {"driveway": "yard"}
    assert loaded["zone_overrides"] == {}
    assert loaded["live_activities"]["package"] is True


def test_normalize_settings_keeps_valid_zone_overrides():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {"front_entry_person": {"thing": "notify"}}


def test_normalize_settings_drops_invalid_entries_within_a_zone_override():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {
        "driveway": {"animal": "log", "ghost": "urgent", "thing": "not_a_level"},
    }
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {"driveway": {"animal": "log"}}


def test_normalize_settings_removes_empty_inner_dicts():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"driveway": {}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {}


def test_normalize_settings_removes_zone_left_empty_after_filtering():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"driveway": {"ghost": "urgent"}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {}


# -- applying to the routing engine ----------------------------------------


def test_apply_settings_changes_what_the_ladder_evaluates_against():
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    custom = policy_settings.default_settings()
    table_key = "routing_table_v2" if "routing_table_v2" in custom else "routing_table"
    custom[table_key]["thing"]["yard"] = "urgent"
    policy_settings.apply_settings(custom)

    assert ladder_policy.TABLE["thing"]["yard"] == "urgent"
    level = evaluate_ladder(Snapshot(subject="thing", place="yard"))
    assert level == "urgent"


def test_apply_settings_copies_the_table_so_later_mutation_does_not_leak():
    custom = policy_settings.default_settings()
    policy_settings.apply_settings(custom)
    table_key = "routing_table_v2" if "routing_table_v2" in custom else "routing_table"
    custom[table_key]["thing"]["yard"] = "urgent"
    assert ladder_policy.TABLE["thing"]["yard"] != "urgent"


def test_get_active_lazily_defaults():
    policy_settings.reset_for_tests()
    active = policy_settings.get_active()
    assert active == policy_settings.default_settings()


# -- zone overrides (Phase 4 addendum) -------------------------------------


def test_zone_override_present_bypasses_the_base_table():
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["routing_table"]["thing"]["doors"] = "log"  # base table says log
    settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    policy_settings.apply_settings(settings)

    level = evaluate_ladder(
        Snapshot(subject="thing", place="doors", zone="front_entry_person")
    )
    assert level == "notify"  # the override, not the base table's "log"


def test_zone_override_absent_falls_through_to_the_base_table():
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    policy_settings.apply_settings(settings)

    # Different zone -- no override applies, ordinary table lookup runs.
    level = evaluate_ladder(Snapshot(subject="thing", place="doors", zone="side_door"))
    rt = settings.get("routing_table_v2") or settings["routing_table"]
    assert level == rt["thing"]["doors"]

    # Same zone, different subject -- no override applies either.
    level = evaluate_ladder(
        Snapshot(subject="person", place="doors", zone="front_entry_person")
    )
    table_key = "routing_table_v2" if "routing_table_v2" in settings else "routing_table"
    subj_key = "person" if table_key == "routing_table_v2" else "stranger"
    assert level == settings[table_key][subj_key]["doors"]


def test_zone_override_does_not_affect_other_zones_in_the_same_place_class():
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"driveway": {"animal": "log"}}
    policy_settings.apply_settings(settings)

    assert evaluate_ladder(Snapshot(subject="animal", place="yard", zone="driveway")) == "log"
    # Another yard-classified zone, no override for it -- base table applies.
    assert evaluate_ladder(
        Snapshot(subject="animal", place="yard", zone="parking_spot")
    ) == settings["routing_table"]["animal"]["yard"]


def test_validate_rejects_bad_camera_optics():
    base = policy_settings.default_settings()
    base["camera_optics"] = {"cam": {"hfov": 5, "mount_ft": -1, "tilt_deg": 100, "faces": "Q"}}
    errors = policy_settings.validate_settings(base)
    assert any("hfov" in e for e in errors)
    assert any("mount_ft" in e for e in errors)
    assert any("tilt_deg" in e for e in errors)
    assert any("faces" in e for e in errors)


def test_normalize_camera_optics_rounds_and_drops_incomplete():
    doc = policy_settings.default_settings()
    doc["camera_optics"] = {
        "good": {"hfov": 114.96, "mount_ft": 10.04, "tilt_deg": 12, "vfov": 79.44,
                 "faces": "SE", "lens": " dahua-5442-vf ", "junk": "x"},
        "incomplete": {"hfov": 90},
    }
    merged = policy_settings.normalize_settings(doc)
    assert merged["camera_optics"] == {
        "good": {"hfov": 115.0, "mount_ft": 10.0, "tilt_deg": 12.0, "vfov": 79.4,
                 "faces": "SE", "lens": "dahua-5442-vf"},
    }


def test_normalize_floorplan_round_trips_and_clears():
    doc = policy_settings.default_settings()
    doc["floorplan"] = {
        "ext": "png", "w": 1600, "h": 1200, "uploaded_at": "2026-08-16T00:00:00Z",
        "calibration": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.2, "length_ft": 42.04},
    }
    merged = policy_settings.normalize_settings(doc)
    assert merged["floorplan"]["ext"] == "png"
    assert merged["floorplan"]["calibration"]["length_ft"] == 42.0
    cleared = policy_settings.normalize_settings({**doc, "floorplan": None})
    assert cleared["floorplan"] is None


def test_normalize_camera_layout_lock_round_trips():
    doc = policy_settings.default_settings()
    doc["camera_layout"] = {
        "porch": {"x": 0.5, "y": 0.5, "azimuth": 90, "locked": True},
        "drive": {"x": 0.2, "y": 0.2, "locked": False},
    }
    merged = policy_settings.normalize_settings(doc)
    assert merged["camera_layout"]["porch"]["locked"] is True
    assert "locked" not in merged["camera_layout"]["drive"]
    assert not policy_settings.validate_settings(doc)


def test_normalize_floorplan_rotation():
    doc = policy_settings.default_settings()
    doc["floorplan"] = {"ext": "png", "w": 1600, "h": 1200, "rotation_deg": -14.5}
    merged = policy_settings.normalize_settings(doc)
    assert merged["floorplan"]["rotation_deg"] == 345.5  # normalized to 0..360
    doc["floorplan"]["rotation_deg"] = 360  # a full turn is no rotation
    assert "rotation_deg" not in policy_settings.normalize_settings(doc)["floorplan"]
    doc["floorplan"]["rotation_deg"] = 999
    assert policy_settings.validate_settings(doc)
    doc["floorplan"]["rotation_deg"] = 90
    assert not policy_settings.validate_settings(doc)


def test_startup_seeds_camera_optics_when_key_absent(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    settings = policy_settings.startup(path)
    seeded = settings["camera_optics"]
    assert seeded == policy_settings.seeded_camera_optics()
    assert seeded["street"]["mount_ft"] == 35
    # Persisted, so the next startup sees the key and does not re-seed.
    on_disk = json.loads(path.read_text())
    assert on_disk["camera_optics"] == seeded
    policy_settings.reset_for_tests()


def test_startup_never_reseeds_over_user_edits(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    policy_settings.startup(path)
    doc = json.loads(path.read_text())
    doc["camera_optics"] = {"street": {"hfov": 100.0, "mount_ft": 20.0, "tilt_deg": 15.0}}
    path.write_text(json.dumps(doc))
    settings = policy_settings.startup(path)
    assert settings["camera_optics"] == {
        "street": {"hfov": 100.0, "mount_ft": 20.0, "tilt_deg": 15.0},
    }
    # Even an emptied table (every camera deleted) stays empty.
    doc["camera_optics"] = {}
    path.write_text(json.dumps(doc))
    assert policy_settings.startup(path)["camera_optics"] == {}
    policy_settings.reset_for_tests()


# -- V3 seeding migration (one alerts stack, 2026-08-20) ---------------------

def _pre_v3_doc() -> dict:
    """A settings doc as an S1-era sidecar wrote it: four-subject outcomes,
    authoritative family booleans."""
    doc = policy_settings.default_settings()
    for subject in policy_settings.SUBJECTS_V3_EXTRA:
        del doc["outcomes"][subject]
        del doc["routing_table_v2"][subject]
    doc["v"] = 1
    return doc


def test_v3_seeding_family_boolean_off_becomes_off_row(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    doc = _pre_v3_doc()
    doc["live_activities"]["openings"] = False
    path.write_text(json.dumps(doc))

    settings = policy_settings.startup(path)
    assert all(v == "off" for v in settings["outcomes"]["opening"].values())
    assert all(v == "log" for v in settings["routing_table_v2"]["opening"].values())
    # Untouched booleans seed the default rows.
    assert settings["outcomes"]["package"] == policy_settings.DEFAULT_EXTRA_OUTCOMES["package"]
    # Derived booleans reflect the seeded rows.
    assert settings["live_activities"]["openings"] is False
    assert settings["live_activities"]["package"] is True
    policy_settings.reset_for_tests()


def test_v3_seeding_alert_all_changes_bumps_glance_to_notify(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    doc = _pre_v3_doc()
    doc["live_activities"]["alert_all_changes"] = True
    path.write_text(json.dumps(doc))

    settings = policy_settings.startup(path)
    for subject in policy_settings.SUBJECTS_V3_EXTRA:
        assert "glance" not in settings["outcomes"][subject].values()
    assert settings["outcomes"]["package"]["yard"] == "notify"
    # The field itself is retired: derived False from here on.
    assert settings["live_activities"]["alert_all_changes"] is False
    policy_settings.reset_for_tests()


def test_v3_seeding_is_idempotent_and_never_reseeds_user_edits(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    path.write_text(json.dumps(_pre_v3_doc()))
    policy_settings.startup(path)

    # User tunes a seeded row; a later startup must not re-seed over it.
    doc = json.loads(path.read_text())
    assert doc["v"] == policy_settings.SETTINGS_VERSION
    doc["outcomes"]["bin"]["street"] = "off"
    path.write_text(json.dumps(doc))
    settings = policy_settings.startup(path)
    assert settings["outcomes"]["bin"]["street"] == "off"
    policy_settings.reset_for_tests()


def test_retired_family_booleans_are_derived_not_stored():
    merged = policy_settings.normalize_settings(
        policy_settings.default_settings()
        | {"live_activities": {"package": False, "alert_all_changes": True}}
    )
    # Incoming retired fields are ignored; derivation from outcomes wins.
    assert merged["live_activities"]["package"] is True
    assert merged["live_activities"]["alert_all_changes"] is False


def test_derived_person_restricted_follows_off_limits_cell():
    doc = policy_settings.default_settings()
    doc["outcomes"]["person"]["off_limits"] = "off"
    merged = policy_settings.normalize_settings(doc)
    assert merged["live_activities"]["person_restricted"] is False
    assert merged["live_activities"]["person"] is True
