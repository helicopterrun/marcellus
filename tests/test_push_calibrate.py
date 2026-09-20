"""Auto-tune calibration (`push/calibrate.py`): pair mining + aim fit."""

from __future__ import annotations

import math

from marcellus.push import calibrate, ground

HFOV = 90.0
MOUNT = 9.0
VFOV = 2 * math.degrees(math.atan((9 / 16) * math.tan(math.radians(HFOV / 2))))
SCALE_FT = 200.0

# Ground truth: three cameras on a triangle, distinct aims.
TRUE = {
    "north": {"x": 0.50, "y": 0.20, "azimuth": 170.0, "tilt": 12.0},
    "east": {"x": 0.80, "y": 0.60, "azimuth": 265.0, "tilt": 18.0},
    "west": {"x": 0.20, "y": 0.60, "azimuth": 80.0, "tilt": 9.0},
}


def _settings(perturb=None):
    """Settings doc using TRUE aims shifted by `perturb[cam] = (daz, dtilt)`."""
    perturb = perturb or {}
    layout, optics_tbl = {}, {}
    for cam, t in TRUE.items():
        daz, dtilt = perturb.get(cam, (0.0, 0.0))
        layout[cam] = {"x": t["x"], "y": t["y"], "azimuth": t["azimuth"] + daz}
        optics_tbl[cam] = {
            "hfov": HFOV, "mount_ft": MOUNT,
            "tilt_deg": t["tilt"] + dtilt, "vfov": VFOV,
        }
    return {
        "camera_layout": layout, "camera_optics": optics_tbl,
        "map_scale_ft": SCALE_FT, "floorplan": None,
    }


def _image_point(cam: str, wx_ft: float, wy_ft: float):
    """Inverse projection: world FEET (map x-east, y-south) -> image (x,y)
    as the TRUE camera would have seen it, or None if out of view."""
    t = TRUE[cam]
    dx = wx_ft - t["x"] * SCALE_FT
    dy = wy_ft - t["y"] * SCALE_FT
    rad = math.radians(t["azimuth"])
    # Inverse of view=(sin,-cos), right=(cos,sin).
    forward = dx * math.sin(rad) - dy * math.cos(rad)
    lateral = dx * math.cos(rad) + dy * math.sin(rad)
    if forward <= 1.0:
        return None
    # True pinhole inverse (matches ground.project): the ray's depression is
    # atan(mount/forward); its image-plane offsets are tangents of the angle
    # off the optical axis, not linear fractions of the FOV.
    depression = math.atan(MOUNT / forward)
    tilt = math.radians(t["tilt"])
    b = math.tan(depression - tilt)
    s = MOUNT / (math.sin(tilt) + b * math.cos(tilt))
    a = lateral / s
    x = 0.5 + a / (2 * math.tan(math.radians(HFOV / 2)))
    y = 0.5 + b / (2 * math.tan(math.radians(VFOV / 2)))
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        return None
    return (x, y)


def test_inverse_projection_round_trips():
    settings = _settings()
    wx, wy = 0.55 * SCALE_FT, 0.45 * SCALE_FT
    for cam in TRUE:
        pt = _image_point(cam, wx, wy)
        if pt is None:
            continue
        base = {c: (TRUE[c]["azimuth"], TRUE[c]["tilt"]) for c in TRUE}
        w = calibrate.world_ft(pt, cam, base, settings)
        assert w is not None
        assert abs(w[0] - wx) < 1e-6
        assert abs(w[1] - wy) < 1e-6


def _walk_points():
    """Two laps around a rectangle inside the triangle, brisk pace (4 ft
    between points, 1 s apart). Varied ranges and bearings to every camera
    break the azimuth/tilt degeneracy; the pace survives the miner's
    near-duplicate thinning."""
    corners = [
        (0.38 * SCALE_FT, 0.30 * SCALE_FT), (0.62 * SCALE_FT, 0.30 * SCALE_FT),
        (0.62 * SCALE_FT, 0.52 * SCALE_FT), (0.38 * SCALE_FT, 0.52 * SCALE_FT),
    ]
    pts = []
    t = 1000.0
    for _lap in range(2):
        for k in range(4):
            ax, ay = corners[k]
            bx, by = corners[(k + 1) % 4]
            steps = max(1, int(math.hypot(bx - ax, by - ay) / 4.0))
            for i in range(steps):
                f = i / steps
                pts.append((ax + (bx - ax) * f, ay + (by - ay) * f, t))
                t += 1.0
    return pts


