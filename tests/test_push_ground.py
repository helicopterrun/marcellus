"""Ground-plane projection and speed (`push/ground.py`)."""

from __future__ import annotations

import math

import pytest

from marcellus.analysis import optics
from marcellus.push import ground

# A synthetic camera: 10ft mount, 12 deg down, 90 deg HFOV (matches the
# stairway cameras). vfov ~ 58.7 deg at 16:9.
FACTS = {
    "hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0,
    "vfov": 2 * math.degrees(math.atan((9 / 16) * math.tan(math.radians(45)))),
}


def test_project_center_of_frame():
    fwd, lat = ground.project(0.5, 0.5, FACTS)
    # Depression = tilt = 12 deg -> forward = 10 / tan(12) ~ 47 ft.
    assert fwd == pytest.approx(10 / math.tan(math.radians(12)), rel=1e-6)
    assert lat == pytest.approx(0.0, abs=1e-9)


def test_project_lower_in_frame_is_closer():
    near = ground.project(0.5, 0.9, FACTS)
    far = ground.project(0.5, 0.4, FACTS)
    assert near is not None and far is not None
    assert near[0] < far[0]


def test_project_above_horizon_is_none():
    # Top of frame: depression = 12 - vfov/2 ~ -17 deg -> sky.
    assert ground.project(0.5, 0.0, FACTS) is None


def test_project_right_of_frame_is_positive_lateral():
    fwd, lat = ground.project(1.0, 0.5, FACTS)
    assert lat > 0
    # At 90 deg HFOV the frame's right edge sits 45 deg off the optical
    # axis; on the tilted axis-row the ground offset is the slant distance
    # to that row (forward / cos tilt), not the forward distance — the old
    # first-order model claimed lat == fwd here and understated lateral.
    assert lat == pytest.approx(fwd / math.cos(math.radians(12)), rel=1e-6)


def test_speed_of_a_known_walk(monkeypatch):
    monkeypatch.setattr(ground, "camera_ground", lambda cam: dict(FACTS))
    # Walk straight down the frame center: y from 0.5 to 0.7 over 4s.
    # Distances: y=0.5 -> 47.0ft, y=0.7 -> 10/tan(12+0.2*vfov) ...
    path = []
    for i in range(5):
        y = 0.5 + 0.05 * i
        path.append((0.5, y, 100.0 + i))
    v = ground.speed_ft_s(path, "fake")
    d_start = ground.project(0.5, 0.5, FACTS)[0]
    d_end = ground.project(0.5, 0.7, FACTS)[0]
    expected = (d_start - d_end) / 4.0
    # Median of per-segment speeds on a curved projection differs a bit
    # from the end-to-end average — generous tolerance.
    assert v == pytest.approx(expected, rel=0.5)
    assert v > 0


def test_speed_requires_timestamps(monkeypatch):
    monkeypatch.setattr(ground, "camera_ground", lambda cam: dict(FACTS))
    assert ground.speed_ft_s([(0.5, 0.5, 0.0), (0.5, 0.7, 0.0)], "fake") is None


def test_speed_none_for_unknown_camera():
    assert ground.speed_ft_s([(0.5, 0.5, 1.0), (0.5, 0.7, 2.0)], "nope") is None


def test_speed_labels():
    assert ground.speed_label(None) is None
    assert ground.speed_label(0.5) is None
    assert ground.speed_label(4.0) == "walking"
    assert ground.speed_label(9.0) == "running"


def test_world_position_camera_facing_east(monkeypatch):
    monkeypatch.setattr(ground, "camera_ground", lambda cam: dict(FACTS))
    layout = {"x": 0.5, "y": 0.5, "azimuth": 90.0}
    pos = ground.world_position(0.5, 0.5, camera="fake", layout_entry=layout, scale_ft=100.0)
    assert pos is not None
    fwd = ground.project(0.5, 0.5, FACTS)[0]
    # Facing east: forward maps to +x on the map, no y change.
    assert pos[0] == pytest.approx(0.5 + fwd / 100.0, rel=1e-6)
    assert pos[1] == pytest.approx(0.5, abs=1e-9)


