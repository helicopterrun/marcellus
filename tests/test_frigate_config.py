"""Tests for reading Frigate's own config (frigate_config.py).

Feeds `/v1/coverage`'s `recording_retention_days`, which exists to keep the
scrub cache's horizon from being mistaken for the recording window's -- on the
reference deployment those are 4 days and 8 days respectively.
"""

from __future__ import annotations

from pathlib import Path

from marcellus.frigate_config import load_frigate_config, recording_retention_days

MODERN = """
record:
  enabled: true
  continuous:
    days: 4
  motion:
    days: 8
  alerts:
    retain:
      days: 90
  detections:
    retain:
      days: 30
cameras:
  doorbell:
    record:
      continuous:
        days: 1
"""


def test_reports_the_outer_bound_of_continuous_and_motion(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(MODERN)
    # 8, not 4: footage exists in the motion band well past the continuous one.
    assert recording_retention_days(cfg) == 8.0


def test_alert_and_detection_retention_are_excluded(tmp_path: Path) -> None:
    """Those keep recordings only around their own events, so reporting 90
    would promise coverage that mostly isn't there."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(MODERN)
    assert recording_retention_days(cfg) != 90.0


def test_camera_overrides_are_merged(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(MODERN)
    # doorbell narrows continuous to 1 day; motion (8) still applies globally.
    assert recording_retention_days(cfg, "doorbell") == 8.0


def test_older_flat_retain_shape(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("record:\n  retain:\n    days: 10\n")
    assert recording_retention_days(cfg) == 10.0


def test_missing_or_broken_config_yields_none(tmp_path: Path) -> None:
    assert recording_retention_days(tmp_path / "nope.yml") is None
    broken = tmp_path / "broken.yml"
    broken.write_text("record: {\n  unclosed: [1, 2\n")
    assert recording_retention_days(broken) is None
    assert load_frigate_config(broken) == {}


def test_config_without_record_section(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("cameras: {}\n")
    assert recording_retention_days(cfg) is None


def test_reparses_when_the_file_changes(tmp_path: Path) -> None:
    """Cached by mtime -- an operator editing Frigate's config shouldn't need
    to restart the sidecar to see it."""
    import os

    cfg = tmp_path / "config.yml"
    cfg.write_text("record:\n  continuous:\n    days: 2\n")
    assert recording_retention_days(cfg) == 2.0
    cfg.write_text("record:\n  continuous:\n    days: 6\n")
    os.utime(cfg, (0, 0))  # force a distinct mtime
    assert recording_retention_days(cfg) == 6.0
