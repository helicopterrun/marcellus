"""Optics & placement math: focal length -> HFOV -> object pixels at distance.

Pure functions (no I/O) plus camera-model, lens, and object presets, so the
placement-planner page (`routes/placement.py` + `static/js/placement.js`) and
its tests share one source of truth. The JS re-implements the same formulas
client-side for live interactivity; `tests/test_optics.py` pins the Python
side so the two can't silently drift.

Model notes
-----------
* HFOV is a rectilinear pinhole model: ``HFOV = 2*atan(W / (2*f))``. Real
  wide-angle lenses distort a few degrees wider than this, so deployed cameras
  carry a *measured* HFOV that overrides the computed value in the UI.
* Varifocal lenses are calibrated to a two-point fit (wide + tele datasheet
  anchors) via :func:`fit_varifocal`, which solves an effective sensor width
  ``W`` and focal offset ``f0`` so the curve passes through *both* anchors
  exactly. This is more accurate across the zoom range than a single nominal
  sensor width, because published CCTV FOV figures bake in some distortion.
* Pixel math keys on the object's horizontal *width*; ``area_px2`` is derived
  as ``width_px**2 * aspect`` (aspect = height/width) so it is directly
  comparable to Frigate's ``objects.filters.<obj>.min_area``.
"""

from __future__ import annotations

import math
from typing import Any

# --------------------------------------------------------------------------
# Core geometry
# --------------------------------------------------------------------------


def hfov_from_focal(focal_mm: float, sensor_width_mm: float, focal_offset_mm: float = 0.0) -> float:
    """Horizontal field of view in degrees for a rectilinear lens.

    ``focal_offset_mm`` is the fitted offset for varifocal lenses (0 for an
    ideal pinhole). See :func:`fit_varifocal`.
    """
    return math.degrees(2 * math.atan(sensor_width_mm / (2 * (focal_mm + focal_offset_mm))))


def focal_from_hfov(hfov_deg: float, sensor_width_mm: float, focal_offset_mm: float = 0.0) -> float:
    """Inverse of :func:`hfov_from_focal` — focal length that yields ``hfov_deg``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return sensor_width_mm / (2 * half) - focal_offset_mm


def fit_varifocal(anchors: list[tuple[float, float]]) -> tuple[float, float]:
    """Solve ``(W, f0)`` so ``HFOV(f) = 2*atan(W / (2*(f + f0)))`` passes through
    both ``(focal_mm, hfov_deg)`` anchor points exactly.

    Returns ``(sensor_width_mm, focal_offset_mm)``.
    """
    (f1, h1), (f2, h2) = anchors
    t1 = math.tan(math.radians(h1) / 2)
    t2 = math.tan(math.radians(h2) / 2)
    # W = 2*t1*(f1 + f0) = 2*t2*(f2 + f0)  ->  f0*(t1 - t2) = t2*f2 - t1*f1
    f0 = (t2 * f2 - t1 * f1) / (t1 - t2)
    w = 2 * t1 * (f1 + f0)
    return w, f0


def px_per_ft(det_w_px: int, hfov_deg: float, distance_ft: float) -> float:
    """Horizontal pixels covering one foot at ``distance_ft``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return det_w_px / (2 * distance_ft * half)


def object_px_width(
    real_width_ft: float, det_w_px: int, hfov_deg: float, distance_ft: float
) -> float:
    """On-screen width in pixels of an object ``real_width_ft`` wide at ``distance_ft``."""
    return real_width_ft * px_per_ft(det_w_px, hfov_deg, distance_ft)


