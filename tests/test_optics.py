"""Pins the placement-planner optics math (mirrored in static/js/placement.js)."""

from __future__ import annotations

import math

import pytest

from marcellus.analysis import optics


def test_fit_varifocal_passes_through_both_anchors() -> None:
    anchors = [(2.7, 114.0), (12.0, 47.0)]
    w, f0 = optics.fit_varifocal(anchors)
    for focal, hfov in anchors:
        assert optics.hfov_from_focal(focal, w, f0) == pytest.approx(hfov, abs=1e-6)


def test_hfov_monotonic_in_focal() -> None:
    w, f0 = optics.fit_varifocal([(2.7, 114.0), (12.0, 47.0)])
    prev = 999.0
    for focal in [2.7, 4, 6, 8, 10, 12]:
        hfov = optics.hfov_from_focal(focal, w, f0)
        assert hfov < prev  # zooming in narrows the field
        prev = hfov


def test_focal_from_hfov_is_inverse() -> None:
    w, f0 = optics.fit_varifocal([(2.7, 114.0), (12.0, 47.0)])
    for focal in [3.0, 5.5, 9.1]:
        hfov = optics.hfov_from_focal(focal, w, f0)
        assert optics.focal_from_hfov(hfov, w, f0) == pytest.approx(focal, abs=1e-6)


def test_max_distance_and_object_px_are_consistent() -> None:
    # At exactly max_distance for a target, the object's on-screen width == target.
    width_ft, det_w, hfov, target = 0.5, 1280, 80.0, 80.0
    d = optics.max_distance_ft(width_ft, det_w, hfov, target)
    assert optics.object_px_width(width_ft, det_w, hfov, d) == pytest.approx(target, abs=1e-6)


def test_main_stream_doubles_reach_vs_substream() -> None:
    # 2688 main is ~2.1x the 1280 substream -> proportionally farther reach.
    sub = optics.max_distance_ft(0.5, 1280, 90.0, 80.0)
    main = optics.max_distance_ft(0.5, 2688, 90.0, 80.0)
    assert main / sub == pytest.approx(2688 / 1280, abs=1e-6)


def test_px_per_ft_falls_with_distance() -> None:
    near = optics.px_per_ft(1280, 90.0, 5)
    far = optics.px_per_ft(1280, 90.0, 50)
    assert near == pytest.approx(far * 10, abs=1e-6)


def test_target_area_matches_width_squared_times_aspect() -> None:
    assert optics.target_area_px2(80, 1.4) == 80 * 80 * 1.4


def test_known_value_hfov_90deg() -> None:
    # A sensor exactly as wide as 2*focal gives a 90 deg HFOV.
    assert optics.hfov_from_focal(4.0, 8.0) == pytest.approx(90.0, abs=1e-9)
    assert optics.hfov_from_focal(4.0, 8.0) == math.degrees(2 * math.atan(1.0))


def test_vfov_matches_aspect_ratio() -> None:
    # 16:9 frame: VFOV is the HFOV scaled by 9/16 in tan-space.
    vfov = optics.vfov_from_hfov(90.0, 1280, 720)
    expected = math.degrees(2 * math.atan(math.tan(math.radians(45)) * 720 / 1280))
    assert vfov == pytest.approx(expected, abs=1e-9)
    assert vfov < 90.0  # narrower vertically than horizontally


def test_ground_coverage_near_far() -> None:
    # 10 ft up, axis 30 deg down, 40 deg VFOV -> rays at 10 and 50 deg depression.
    near, far = optics.ground_coverage(10.0, 30.0, 40.0)
    assert near == pytest.approx(10.0 / math.tan(math.radians(50)), abs=1e-6)
    assert far == pytest.approx(10.0 / math.tan(math.radians(10)), abs=1e-6)
    assert near < far


def test_ground_coverage_top_ray_above_horizon_is_unbounded() -> None:
    # Shallow tilt: top ray points up -> far edge is the horizon (None).
    near, far = optics.ground_coverage(10.0, 5.0, 40.0)
    assert far is None
    assert near > 0


def test_bbox_height_shrinks_with_distance() -> None:
    # ground-standing person: bottom 0, top 5.5 ft
    near = optics.bbox_height_px(0.0, 5.5, 10.0, 10.0, 50.0, 720)
    far = optics.bbox_height_px(0.0, 5.5, 10.0, 40.0, 50.0, 720)
    assert near > far > 0


def test_bbox_height_face_at_eye_level() -> None:
    # A 0.7 ft face centred at 5.4 ft eye level, 10 ft camera, 12 ft away.
    px = optics.bbox_height_px(5.4 - 0.35, 5.4 + 0.35, 10.0, 12.0, 50.0, 720)
    assert px > 0


def test_face_depression_sign() -> None:
    # Low doorbell (3 ft) vs a 5.5 ft eye line -> looking UP (negative).
    assert optics.face_depression_deg(3.0, 6.0, 5.5) < 0
    # High mast looking down -> steep positive depression.
    assert optics.face_depression_deg(35.0, 20.0, 5.5) > 30


def test_dori_identification_closer_than_recognition() -> None:
    idd = optics.dori_distance_ft(1280, 90.0, optics.DORI_PX_PER_M["identification"])
    rec = optics.dori_distance_ft(1280, 90.0, optics.DORI_PX_PER_M["recognition"])
    # Identification needs 2x the density of recognition -> half the distance.
    assert idd == pytest.approx(rec / 2, abs=1e-6)


def test_presets_payload_shape() -> None:
    p = optics.presets_payload()
    assert {"lenses", "resolutions", "objects", "cameras", "dori", "refs"} <= p.keys()
    assert p["refs"]["face_min_area_px2"] == 1000  # live config, not the 500 default
    assert p["refs"]["face_recog_floor_px2"] == 3000
    assert set(p["dori"]) == {"detection", "observation", "recognition", "identification"}
    # Every deployed camera carries a (possibly estimated) tilt for the elevation view.
    for cam in p["cameras"]:
        assert "tilt_deg" in cam and "mount_ft" in cam

    # Every varifocal lens carries a fitted sensor width + offset for the JS.
    vari = [lo for lo in p["lenses"] if lo["type"] == "varifocal"]
    assert vari
    for lo in vari:
        assert "sensor_width_mm" in lo and "focal_offset_mm" in lo

    for obj in p["objects"]:
        assert {"id", "label", "width_ft", "aspect", "target_px"} <= obj.keys()

    # Deployed cameras reference a real lens + resolution id.
    lens_ids = {lo["id"] for lo in p["lenses"]}
    res_ids = {r["id"] for r in p["resolutions"]}
    for cam in p["cameras"]:
        assert cam["lens"] in lens_ids
        assert cam["res"] in res_ids


def test_deployment_seed_entries_are_complete_and_lenses_resolve():
    """The seed feeds policy_settings.seeded_camera_optics — every entry
    needs the projection trio, and named lenses must exist in the catalogue."""
    lens_ids = {lens["id"] for lens in optics.presets_payload()["lenses"]}
    for cam in optics.DEPLOYMENT_SEED:
        assert {"id", "hfov", "mount_ft", "tilt_deg"} <= cam.keys(), cam.get("id")
        assert cam["lens"] in lens_ids, cam["id"]