def _tracks(label="person", jitter=0.0):
    """Capture-window-shaped tracks of the walk as each TRUE camera saw it."""
    tracks = []
    for cam in TRUE:
        points = []
        for k, (wx, wy, t) in enumerate(_walk_points()):
            pt = _image_point(cam, wx, wy)
            if pt is None:
                continue
            j = jitter * math.sin(k * 1.7)
            points.append([pt[0] + j, pt[1] + j, t])
        if len(points) >= 2:
            tracks.append({
                "camera": cam, "track_id": "walk-" + cam,
                "label": label, "points": points,
            })
    return tracks


def test_mining_yields_pairs_per_camera_pair():
    settings = _settings()
    pairs, counts, warnings = calibrate.mine_pairs(_tracks(), settings)
    assert pairs
    assert all(n >= 20 for n in counts.values())
    assert len(counts) >= 2  # at least two camera pairs constrained


def test_mining_rejects_label_mismatch():
    settings = _settings()
    tracks = _tracks()
    tracks[0]["label"] = "car"
    pairs, counts, _ = calibrate.mine_pairs(tracks, settings)
    cam0 = tracks[0]["camera"]
    assert all(cam0 not in key.split("|") for key in counts)


def test_mining_rejects_time_disjoint_tracks():
    settings = _settings()
    tracks = _tracks()
    # Shift one camera's clock far away: no overlap, no pairs with it.
    shifted = tracks[0]["camera"]
    tracks[0]["points"] = [[x, y, t + 9999] for x, y, t in tracks[0]["points"]]
    _, counts, _ = calibrate.mine_pairs(tracks, settings)
    assert all(shifted not in key.split("|") for key in counts)


def test_mining_respects_per_trackpair_cap():
    settings = _settings()
    pairs, _, _ = calibrate.mine_pairs(_tracks(), settings, max_pairs_per_trackpair=5)
    from collections import Counter
    per = Counter((p[0], p[2]) for p in pairs)
    assert all(n <= 5 for n in per.values())


def test_objective_prefers_truth_over_perturbed():
    perturb = {"north": (6.0, -3.0), "east": (-4.0, 2.0), "west": (3.0, -1.5)}
    settings = _settings(perturb)
    pairs, _, _ = calibrate.mine_pairs(_tracks(), settings)
    layout = settings["camera_layout"]
    base = {
        c: (layout[c]["azimuth"], settings["camera_optics"][c]["tilt_deg"])
        for c in TRUE
    }
    truth = {c: (TRUE[c]["azimuth"], TRUE[c]["tilt"]) for c in TRUE}
    assert (
        calibrate.objective(truth, pairs, settings, base=base)
        < calibrate.objective(base, pairs, settings, base=base)
    )


def test_fit_recovers_true_aims():
    perturb = {"north": (6.0, -3.0), "east": (-4.0, 2.0), "west": (3.0, -1.5)}
    settings = _settings(perturb)
    pairs, _, _ = calibrate.mine_pairs(_tracks(), settings)
    report = calibrate.fit(pairs, settings)
    assert report["rms_after_ft"] < 2.0
    assert report["rms_after_ft"] < report["rms_before_ft"] / 3
    for cam, entry in report["cameras"].items():
        d_az = abs((entry["azimuth_after"] - TRUE[cam]["azimuth"] + 180) % 360 - 180)
        assert d_az < 1.0, (cam, entry)
        assert abs(entry["tilt_after"] - TRUE[cam]["tilt"]) < 1.0, (cam, entry)