def max_distance_ft(
    real_width_ft: float, det_w_px: int, hfov_deg: float, target_px: float
) -> float:
    """Farthest distance (ft) at which the object's width still meets ``target_px``."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return real_width_ft * det_w_px / (2 * target_px * half)


def target_area_px2(target_px_width: float, aspect_h_to_w: float) -> float:
    """Bounding-box area in px^2 for a ``target_px_width``-wide box of given aspect.

    Comparable to Frigate ``objects.filters.<obj>.min_area``.
    """
    return target_px_width * target_px_width * aspect_h_to_w


# --------------------------------------------------------------------------
# Vertical geometry: mount height + down-angle
# --------------------------------------------------------------------------
#
# Side-elevation model. The camera sits at ``height_ft`` and looks down at
# ``tilt_deg`` (depression of the optical axis below horizontal). Bounding-box
# *height* in pixels comes from the angular subtension between an object's feet
# and head — which, unlike the width*aspect shortcut, depends on mount height
# and viewing angle. This is what makes a high steep camera capture tops of
# heads rather than faces even when the raw pixel count looks fine.

FT_PER_M = 3.280839895

# DORI (EN 62676-4) scene pixel-density thresholds, in px per metre of real
# horizontal width. Frigate's face-recognition docs point at the camera's DORI
# *Identification* range as the realistic upper bound for recognition.
DORI_PX_PER_M = {
    "detection": 25.0,
    "observation": 63.0,
    "recognition": 125.0,
    "identification": 250.0,
}

# Frigate-published references surfaced in the planner UI.
FACE_MIN_AREA_PX2 = 1000  # this deployment's face_recognition.min_area (Frigate default is 500)
# Empirical: confident recognitions (recog_score >= 0.9) cluster at >= ~3000 px2
# across 163 captured face attempts (2026-06). Below this, a face may be saved
# but rarely recognized — a better target than the generic 80px rule of thumb.
FACE_RECOG_FLOOR_PX2 = 3000
MODEL_INPUT_PX = 320  # object detector input is letterboxed to 320x320


def vfov_from_hfov(hfov_deg: float, det_w_px: int, det_h_px: int) -> float:
    """Vertical FOV from horizontal FOV and the detect frame's aspect ratio."""
    half_h = math.tan(math.radians(hfov_deg) / 2)
    return math.degrees(2 * math.atan(half_h * det_h_px / det_w_px))


def ground_coverage(
    height_ft: float, tilt_deg: float, vfov_deg: float
) -> tuple[float, float | None]:
    """Near and far ground distances (ft) the vertical FOV covers.

    ``tilt_deg`` is the optical-axis depression below horizontal. Returns
    ``(near_ft, far_ft)``; ``far_ft`` is ``None`` when the top ray is at or
    above the horizon (the camera sees sky -> unbounded far edge).
    """
    top_dep = tilt_deg - vfov_deg / 2  # smallest depression -> farthest
    bot_dep = tilt_deg + vfov_deg / 2  # largest depression -> nearest

    def hit(dep_deg: float) -> float | None:
        if dep_deg <= 0:
            return None  # ray at/above horizon
        if dep_deg >= 90:
            return 0.0  # ray points straight down / behind the mast
        return height_ft / math.tan(math.radians(dep_deg))

    near = hit(bot_dep)
    far = hit(top_dep)
    return (near if near is not None else 0.0, far)


def bbox_height_px(
    obj_bottom_ft: float,
    obj_top_ft: float,
    cam_height_ft: float,
    distance_ft: float,
    vfov_deg: float,
    det_h_px: int,
) -> float:
    """On-screen bounding-box height (px) of an object spanning ``obj_bottom_ft``
    to ``obj_top_ft`` off the ground, via the angular subtension between those
    two heights — accounts for foreshortening with viewing angle. A ground-
    standing person is ``obj_bottom_ft=0, obj_top_ft=height``; a face at eye
    level is a short span centred on the eye height.
    """
    dep_bottom = math.atan((cam_height_ft - obj_bottom_ft) / distance_ft)
    dep_top = math.atan((cam_height_ft - obj_top_ft) / distance_ft)
    angular = dep_bottom - dep_top  # radians, positive (lower point sits lower in frame)
    return angular / math.radians(vfov_deg) * det_h_px


def face_depression_deg(cam_height_ft: float, distance_ft: float, eye_height_ft: float) -> float:
    """Depression angle (deg) of the camera's line of sight to a subject's eyes.

    Negative = camera below eye level (looking up -> good frontal capture);
    large positive = steep down-angle -> tops of heads.
    """
    return math.degrees(math.atan((cam_height_ft - eye_height_ft) / distance_ft))


