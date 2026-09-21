"""`marcellus.tuning`: the knob registry, override file, validation, apply,
and effective-config reporting (settings-dial spec Part A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus import tuning
from marcellus.config import Settings


@pytest.fixture(autouse=True)
def _isolated_snapshot():
    tuning.reset_for_tests()
    yield
    tuning.reset_for_tests()


def _all_settings_keys() -> set[str]:
    keys: set[str] = set()
    for section, model in tuning._SECTION_MODELS.items():
        for name in model.model_fields:
            keys.add(f"{section}.{name}")
    keys.add("log_level")
    return keys


def test_registry_covers_every_settings_field():
    assert {k.key for k in tuning.KNOBS} == _all_settings_keys()


def test_registry_has_no_duplicate_keys():
    keys = [k.key for k in tuning.KNOBS]
    assert len(keys) == len(set(keys))


def test_wiring_and_secret_fields_are_not_editable():
    assert tuning.KNOBS_BY_KEY["frigate.base_url"].editable is False
    assert tuning.KNOBS_BY_KEY["frigate.config_path"].editable is False
    assert tuning.KNOBS_BY_KEY["sidecar.bind_host"].editable is False
    assert tuning.KNOBS_BY_KEY["push.mqtt_password"].editable is False
    assert tuning.KNOBS_BY_KEY["push.mqtt_password"].kind == "secret"
    assert tuning.KNOBS_BY_KEY["push.relay_key"].editable is False
    assert tuning.KNOBS_BY_KEY["push.relay_key"].kind == "secret"


def test_encounters_adjacency_is_editable_and_live() -> None:
    # Was wiring (restart-required, config-file-only) -- now live-editable
    # from /settings, rebuilt by EncounterService.reconcile().
    for key in ("encounters.adjacency", "encounters.not_adjacent"):
        knob = tuning.KNOBS_BY_KEY[key]
        assert knob.editable is True, key
        assert knob.live is True, key
        assert knob.kind == "pair_list", key


def test_face_capture_and_watchdog_are_editable_but_restart_required():
    for key in (
        "face_capture.capture_camera",
        "face_capture.trigger_cameras",
        "watchdog.restart_command",
        "watchdog.probe_path",
    ):
        knob = tuning.KNOBS_BY_KEY[key]
        assert knob.editable is True, key
        assert knob.live is False, key


def test_live_knobs_classification():
    for key in (
        "encounters.backfill_lookback_s",
        "encounters.gap_s",
        "encounters.reconcile_interval_s",
        "sidecar.login_rate_limit_attempts",
        "scrub.retention_days",
        "push.offline_silence_s",
        "log_level",
        "face_enrich.interval_s",
    ):
        assert tuning.KNOBS_BY_KEY[key].live is True, key

    for key in (
        "push.enabled",
        "push.transport",
        "scrub.cell_w",
        "push.thumbnail_max_edge",
        "push.dwell_source",
        "push.activity_resolution_s",
        "face_enrich.model_dir",
    ):
        assert tuning.KNOBS_BY_KEY[key].live is False, key


def test_enum_knobs():
    assert tuning.KNOBS_BY_KEY["scrub.format"].kind == "enum"
    assert tuning.KNOBS_BY_KEY["scrub.format"].choices == ("jpeg", "webp")
    assert tuning.KNOBS_BY_KEY["push.transport"].choices == ("mock", "relay")
    assert tuning.KNOBS_BY_KEY["push.dwell_source"].choices == ("events", "reviews")
    assert tuning.KNOBS_BY_KEY["log_level"].choices == ("DEBUG", "INFO", "WARNING", "ERROR")


def test_gap_s_is_dict_int():
    assert tuning.KNOBS_BY_KEY["encounters.gap_s"].kind == "dict_int"


def test_range_constraints_mirror_pydantic_fields():
    knob = tuning.KNOBS_BY_KEY["push.mqtt_queue_max"]
    assert (knob.min, knob.max) == (100, 100000)
    knob = tuning.KNOBS_BY_KEY["push.relay_retry_attempts"]
    assert (knob.min, knob.max) == (1, 10)


# ---------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------


def test_validate_unknown_key():
    errors = tuning.validate({"nope.not_real": 1})
    assert any("unknown key" in e for e in errors)


def test_validate_non_editable_key():
    errors = tuning.validate({"frigate.base_url": "http://x"})
    assert any("not editable" in e for e in errors)


def test_validate_wrong_type():
    errors = tuning.validate({"scrub.cell_w": "not an int"})
    assert any("scrub.cell_w" in e for e in errors)


def test_validate_out_of_range():
    errors = tuning.validate({"push.mqtt_queue_max": 5})
    assert any("must be >=" in e for e in errors)


def test_validate_bad_enum():
    errors = tuning.validate({"scrub.format": "png"})
    assert any("scrub.format" in e for e in errors)


def test_validate_dict_int():
    assert tuning.validate({"encounters.gap_s": {"animal": 10.0}}) == []
    errors = tuning.validate({"encounters.gap_s": {"animal": "ten"}})
    assert any("encounters.gap_s" in e for e in errors)


def test_validate_cross_field_scrub_interval_error():
    errors = tuning.validate({"scrub.aged_interval_s": 999999.0})
    assert errors, "aged_interval_s past every derived interval should fail cross-field validation"


def test_validate_ok_empty():
    assert tuning.validate({}) == []


# ---------------------------------------------------------------------
# apply/revert round trip
# ---------------------------------------------------------------------


def test_apply_overrides_round_trip():
    settings = Settings()
    assert settings.scrub.cell_w == 320
    tuning.apply_overrides(settings, {"scrub.cell_w": 400})
    assert settings.scrub.cell_w == 400

    tuning.snapshot_startup(Settings())
    base = tuning.get_startup_snapshot()
    assert base is not None
    knob = tuning.KNOBS_BY_KEY["scrub.cell_w"]
    tuning.set_field(settings, knob, tuning.snapshot_value(base, knob))
    assert settings.scrub.cell_w == 320


def test_apply_overrides_dict_int_merges():
    settings = Settings()
    tuning.apply_overrides(settings, {"encounters.gap_s": {"animal": 999.0}})
    assert settings.encounters.gap_s["animal"] == 999.0
    # Other keys untouched.
    assert settings.encounters.gap_s["person"] == 90.0


def test_apply_log_level_sets_root_and_uvicorn_loggers():
    import logging

    tuning.apply_log_level("WARNING")
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("uvicorn").level == logging.WARNING
    logging.getLogger().setLevel(logging.NOTSET)


def test_env_locked_key_not_applied_by_load_settings(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MARCELLUS_SCRUB__CELL_W", "111")
    overrides_file = tmp_path / "config" / "tuning.json"
    overrides_file.parent.mkdir(parents=True)
    overrides_file.write_text(json.dumps({"_rev": 1, "scrub.cell_w": 222}))

    from marcellus.config import load_settings

    settings = load_settings()
    # env wins: the override file's value must not have landed.
    assert settings.scrub.cell_w == 111


def test_load_settings_applies_non_env_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    overrides_file = tmp_path / "config" / "tuning.json"
    overrides_file.parent.mkdir(parents=True)
    overrides_file.write_text(json.dumps({"_rev": 1, "scrub.cell_w": 333}))

    from marcellus.config import load_settings

    settings = load_settings()
    assert settings.scrub.cell_w == 333


# ---------------------------------------------------------------------
# file read/write
# ---------------------------------------------------------------------


def test_read_overrides_missing_file(tmp_path: Path):
    assert tuning.read_overrides(tmp_path / "nope.json") == {}


def test_read_overrides_corrupt_file_tolerated(tmp_path: Path, caplog):
    p = tmp_path / "tuning.json"
    p.write_text("{not json")
    assert tuning.read_overrides(p) == {}


def test_write_overrides_atomic_and_bumps_rev(tmp_path: Path):
    p = tmp_path / "sub" / "tuning.json"
    rev1 = tuning.write_overrides(p, {"scrub.cell_w": 400})
    assert rev1 == 1
    assert not p.with_suffix(".json.tmp").exists()
    data = json.loads(p.read_text())
    assert data["scrub.cell_w"] == 400
    assert data["_rev"] == 1

    rev2 = tuning.write_overrides(p, {"scrub.cell_w": 500})
    assert rev2 == 2
    assert tuning.read_rev(p) == 2
    assert tuning.read_overrides(p) == {"scrub.cell_w": 500}


# ---------------------------------------------------------------------
# effective()
# ---------------------------------------------------------------------


def test_effective_source_default():
    settings = Settings()
    rows = tuning.effective(settings, {})
    row = next(r for r in rows if r["key"] == "scrub.cell_w")
    assert row["source"] == "default"
    assert row["value"] == 320


def test_effective_source_yaml():
    from marcellus.config import ScrubSection

    settings = Settings(scrub=ScrubSection(cell_w=555))
    rows = tuning.effective(settings, {})
    row = next(r for r in rows if r["key"] == "scrub.cell_w")
    assert row["source"] == "yaml"


def test_effective_source_env(monkeypatch):
    monkeypatch.setenv("MARCELLUS_SCRUB__CELL_W", "700")
    settings = Settings()
    rows = tuning.effective(settings, {})
    row = next(r for r in rows if r["key"] == "scrub.cell_w")
    assert row["source"] == "env"
    assert row["locked"] is True


def test_effective_source_override():
    settings = Settings()
    tuning.apply_overrides(settings, {"scrub.cell_w": 800})
    rows = tuning.effective(settings, {"scrub.cell_w": 800})
    row = next(r for r in rows if r["key"] == "scrub.cell_w")
    assert row["source"] == "override"


def test_effective_masks_secrets():
    from marcellus.config import PushSection

    settings = Settings(push=PushSection(relay_key="s3cr3t"))
    rows = tuning.effective(settings, {})
    row = next(r for r in rows if r["key"] == "push.relay_key")
    assert row["value"] == "••••"

    settings2 = Settings()
    rows2 = tuning.effective(settings2, {})
    row2 = next(r for r in rows2 if r["key"] == "push.relay_key")
    assert row2["value"] == ""


def test_pending_restart_lists_non_live_change_not_live_change():
    settings = Settings()
    tuning.snapshot_startup(settings)
    overrides = {"scrub.cell_w": 999, "scrub.retention_days": 30}
    tuning.apply_overrides(settings, overrides)
    pending = tuning.pending_restart(settings, overrides)
    assert "scrub.cell_w" in pending
    assert "scrub.retention_days" not in pending  # live -- no restart needed


def test_pending_restart_empty_with_no_snapshot():
    settings = Settings()
    assert tuning.pending_restart(settings, {"scrub.cell_w": 999}) == []


def test_pending_restart_ignores_override_already_applied_at_boot():
    """An override the process booted with is not pending; removing it is."""
    settings = Settings()
    tuning.snapshot_base(settings)
    tuning.apply_overrides(settings, {"scrub.cell_w": 999})
    tuning.snapshot_startup(settings)
    assert tuning.pending_restart(settings) == []
    base = tuning.get_startup_snapshot()
    assert base is not None
    knob = tuning.KNOBS_BY_KEY["scrub.cell_w"]
    tuning.set_field(settings, knob, tuning.snapshot_value(base, knob))
    assert tuning.pending_restart(settings) == ["scrub.cell_w"]


def test_load_settings_skips_invalid_file_entries(tmp_path, monkeypatch, caplog):
    import json
    import logging

    from marcellus.config import load_settings

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tuning.json").write_text(
        json.dumps({"_rev": 1, "scrub.cell_w": "wide", "nope.key": 1, "scrub.retention_days": 9})
    )
    with caplog.at_level(logging.WARNING, logger="marcellus.config"):
        s = load_settings()
    assert s.scrub.retention_days == 9
    assert s.scrub.cell_w == 320
    assert "scrub.cell_w" in caplog.text and "nope.key" in caplog.text


def test_encounters_learned_gap_keys_are_live_bool_and_float() -> None:
    """M3: `use_learned_gaps` and `transition_slack` are live -- the
    rollout plan flips them via PUT /v1/tuning without a restart."""
    use_learned = tuning.KNOBS_BY_KEY["encounters.use_learned_gaps"]
    slack = tuning.KNOBS_BY_KEY["encounters.transition_slack"]
    assert use_learned.editable is True
    assert use_learned.live is True
    assert use_learned.kind == "bool"
    assert slack.editable is True
    assert slack.live is True
    assert slack.kind == "float"


def test_apply_use_learned_gaps_override_round_trips_as_bool() -> None:
    settings = Settings()
    assert settings.encounters.use_learned_gaps is False
    tuning.apply_overrides(settings, {"encounters.use_learned_gaps": True})
    assert settings.encounters.use_learned_gaps is True
    assert isinstance(settings.encounters.use_learned_gaps, bool)