def test_fit_survives_mismatched_pairs():
    # 10% of pairs come from a second walker elsewhere: Huber keeps the
    # fit within 1.5 deg of truth.
    perturb = {"north": (6.0, -3.0), "east": (-4.0, 2.0), "west": (3.0, -1.5)}
    settings = _settings(perturb)
    pairs, _, _ = calibrate.mine_pairs(_tracks(), settings)
    bogus = []
    for k in range(len(pairs) // 10):
        cam_a, pt_a, cam_b, _pt_b = pairs[k * 10]
        bogus.append((cam_a, pt_a, cam_b, (0.2 + 0.05 * (k % 5), 0.85)))
    report = calibrate.fit(pairs + bogus, settings)
    for cam, entry in report["cameras"].items():
        d_az = abs((entry["azimuth_after"] - TRUE[cam]["azimuth"] + 180) % 360 - 180)
        assert d_az < 1.5, (cam, entry)
        assert abs(entry["tilt_after"] - TRUE[cam]["tilt"]) < 1.5, (cam, entry)


def test_fit_leaves_unpaired_camera_untouched():
    perturb = {"north": (6.0, -3.0), "east": (-4.0, 2.0)}
    settings = _settings(perturb)
    tracks = [t for t in _tracks() if t["camera"] != "west"]
    pairs, _, _ = calibrate.mine_pairs(tracks, settings)
    report = calibrate.fit(pairs, settings)
    assert "west" not in report["cameras"]


def test_facts_for_missing_camera_is_none():
    assert calibrate.facts_for(_settings(), "nope") is None
    assert ground.camera_ground is not None  # module wiring sanity


# ---- POST /v1/push/map/autotune ------------------------------------------


def _autotune_client(tmp_path, frigate_db_path, sidecar_db_path, capture_rows):
    import json

    from fastapi.testclient import TestClient

    from marcellus.config import (
        FrigateSection,
        PushSection,
        Settings,
        SidecarSection,
    )
    from marcellus.server import create_app

    capture_file = tmp_path / "mqtt-capture.jsonl"
    capture_file.write_text("\n".join(json.dumps(r) for r in capture_rows))
    (tmp_path / "frigate-config.yml").write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=tmp_path / "frigate-config.yml", db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp_path / "push_settings.json"),
            capture_path=str(capture_file),
        ),
    )
    return TestClient(create_app(settings))


def _capture_rows(now):
    """The synthetic walk as frigate/events capture rows, shifted to `now`."""
    rows = []
    shift = now - 1050.0  # walk timestamps start at 1000.0
    for track in _tracks():
        pts = [[x, y, t + shift] for x, y, t in track["points"]]
        rows.append({
            "ts": pts[-1][2], "topic": "frigate/events",
            "payload": {"type": "update", "after": {
                "camera": track["camera"], "id": track["track_id"],
                "label": track["label"], "path_data": pts,
            }},
        })
    return rows


def test_autotune_endpoint_reports(
    tmp_path, frigate_db_path, sidecar_db_path,
):
    import time

    from marcellus.push import policy_settings

    policy_settings.reset_for_tests()
    try:
        client = _autotune_client(
            tmp_path, frigate_db_path, sidecar_db_path, _capture_rows(time.time()),
        )
        perturb = {"north": (6.0, -3.0), "east": (-4.0, 2.0), "west": (3.0, -1.5)}
        doc = dict(policy_settings.get_active())
        doc.update(_settings(perturb))
        policy_settings.apply_settings(doc)
        body = client.post("/v1/push/map/autotune?minutes=60").json()
        report = body["report"]
        assert report["rms_after_ft"] < report["rms_before_ft"]
        assert set(report["cameras"]) == set(TRUE)
        assert report["pair_counts"]
        assert body["elapsed_s"] >= 0
    finally:
        policy_settings.reset_for_tests()


def test_autotune_endpoint_400_on_empty_capture(
    tmp_path, frigate_db_path, sidecar_db_path,
):
    from marcellus.push import policy_settings

    policy_settings.reset_for_tests()
    try:
        client = _autotune_client(tmp_path, frigate_db_path, sidecar_db_path, [])
        resp = client.post("/v1/push/map/autotune")
        assert resp.status_code == 400
        assert "cross-camera" in resp.json()["detail"]["message"]
    finally:
        policy_settings.reset_for_tests()


# ---- solve_landmarks ------------------------------------------------------