def dori_distance_ft(det_w_px: int, hfov_deg: float, px_per_m: float) -> float:
    """Distance (ft) at which scene density drops to ``px_per_m`` px per metre."""
    half = math.tan(math.radians(hfov_deg) / 2)
    return det_w_px * FT_PER_M / (2 * px_per_m * half)


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------

# Lens / camera-model catalogue. Varifocal lenses give two datasheet anchors
# (wide, tele); fixed lenses give a single nominal HFOV. ``custom`` lets the
# user drive HFOV directly. Resolutions are offered separately so a model can
# be evaluated at either its detect-substream or main-stream width.
_LENSES: list[dict[str, Any]] = [
    {
        "id": "dahua-5442-vf",
        "label": "Dahua/EmpireTech 5442 varifocal 2.7–12mm (1/1.8\")",
        "type": "varifocal",
        "focal_min": 2.7,
        "focal_max": 12.0,
        # Datasheet horizontal FOV: 114° @ 2.7mm, 47° @ 12mm.
        "anchors": [[2.7, 114.0], [12.0, 47.0]],
        "note": "ZEB fleet: gate, garden, alley-wide, stairway-tight, stairway-wide.",
    },
    {
        "id": "dahua-5442-fixed28",
        "label": "Dahua 5442 fixed 2.8mm (1/1.8\")",
        "type": "fixed",
        "hfov": 108.0,
        "note": "walkway (HDW5442TP-AS). Lens mm unconfirmed — measure to verify.",
    },
    {
        "id": "dahua-t2431-fixed28",
        "label": "Dahua T2431 fixed 2.8mm (1/2.7\")",
        "type": "fixed",
        "hfov": 106.0,
        "note": "street. Deployed instance measures ~115°.",
    },
    {
        "id": "unifi-g4-doorbell",
        "label": "UniFi G4 Doorbell Pro",
        "type": "fixed",
        "hfov": 138.0,  # main camera: H 138°, V 114°, D 155°
    },
    {
        "id": "unifi-g4-instant",
        "label": "UniFi G4 Instant",
        "type": "fixed",
        "hfov": 97.5,  # H 97.5°, V 79.4°, D 118.2°
    },
    {
        "id": "unifi-g5-flex",
        "label": "UniFi G5 Flex",
        "type": "fixed",
        "hfov": 102.0,
        "note": "crows-nest deployed instance measures ~70°.",
    },
    {
        "id": "custom",
        "label": "Custom (enter HFOV directly)",
        "type": "custom",
    },
]

# Common detect/main stream widths in the fleet.
_RESOLUTIONS: list[dict[str, Any]] = [
    {"id": "sub-720", "label": "1280×720 (detect substream)", "w": 1280, "h": 720},
    {"id": "main-1520", "label": "2688×1520 (Dahua main)", "w": 2688, "h": 1520},
    {"id": "fhd-1080", "label": "1920×1080 (street main)", "w": 1920, "h": 1080},
    {"id": "doorbell-720", "label": "960×720 (doorbell)", "w": 960, "h": 720},
    {"id": "main-960", "label": "1280×960 (package 4:3 main)", "w": 1280, "h": 960},
]

# Object catalogue: real_width_ft, aspect (height/width), and a target minimum
# on-screen width in px for reliable detection/recognition. Rules of thumb —
# tune against real event data later (frigate.db calibration is a follow-up).
_OBJECTS: list[dict[str, Any]] = [
    {"id": "face", "label": "face (recognition)", "width_ft": 0.5, "aspect": 1.4, "target_px": 80},
    {"id": "person", "label": "person", "width_ft": 1.6, "aspect": 3.4, "target_px": 40},
    {"id": "car", "label": "car (front/rear)", "width_ft": 6.0, "aspect": 0.8, "target_px": 50},
    {"id": "bicycle", "label": "bicycle + rider", "width_ft": 1.8, "aspect": 1.2, "target_px": 45},
    {"id": "motorcycle", "label": "motorcycle", "width_ft": 2.0, "aspect": 1.3, "target_px": 45},
    {"id": "package", "label": "package", "width_ft": 1.0, "aspect": 0.9, "target_px": 30},
    {"id": "cat", "label": "cat / raccoon", "width_ft": 1.3, "aspect": 0.55, "target_px": 35},
]

