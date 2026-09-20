"""Calibrated heading from the path trail (`delivery_wire._heading_label`).

Measured 2026-08-15: this install's Frigate reports velocity_angle=0 and
speed=0 on every event (no zone distance calibration), so heading derives
from path_data dotted against the per-camera vector drawn on /cameras.
"""

from __future__ import annotations

import pytest

from marcellus.push import policy_settings
from marcellus.push.delivery_wire import (
    _build_motion,
    _heading_label,
    _movement_vector,
)


@pytest.fixture(autouse=True)
def _reset_policy():
    policy_settings.apply_settings(policy_settings.default_settings())
    yield
    policy_settings.apply_settings(policy_settings.default_settings())


def _calibrate(camera: str, dx: float, dy: float) -> None:
    settings = policy_settings.default_settings()
    settings["camera_headings"] = {camera: {"dx": dx, "dy": dy}}
    policy_settings.apply_settings(settings)


def test_movement_vector_none_for_short_or_jittery_trails():
    assert _movement_vector(None) is None
    assert _movement_vector([(0.5, 0.5)]) is None
    # All points within the jitter floor.
    assert _movement_vector([(0.500, 0.500), (0.505, 0.502), (0.501, 0.499)]) is None


def test_movement_vector_walks_back_past_jitter():
    # Recent jitter, but real travel further back in the trail.
    vec = _movement_vector([(0.2, 0.5), (0.5, 0.5), (0.505, 0.5)])
    assert vec is not None
    dx, dy = vec
    assert dx == pytest.approx(1.0, abs=0.05)
    assert dy == pytest.approx(0.0, abs=0.05)


def test_stationary_wins_regardless_of_calibration():
    assert _heading_label([(0.1, 0.1), (0.9, 0.9)], True, "garden") == "stationary"


def test_uncalibrated_camera_has_no_heading():
    assert _heading_label([(0.1, 0.5), (0.9, 0.5)], False, "garden") is None


def test_calibrated_headings():
    # "Home" is toward the top of the frame (dy = -1).
    _calibrate("garden", 0.0, -1.0)
    # Moving up the frame: approaching.
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "garden") == "approaching"
    # Moving down the frame: leaving.
    assert _heading_label([(0.5, 0.2), (0.5, 0.8)], False, "garden") == "leaving"
    # Moving sideways: passing.
    assert _heading_label([(0.1, 0.5), (0.9, 0.5)], False, "garden") == "passing"
    # A different camera stays uncalibrated.
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "street") is None


def test_build_motion_shapes():
    _calibrate("garden", 1.0, 0.0)
    assert _build_motion([(0.1, 0.5), (0.9, 0.5)], False, "garden") == {
        "heading": "approaching"
    }
    assert _build_motion(None, False, "garden") is None
    assert _build_motion(None, True, "garden") == {"heading": "stationary"}


# ---- Derived heading from world geometry (pie azimuth + secure area) ----


def _world_settings(azimuth, cam_x=0.5, cam_y=0.2, fov=90.0):
    settings = policy_settings.default_settings()
    settings["camera_layout"] = {
        "garden": {"x": cam_x, "y": cam_y, "azimuth": azimuth, "fov": fov},
    }
    # Secure area centered at (0.5, 0.8) — due SOUTH of the camera.
    settings["secure_area"] = {"x0": 0.4, "y0": 0.7, "x1": 0.6, "y1": 0.9}
    return settings


def test_derived_heading_secure_area_dead_ahead_points_up():
    """Camera faces south (az 180) with the secure area straight ahead:
    'toward home' is deeper into the scene = up in the frame."""
    vec = policy_settings.derived_camera_heading("garden", _world_settings(180.0))
    assert vec is not None
    assert vec["dy"] == pytest.approx(-1.0, abs=0.01)
    assert vec["dx"] == pytest.approx(0.0, abs=0.01)


def test_derived_heading_secure_area_behind_points_down():
    """Camera faces north (az 0) with the secure area behind it: 'toward
    home' is out of the bottom of the frame."""
    vec = policy_settings.derived_camera_heading("garden", _world_settings(0.0))
    assert vec is not None
    assert vec["dy"] == pytest.approx(1.0, abs=0.01)


def test_derived_heading_secure_area_to_the_right():
    """Camera faces east (az 90); secure area to the south = camera's
    right: arrow points right in the frame."""
    vec = policy_settings.derived_camera_heading("garden", _world_settings(90.0))
    assert vec is not None
    assert vec["dx"] == pytest.approx(1.0, abs=0.01)
    assert vec["dy"] == pytest.approx(0.0, abs=0.01)


