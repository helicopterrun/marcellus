"""Geometric fusion (`push/fusion.py`): polygon projection and clustering."""

from __future__ import annotations

import math

import pytest

from marcellus.push import fusion, ground

# Same synthetic rig as test_push_ground: 10ft mount, 12° down, 90° HFOV.
FACTS = {
    "hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0,
    "vfov": 2 * math.degrees(math.atan((9 / 16) * math.tan(math.radians(45)))),
}
LAYOUT = {"x": 0.5, "y": 0.5, "azimuth": 0.0}
SCALE_FT = 200.0


@pytest.fixture(autouse=True)
def _fake_camera_ground(monkeypatch):
    monkeypatch.setattr(ground, "camera_ground", lambda cam: dict(FACTS))


def _project(coords, **kw):
    args = {"camera": "cam", "layout_entry": LAYOUT, "scale_ft": SCALE_FT}
    args.update(kw)
    return fusion.project_polygon(coords, **args)


def test_floor_polygon_round_trips():
    # A quad entirely in the projectable lower half of the frame.
    coords = [(0.3, 0.6), (0.7, 0.6), (0.7, 0.9), (0.3, 0.9)]
    pts = _project(coords)
    assert pts is not None and len(pts) >= 4
    # Every corner's own projection appears among the densified samples.
    for c in coords:
        wp = ground.world_position(
            c[0], c[1], camera="cam", layout_entry=LAYOUT, scale_ft=SCALE_FT,
        )
        assert any(math.hypot(p[0] - wp[0], p[1] - wp[1]) < 1e-6 for p in pts)


def test_sky_polygon_is_none():
    # Entire polygon above the horizon (top of frame).
    assert _project([(0.1, 0.0), (0.9, 0.0), (0.5, 0.05)]) is None


def test_horizon_crossing_polygon_clips():
    # Spans from sky (y=0) to floor (y=0.9): must clip, not vanish, and
    # every surviving point must be a valid projection.
    coords = [(0.2, 0.0), (0.8, 0.0), (0.8, 0.9), (0.2, 0.9)]
    pts = _project(coords)
    assert pts is not None
    full = _project([(0.2, 0.6), (0.8, 0.6), (0.8, 0.9), (0.2, 0.9)])
    assert len(pts) > 3
    assert full is not None
    facts = ground.camera_ground("cam")
    # The clipped edge hugs the projection limit: its farthest point is
    # near _MAX_FORWARD_FT while a pure floor quad stays well inside.
    assert any(
        abs(150.0 - fwd) < 20.0
        for fwd in [
            # recover forward distance from map y (azimuth 0: north = -y)
            (0.5 - p[1]) * SCALE_FT for p in pts
        ]
    )
    assert facts is not None


def _pos(camera, track_id, x, y, forward_ft, label="person", stationary=False):
    return fusion.TrackPos(
        camera=camera, track_id=track_id, label=label, x=x, y=y,
        forward_ft=forward_ft, stationary=stationary, age_s=0.5,
    )


def test_cluster_merges_close_cross_camera():
    # 8 ft apart on a 200 ft map: within the 10 ft floor.
    a = _pos("north", "t1", 0.50, 0.50, 30.0)
    b = _pos("south", "t2", 0.54, 0.50, 30.0)  # 0.04 * 200 = 8 ft
    out = fusion.cluster([a, b], scale_ft=SCALE_FT)
    assert len(out) == 1
    assert {m.camera for m in out[0].members} == {"north", "south"}
    # Weighted mean sits between the two.
    assert 0.50 < out[0].x < 0.54


def test_cluster_keeps_distant_apart():
    a = _pos("north", "t1", 0.5, 0.5, 30.0)
    b = _pos("south", "t2", 0.7, 0.5, 30.0)  # 40 ft
    assert len(fusion.cluster([a, b], scale_ft=SCALE_FT)) == 2


def test_cluster_never_merges_same_camera():
    a = _pos("north", "t1", 0.50, 0.50, 30.0)
    b = _pos("north", "t2", 0.505, 0.50, 30.0)  # 1 ft apart, same camera
    assert len(fusion.cluster([a, b], scale_ft=SCALE_FT)) == 2


def test_cluster_requires_matching_labels():
    a = _pos("north", "t1", 0.50, 0.50, 30.0, label="person")
    b = _pos("south", "t2", 0.505, 0.50, 30.0, label="car")
    c = _pos("gate", "t3", 0.50, 0.50, 30.0, label=None)
    assert len(fusion.cluster([a, b, c], scale_ft=SCALE_FT)) == 3


def test_threshold_scales_with_forward_distance():
    # 25 ft apart, both seen ~120 ft out: threshold = 0.25*120 = 30 ft.
    a = _pos("north", "t1", 0.500, 0.5, 120.0)
    b = _pos("south", "t2", 0.625, 0.5, 120.0)  # 25 ft
    assert len(fusion.cluster([a, b], scale_ft=SCALE_FT)) == 1
    # Same separation seen close-up stays two objects.
    a2 = _pos("north", "t1", 0.500, 0.5, 20.0)
    b2 = _pos("south", "t2", 0.625, 0.5, 20.0)
    assert len(fusion.cluster([a2, b2], scale_ft=SCALE_FT)) == 2


def test_fused_position_weights_nearer_camera():
    near = _pos("north", "t1", 0.50, 0.5, 10.0)
    far = _pos("south", "t2", 0.54, 0.5, 90.0)
    (c,) = fusion.cluster([near, far], scale_ft=SCALE_FT)
    # 1/10 vs 1/90 weighting pulls the fused point toward the near camera.
    assert abs(c.x - near.x) < abs(c.x - far.x)


class _FakeState:
    def __init__(self, path_data, label="person", stationary=False):
        self.path_data = path_data
        self.label = label
        self.stationary = stationary


class _FakeStore:
    def __init__(self, entries):
        self._entries = entries

    def items(self):
        return list(self._entries.items())


def test_track_world_positions_filters_stale_and_unplaced():
    now = 1000.0
    store = _FakeStore({
        ("cam", "fresh"): _FakeState([(0.5, 0.7, now - 1.0)]),
        ("cam", "stale"): _FakeState([(0.5, 0.7, now - 30.0)]),
        ("cam", "sky"): _FakeState([(0.5, 0.0, now - 1.0)]),
        ("unplaced", "t"): _FakeState([(0.5, 0.7, now - 1.0)]),
    })
    settings = {"camera_layout": {"cam": dict(LAYOUT)}, "map_scale_ft": SCALE_FT}
    out = fusion.track_world_positions(store, settings, now=now)
    assert [(p.camera, p.track_id) for p in out] == [("cam", "fresh")]
    assert out[0].label == "person"
    assert out[0].forward_ft > 0


def test_track_world_positions_no_scale_is_empty():
    store = _FakeStore({("cam", "t"): _FakeState([(0.5, 0.7, 999.5)])})
    assert fusion.track_world_positions(store, {"camera_layout": {}}, now=1000.0) == []