def test_world_position_requires_scale_and_azimuth(monkeypatch):
    monkeypatch.setattr(ground, "camera_ground", lambda cam: dict(FACTS))
    assert ground.world_position(
        0.5, 0.5, camera="fake", layout_entry={"x": 0.5, "y": 0.5}, scale_ft=100.0,
    ) is None
    assert ground.world_position(
        0.5, 0.5, camera="fake", layout_entry={"x": 0.5, "y": 0.5, "azimuth": 0.0},
        scale_ft=0,
    ) is None


def test_camera_ground_reads_settings_backed_optics():
    from marcellus.push import policy_settings

    doc = policy_settings.default_settings()
    doc["camera_optics"] = {
        "computed": {"hfov": 100.0, "mount_ft": 12.0, "tilt_deg": 10.0},
        "vendor": {"hfov": 138.0, "mount_ft": 3.0, "tilt_deg": 6.0, "vfov": 114.0},
    }
    policy_settings.apply_settings(doc)
    try:
        computed = ground.camera_ground("computed")
        assert computed is not None
        assert computed["vfov"] == pytest.approx(
            optics.vfov_from_hfov(100.0, 16, 9), rel=1e-6
        )
        # Vendor-published vfov wins over the 16:9 derivation.
        vendor = ground.camera_ground("vendor")
        assert vendor is not None and vendor["vfov"] == 114.0
        assert ground.camera_ground("never-onboarded") is None
    finally:
        policy_settings.reset_for_tests()


# ---- Distance to the secure area (map point -> rect, in feet) ----


def test_distance_to_secure_inside_is_zero():
    sa = {"x0": 0.2, "y0": 0.2, "x1": 0.6, "y1": 0.6}
    assert ground.distance_to_secure_ft(0.4, 0.4, sa, scale_ft=100) == 0.0


def test_distance_to_secure_edge_and_corner():
    sa = {"x0": 0.2, "y0": 0.2, "x1": 0.6, "y1": 0.6}
    # Straight left of the rect: pure x-leg, 0.1 * 100 ft.
    assert ground.distance_to_secure_ft(0.1, 0.4, sa, scale_ft=100) == pytest.approx(10.0)
    # Diagonal off the corner: hypot of both legs.
    d = ground.distance_to_secure_ft(0.1, 0.1, sa, scale_ft=100)
    assert d == pytest.approx(math.hypot(10.0, 10.0))


def test_distance_to_secure_aspect_scales_the_y_leg():
    sa = {"x0": 0.2, "y0": 0.2, "x1": 0.6, "y1": 0.6}
    # 0.1 above the rect on a map twice as tall as wide: 0.1 * 100 * 2 ft.
    d = ground.distance_to_secure_ft(0.4, 0.1, sa, scale_ft=100, aspect_h_over_w=2.0)
    assert d == pytest.approx(20.0)


def test_distance_to_secure_none_when_unconfigured():
    assert ground.distance_to_secure_ft(0.5, 0.5, None, scale_ft=100) is None
    assert ground.distance_to_secure_ft(0.5, 0.5, {"x0": 0}, scale_ft=100) is None
    sa = {"x0": 0.2, "y0": 0.2, "x1": 0.6, "y1": 0.6}
    assert ground.distance_to_secure_ft(0.5, 0.5, sa, scale_ft=None) is None
    assert ground.distance_to_secure_ft(0.5, 0.5, sa, scale_ft=0) is None


def test_distance_to_secure_reversed_corners_normalize():
    sa = {"x0": 0.6, "y0": 0.6, "x1": 0.2, "y1": 0.2}
    assert ground.distance_to_secure_ft(0.4, 0.4, sa, scale_ft=100) == 0.0