def test_derived_heading_requires_azimuth_and_secure_area():
    settings = _world_settings(180.0)
    del settings["camera_layout"]["garden"]["azimuth"]
    assert policy_settings.derived_camera_heading("garden", settings) is None
    settings = _world_settings(180.0)
    settings["secure_area"] = None
    assert policy_settings.derived_camera_heading("garden", settings) is None


def test_heading_label_falls_back_to_derived_when_no_manual_arrow():
    settings = _world_settings(180.0)
    policy_settings.apply_settings(settings)
    # Moving up the frame (toward the derived 'home' direction): approaching.
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "garden") == "approaching"
    assert _heading_label([(0.5, 0.2), (0.5, 0.8)], False, "garden") == "leaving"

    # A manual arrow overrides the derived one: point it the OPPOSITE way.
    settings["camera_headings"] = {"garden": {"dx": 0.0, "dy": 1.0}}
    policy_settings.apply_settings(settings)
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "garden") == "leaving"


# ---- Direction/speed as ladder modifiers --------------------------------


def test_ladder_reasons_include_direction_and_speed():
    from marcellus.push import ladder_policy
    from marcellus.push.ladder import Snapshot, evaluate_ladder

    assert "approaching_secure" in ladder_policy.WORRY_REASONS
    assert "moving_fast" in ladder_policy.WORRY_REASONS
    assert "leaving_scene" in ladder_policy.CALM_REASONS

    base = evaluate_ladder(Snapshot(subject="person", place="yard"))
    bumped = evaluate_ladder(
        Snapshot(subject="person", place="yard", approaching_secure=True)
    )
    calmed = evaluate_ladder(
        Snapshot(subject="person", place="doors", leaving_scene=True)
    )
    levels = ladder_policy.LEVELS
    assert levels.index(bumped) == levels.index(base) + 1
    doors_base = evaluate_ladder(Snapshot(subject="person", place="doors"))
    assert levels.index(calmed) == levels.index(doors_base) - 1


def test_heading_streak_counts_and_resets():
    from marcellus.push.delivery_wire import (
        _heading_streaks,
        _update_heading_streak,
        last_heading,
    )

    _heading_streaks.clear()
    assert _update_heading_streak("cam", "t1", "approaching") == 1
    assert _update_heading_streak("cam", "t1", "approaching") == 2
    assert last_heading("cam", "t1") == "approaching"
    # Direction change resets the streak.
    assert _update_heading_streak("cam", "t1", "leaving") == 1
    # None (unknown) zeroes it.
    assert _update_heading_streak("cam", "t1", None) == 0
    _heading_streaks.clear()


# ---- Distance copy: rounding, hysteresis, and the approach story ----


def test_round_distance_ft():
    from marcellus.push.delivery_wire import _round_distance_ft
    assert _round_distance_ft(31.0) == 30
    assert _round_distance_ft(33.0) == 35
    assert _round_distance_ft(1.0) == 5  # floor: never announce below 5


def test_stabilize_distance_hysteresis():
    from marcellus.push import delivery_wire as dw
    dw._announced_distance.clear()
    assert dw._stabilize_distance("cam", "t1", 31.0) == 30
    # Small wobble (< 10 ft from the announced 30) keeps saying 30.
    assert dw._stabilize_distance("cam", "t1", 26.0) == 30
    assert dw._stabilize_distance("cam", "t1", 38.0) == 30
    # A real move re-announces.
    assert dw._stabilize_distance("cam", "t1", 18.0) == 20
    # Inside the secure area announces 0 immediately, no hysteresis.
    assert dw._stabilize_distance("cam", "t1", 0.0) == 0
    # Other tracks are independent.
    assert dw._stabilize_distance("cam", "t2", 52.0) == 50
    dw._announced_distance.clear()


def test_approach_story_copy():
    from marcellus.push.delivery_wire import _approach_story
    assert _approach_story(30, {"walking"}) == "approaching — 30 ft out, walking"
    assert _approach_story(30, {"walking", "running"}) == "approaching — 30 ft out, running"
    assert _approach_story(30, set()) == "approaching — 30 ft out"
    assert _approach_story(0, {"walking"}) == "at the house"
    # Beyond 100 ft (or unknown) the classic phrase stands.
    assert _approach_story(105, {"walking"}) == "approaching the house"
    assert _approach_story(None, {"walking"}) == "approaching the house"