# Deployed fleet at the time optics moved into settings — a ONE-TIME SEED for
# the settings-backed `camera_optics` table (policy_settings.startup seeds it
# when the key is absent), not consulted at runtime after seeding. The
# /cameras page owns the live values now; edit there, not here. ``hfov`` is
# the *measured* value (from the spatial reference). ``res`` selects the
# operative detect stream. ``vfov`` is carried only where the vendor publishes
# it (UniFi). ``tilt_deg`` (optical-axis depression below horizontal) was a
# rough estimate.
DEPLOYMENT_SEED: list[dict[str, Any]] = [
    {"id": "street", "lens": "dahua-t2431-fixed28", "hfov": 115, "res": "fhd-1080",
     "mount_ft": 35, "tilt_deg": 22, "faces": "S", "face_rec": False,
     "note": "high mount, steep angle"},
    {"id": "gate", "lens": "dahua-5442-vf", "hfov": 80, "res": "sub-720",
     "mount_ft": 10, "tilt_deg": 12, "faces": "SE", "face_rec": False},
    {"id": "garden", "lens": "dahua-5442-vf", "hfov": 80, "res": "sub-720",
     "mount_ft": 7, "tilt_deg": 10, "faces": "SW", "face_rec": False},
    {"id": "doorbell", "lens": "unifi-g4-doorbell", "hfov": 138, "vfov": 114, "res": "doorbell-720",
     "mount_ft": 3, "tilt_deg": 6, "faces": "S", "face_rec": True},
    {"id": "package", "lens": "unifi-g4-instant", "hfov": 97.5, "vfov": 79.4, "res": "main-960",
     "mount_ft": 3, "tilt_deg": 45, "faces": "S", "face_rec": False},
    {"id": "alley-wide", "lens": "dahua-5442-vf", "hfov": 115, "res": "sub-720",
     "mount_ft": 45, "tilt_deg": 28, "faces": "N", "face_rec": False,
     "note": "high mount, steep angle"},
    {"id": "stairway-tight", "lens": "dahua-5442-vf", "hfov": 90, "res": "sub-720",
     "mount_ft": 10, "tilt_deg": 12, "faces": "N", "face_rec": True},
    {"id": "stairway-wide", "lens": "dahua-5442-vf", "hfov": 90, "res": "sub-720",
     "mount_ft": 10, "tilt_deg": 12, "faces": "NW", "face_rec": False},
    {"id": "walkway", "lens": "dahua-5442-fixed28", "hfov": 108, "res": "sub-720",
     "mount_ft": 10, "tilt_deg": 8, "faces": "E", "face_rec": False,
     "note": "HFOV estimated — confirm"},
    {"id": "crows-nest", "lens": "unifi-g5-flex", "hfov": 70, "res": "sub-720",
     "mount_ft": 55, "tilt_deg": 2, "faces": "W", "face_rec": False,
     "note": "animal-only; high mount"},
]


def _lens_with_fit(lens: dict[str, Any]) -> dict[str, Any]:
    """Attach fitted ``W``/``f0`` to a varifocal lens so JS can evaluate HFOV(f)."""
    out = dict(lens)
    if lens["type"] == "varifocal":
        w, f0 = fit_varifocal([tuple(a) for a in lens["anchors"]])
        out["sensor_width_mm"] = round(w, 4)
        out["focal_offset_mm"] = round(f0, 4)
    return out


def presets_payload() -> dict[str, Any]:
    """JSON-serializable bundle injected into the placement template for the JS."""
    return {
        "lenses": [_lens_with_fit(lo) for lo in _LENSES],
        "resolutions": _RESOLUTIONS,
        "objects": _OBJECTS,
        "cameras": DEPLOYMENT_SEED,
        "dori": DORI_PX_PER_M,
        "refs": {
            "face_min_area_px2": FACE_MIN_AREA_PX2,
            "face_recog_floor_px2": FACE_RECOG_FLOOR_PX2,
            "model_input_px": MODEL_INPUT_PX,
            "ft_per_m": FT_PER_M,
        },
    }