def _landmark_matches(cam, world_pts):
    out = []
    for wx, wy in world_pts:
        pt = _image_point(cam, wx, wy)
        assert pt is not None, (cam, wx, wy)
        out.append({"u": pt[0], "v": pt[1], "mx": wx / SCALE_FT, "my": wy / SCALE_FT})
    return out


NORTH_LANDMARKS = [
    (0.42 * SCALE_FT, 0.34 * SCALE_FT), (0.58 * SCALE_FT, 0.32 * SCALE_FT),
    (0.50 * SCALE_FT, 0.45 * SCALE_FT), (0.60 * SCALE_FT, 0.48 * SCALE_FT),
]


def test_solve_landmarks_recovers_hfov_azimuth_tilt():
    settings = _settings({"north": (8.0, -4.0)})
    settings["camera_optics"]["north"]["hfov"] = 105.0  # true is 90
    settings["camera_optics"]["north"]["vfov"] = None
    del settings["camera_optics"]["north"]["vfov"]
    matches = _landmark_matches("north", NORTH_LANDMARKS)
    report = calibrate.solve_landmarks("north", matches, settings)
    assert abs(report["hfov_after"] - HFOV) < 2.0, report
    d_az = abs((report["azimuth_after"] - TRUE["north"]["azimuth"] + 180) % 360 - 180)
    assert d_az < 1.0, report
    assert abs(report["tilt_after"] - TRUE["north"]["tilt"]) < 1.0, report
    assert report["rms_ft"] < 1.0, report
    # Ground coverage span for the map wedge: wider than the lens HFOV
    # whenever the camera is tilted (atan(tan(hfov/2)/cos(tilt)) per side).
    expected_wedge = 2 * math.degrees(math.atan(
        math.tan(math.radians(report["hfov_after"]) / 2)
        / math.cos(math.radians(report["tilt_after"]))
    ))
    assert abs(report["wedge_fov_after"] - expected_wedge) < 0.1
    assert report["wedge_fov_after"] > report["hfov_after"]


def test_solve_landmarks_scales_vendor_vfov():
    settings = _settings({"north": (8.0, -4.0)})
    settings["camera_optics"]["north"]["hfov"] = 105.0
    # Vendor vfov consistent with the TRUE rig at hfov 105 would be wrong;
    # here the vendor value equals the 16:9 derivation at the wrong hfov,
    # so after solving back to ~90 the returned vfov must shrink with it.
    matches = _landmark_matches("north", NORTH_LANDMARKS)
    report = calibrate.solve_landmarks("north", matches, settings)
    assert report["vfov_after"] is not None
    assert report["vfov_after"] < settings["camera_optics"]["north"]["vfov"]


def test_solve_landmarks_requires_two_matches():
    import pytest

    settings = _settings()
    with pytest.raises(ValueError):
        calibrate.solve_landmarks(
            "north", _landmark_matches("north", NORTH_LANDMARKS[:1]), settings,
        )


def test_landmark_solve_endpoint(tmp_path, frigate_db_path, sidecar_db_path):
    from marcellus.push import policy_settings

    policy_settings.reset_for_tests()
    try:
        client = _autotune_client(tmp_path, frigate_db_path, sidecar_db_path, [])
        doc = dict(policy_settings.get_active())
        settings = _settings({"north": (8.0, -4.0)})
        settings["camera_optics"]["north"]["hfov"] = 105.0
        del settings["camera_optics"]["north"]["vfov"]
        doc.update(settings)
        policy_settings.apply_settings(doc)
        matches = _landmark_matches("north", NORTH_LANDMARKS)
        resp = client.post(
            "/v1/push/map/landmark-solve",
            json={"camera": "north", "matches": matches},
        )
        assert resp.status_code == 200, resp.text
        report = resp.json()
        assert abs(report["hfov_after"] - HFOV) < 2.0
        assert len(report["residual_ft"]) == len(matches)
        # Bad requests surface as 400s, not 500s.
        assert client.post(
            "/v1/push/map/landmark-solve",
            json={"camera": "north", "matches": matches[:1]},
        ).status_code == 400
        assert client.post(
            "/v1/push/map/landmark-solve",
            json={"camera": "nope", "matches": matches},
        ).status_code == 400
    finally:
        policy_settings.reset_for_tests()
