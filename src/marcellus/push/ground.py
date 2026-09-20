"""Ground-plane projection: image coordinates -> real-world feet.

Built entirely from the settings-backed per-camera rig facts (measured HFOV,
mount height, tilt — `camera_optics`, onboarded/edited on the /cameras
page) — no zone dimension measurements required.
A first-order pinhole model over flat ground: accurate enough (±25%) to
separate walking from running and to place tracks on the layout map, and
honest about its limits — anything near the horizon or beyond
_MAX_FORWARD_FT projects to None instead of a wild number.

Frigate's built-in speed estimation (zone `distances`) stays the upgrade
path if zones ever get measured; this module exists so speed works today.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

from marcellus.analysis import optics
from marcellus.push import policy_settings

#: Beyond this forward distance the depression angle is so shallow that a
#: one-pixel error swings the estimate by tens of feet — refuse to guess.
_MAX_FORWARD_FT = 150.0
#: Minimum depression angle (degrees) for a usable projection.
_MIN_DEPRESSION_DEG = 1.0
#: Speed bins (ft/s). Brisk walk ~4.4 ft/s, slow jog ~7.5 ft/s.
RUNNING_FT_S = 7.0
WALKING_FT_S = 1.5
#: Segments must span at least this much time for a stable estimate.
_MIN_SEGMENT_DT_S = 0.4

_DETECT_ASPECT = (16, 9)


def camera_ground(camera: str) -> dict[str, float] | None:
    """Per-camera projection facts from the settings-backed `camera_optics`
    table (onboarded on /cameras), or None when the camera hasn't been
    onboarded or lacks the needed numbers. Reads the live policy per call,
    so an optics edit applies on the next event with no restart."""
    facts = policy_settings.camera_optics(camera)
    if not facts:
        return None
    hfov, mount, tilt = facts.get("hfov"), facts.get("mount_ft"), facts.get("tilt_deg")
    if not hfov or not mount or tilt is None:
        return None
    vfov = facts.get("vfov")
    if not vfov:
        vfov = optics.vfov_from_hfov(float(hfov), *_DETECT_ASPECT)
    return {
        "hfov": float(hfov),
        "mount_ft": float(mount),
        "tilt_deg": float(tilt),
        "vfov": float(vfov),
    }


def project(
    x_norm: float, y_norm: float, facts: dict[str, float],
) -> tuple[float, float] | None:
    """Image point -> (forward_ft, lateral_ft) in the camera's ground frame.
    Forward is along the optical axis' ground projection; lateral is
    camera-right. None when the point sits at/above the effective horizon
    or projects past _MAX_FORWARD_FT.

    Full pinhole ray/ground intersection. The earlier first-order version
    (depression linear in v, lateral scaled by ground forward) understated
    lateral by a factor of ~1/cos(tilt) and bent rows near the frame edge —
    at a 45° tilt that's a 30%+ error, which made landmark calibration
    report large residuals on correct clicks."""
    # Image-plane offsets, normalized to a focal length of 1.
    a = (x_norm - 0.5) * 2.0 * math.tan(math.radians(facts["hfov"]) / 2.0)
    b = (y_norm - 0.5) * 2.0 * math.tan(math.radians(facts["vfov"]) / 2.0)
    t = math.radians(facts["tilt_deg"])
    # Ray direction: down-component and horizontal forward-component of the
    # optical axis tilted down by t, with the image offset rotated along.
    down = math.sin(t) + b * math.cos(t)
    horiz = math.cos(t) - b * math.sin(t)
    depression = math.degrees(math.atan2(down, horiz))
    if depression < _MIN_DEPRESSION_DEG:
        return None
    s = facts["mount_ft"] / down  # ray parameter where it meets the ground
    forward = s * horiz
    if forward > _MAX_FORWARD_FT or forward < 0:
        return None
    return (forward, s * a)


def speed_ft_s(
    path_data: list[tuple[float, float, float]] | tuple[tuple[float, float, float], ...],
    camera: str,
) -> float | None:
    """Median ground speed over the trail's recent usable segments. None
    when the camera lacks projection facts, points lack timestamps, or no
    segment spans enough time with both endpoints projectable."""
    facts = camera_ground(camera)
    if facts is None or not path_data or len(path_data) < 2:
        return None
    pts = [p for p in path_data if len(p) >= 3 and p[2] > 0]
    if len(pts) < 2:
        return None
    speeds: list[float] = []
    # Walk backward pairing each point with the nearest earlier point that
    # gives a >= _MIN_SEGMENT_DT_S span; up to 3 segments, newest first.
    i = len(pts) - 1
    while i > 0 and len(speeds) < 3:
        j = i - 1
        while j >= 0 and pts[i][2] - pts[j][2] < _MIN_SEGMENT_DT_S:
            j -= 1
        if j < 0:
            break
        a, b = pts[j], pts[i]
        ga, gb = project(a[0], a[1], facts), project(b[0], b[1], facts)
        dt = b[2] - a[2]
        if ga is not None and gb is not None and dt > 0:
            dist = math.hypot(gb[0] - ga[0], gb[1] - ga[1])
            speeds.append(dist / dt)
        i = j
    if not speeds:
        return None
    return statistics.median(speeds)


def speed_label(ft_s: float | None) -> str | None:
    """Honest bins only — no numeric mph from a ±25% model."""
    if ft_s is None:
        return None
    if ft_s >= RUNNING_FT_S:
        return "running"
    if ft_s >= WALKING_FT_S:
        return "walking"
    return None


def world_position(
    x_norm: float, y_norm: float, *,
    camera: str,
    layout_entry: dict[str, float],
    scale_ft: float,
    aspect_h_over_w: float = 1.0,
) -> tuple[float, float] | None:
    """Image point -> layout-map coordinates (0..1-ish space, may exceed
    the map edges). Requires the camera's map position + pie azimuth and
    the map's real-world width (`map_scale_ft`). `aspect_h_over_w` is the
    map's height/width ratio (an uploaded floorplan is rarely square): the
    map's real height is `scale_ft * aspect`, so normalized y-feet convert
    at that rate."""
    facts = camera_ground(camera)
    azimuth = layout_entry.get("azimuth")
    if facts is None or azimuth is None or not scale_ft or scale_ft <= 0:
        return None
    if not aspect_h_over_w or aspect_h_over_w <= 0:
        aspect_h_over_w = 1.0
    g = project(x_norm, y_norm, facts)
    if g is None:
        return None
    forward, lateral = g
    rad = math.radians(azimuth)
    # Map coords y-down, compass 0 = north = -y: view = (sin, -cos),
    # camera-right = (cos, sin).
    dx = (forward * math.sin(rad) + lateral * math.cos(rad)) / scale_ft
    dy = (forward * -math.cos(rad) + lateral * math.sin(rad)) / (scale_ft * aspect_h_over_w)
    return (layout_entry.get("x", 0.0) + dx, layout_entry.get("y", 0.0) + dy)


def distance_to_secure_ft(
    x: float, y: float, secure_area: dict[str, float] | None,
    *, scale_ft: float | None, aspect_h_over_w: float = 1.0,
) -> float | None:
    """Distance in feet from a map point to the secure-area rectangle.

    0.0 inside the rectangle; None when the secure area or map scale is
    missing. Map coords are normalized with y scaled by the floorplan
    aspect, so the y-leg converts through scale·aspect.
    """
    if not isinstance(secure_area, dict) or not scale_ft or scale_ft <= 0:
        return None
    try:
        x0, x1 = sorted((float(secure_area["x0"]), float(secure_area["x1"])))
        y0, y1 = sorted((float(secure_area["y0"]), float(secure_area["y1"])))
    except (KeyError, TypeError, ValueError):
        return None
    if not aspect_h_over_w or aspect_h_over_w <= 0:
        aspect_h_over_w = 1.0
    dx = max(x0 - x, 0.0, x - x1)
    dy = max(y0 - y, 0.0, y - y1)
    return math.hypot(dx * scale_ft, dy * scale_ft * aspect_h_over_w)


def map_aspect(settings: dict[str, Any]) -> float:
    """The layout map's height/width ratio: the uploaded floorplan's pixel
    aspect when one is set, else 1.0 (the square default map)."""
    fp = settings.get("floorplan")
    if isinstance(fp, dict) and fp.get("w") and fp.get("h"):
        return float(fp["h"]) / float(fp["w"])
    return 1.0
