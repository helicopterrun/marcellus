"""Auto-tune camera aim from replayed capture history.

When two cameras saw the same object at the same instant, their map
projections should coincide. The flight recorder holds hours of
`path_data` on one clock, so replaying it yields thousands of such
constraint pairs; minimizing their disagreement tunes each camera's
azimuth and tilt — the two eyeballed numbers. Positions, mount heights,
and HFOVs are measured facts and stay fixed.

Pure functions over a settings document and capture-window-shaped
tracks; no I/O, no live policy reads — the caller passes the doc in.
The optimizer is cyclic coordinate descent with a golden-section line
search: 2 params/camera and a few thousand pairs need nothing heavier,
and the repo carries no numpy/scipy in core deps by design.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from marcellus.analysis import optics
from marcellus.push import ground

#: A constraint: (cam_a, (x,y)_a, cam_b, (x,y)_b) — image points captured
#: at the "same" instant.
Pair = tuple[str, tuple[float, float], str, tuple[float, float]]
#: camera -> (azimuth_deg, tilt_deg) overrides.
Params = dict[str, tuple[float, float]]

_DETECT_ASPECT = (16, 9)
#: Interpolating across a longer gap fabricates positions.
_MAX_BRACKET_S = 1.5
#: Consecutive kept pairs must differ this much (ft) in either projection —
#: a parked car generating hundreds of identical pairs adds no information.
_MIN_MOTION_FT = 3.0
#: Trial params that push a point off the projectable range are charged as
#: a fixed miss of this size, so the fit can't win by hiding points.
_OFFRANGE_MISS_FT = 30.0
_GOLDEN = (math.sqrt(5) - 1) / 2


def facts_for(settings: dict[str, Any], camera: str) -> dict[str, float] | None:
    """`ground.camera_ground` equivalent reading a passed-in doc."""
    entry = (settings.get("camera_optics") or {}).get(camera)
    if not entry:
        return None
    hfov, mount, tilt = entry.get("hfov"), entry.get("mount_ft"), entry.get("tilt_deg")
    if not hfov or not mount or tilt is None:
        return None
    vfov = entry.get("vfov")
    if not vfov:
        vfov = optics.vfov_from_hfov(float(hfov), *_DETECT_ASPECT)
    return {
        "hfov": float(hfov), "mount_ft": float(mount),
        "tilt_deg": float(tilt), "vfov": float(vfov),
    }


def _tunable_cameras(settings: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Cameras with optics facts AND an aimed layout entry: camera -> facts."""
    layout = settings.get("camera_layout") or {}
    out: dict[str, dict[str, float]] = {}
    for camera, entry in layout.items():
        if entry.get("azimuth") is None:
            continue
        facts = facts_for(settings, camera)
        if facts is not None:
            out[camera] = facts
    return out


def world_ft(
    pt: tuple[float, float], camera: str, params: Params, settings: dict[str, Any],
    facts: dict[str, float] | None = None,
) -> tuple[float, float] | None:
    """Image point → map position in FEET under (azimuth, tilt) overrides.

    Same math as `ground.world_position`, but in feet (interpretable
    objective, no aspect-scaled anisotropy) and parameterized so the
    optimizer can try trial aims without touching the settings doc.
    """
    if facts is None:
        facts = facts_for(settings, camera)
    layout = (settings.get("camera_layout") or {}).get(camera)
    scale_ft = settings.get("map_scale_ft")
    if facts is None or layout is None or not scale_ft or scale_ft <= 0:
        return None
    azimuth, tilt = params.get(camera, (layout.get("azimuth"), facts["tilt_deg"]))
    if azimuth is None:
        return None
    g = ground.project(pt[0], pt[1], {**facts, "tilt_deg": tilt})
    if g is None:
        return None
    forward, lateral = g
    rad = math.radians(azimuth)
    aspect = ground.map_aspect(settings)
    return (
        layout.get("x", 0.0) * scale_ft
        + forward * math.sin(rad) + lateral * math.cos(rad),
        layout.get("y", 0.0) * scale_ft * aspect
        + forward * -math.cos(rad) + lateral * math.sin(rad),
    )


def _interp_at(points: list[list[float]], t: float, max_dt_s: float) -> tuple[float, float] | None:
    """Track position at time t: linear interpolation between bracketing
    points (bracket span ≤ _MAX_BRACKET_S), else nearest point within
    max_dt_s. Points are [x, y, t] sorted by t."""
    lo = None
    for p in points:
        if p[2] <= t:
            lo = p
        else:
            if lo is not None and p[2] - lo[2] <= _MAX_BRACKET_S:
                f = (t - lo[2]) / (p[2] - lo[2]) if p[2] > lo[2] else 0.0
                return (lo[0] + (p[0] - lo[0]) * f, lo[1] + (p[1] - lo[1]) * f)
            near = lo if lo is not None and t - lo[2] <= p[2] - t else p
            return (near[0], near[1]) if abs(near[2] - t) <= max_dt_s else None
    if lo is not None and t - lo[2] <= max_dt_s:
        return (lo[0], lo[1])
    return None


def mine_pairs(
    tracks: list[dict[str, Any]],
    settings: dict[str, Any],
    *,
    max_dt_s: float = 0.4,
    max_forward_ft: float = 100.0,
    min_pairs_per_campair: int = 20,
    max_pairs_per_trackpair: int = 40,
) -> tuple[list[Pair], dict[str, int], list[str]]:
    """Cross-camera same-instant observation pairs from capture tracks.

    Returns (pairs, counts by "camA|camB", warnings). Camera pairs with
    fewer than `min_pairs_per_campair` usable pairs are dropped — too few
    constraints to mean anything.
    """
    cams = _tunable_cameras(settings)
    base: Params = {}
    layout = settings.get("camera_layout") or {}
    for cam, facts in cams.items():
        base[cam] = (layout[cam]["azimuth"], facts["tilt_deg"])

    usable = [
        t for t in tracks
        if t.get("camera") in cams and len(t.get("points") or []) >= 2
    ]
    by_campair: dict[tuple[str, str], list[Pair]] = {}
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            ta, tb = usable[i], usable[j]
            if ta["camera"] == tb["camera"] or ta.get("label") != tb.get("label"):
                continue
            if ta["camera"] > tb["camera"]:
                ta, tb = tb, ta
            pa, pb = ta["points"], tb["points"]
            if pa[-1][2] < pb[0][2] or pb[-1][2] < pa[0][2]:
                continue  # no time overlap
            src, other = (pa, tb) if len(pa) <= len(pb) else (pb, ta)
            src_cam = ta["camera"] if src is pa else tb["camera"]
            oth_cam = other["camera"]
            candidates: list[Pair] = []
            # (src_world, oth_world)
            last_kept: tuple[tuple[float, float], tuple[float, float]] | None = None
            for p in src:
                if p[2] <= 0:
                    continue
                o = _interp_at(other["points"], p[2], max_dt_s)
                if o is None:
                    continue
                sp, op = (p[0], p[1]), o
                pair: Pair = (
                    (src_cam, sp, oth_cam, op)
                    if src_cam == ta["camera"] else (oth_cam, op, src_cam, sp)
                )
                # Both must project under CURRENT facts, inside the range
                # where the model is trustworthy.
                ok = True
                worlds = []
                for cam, pt in ((pair[0], pair[1]), (pair[2], pair[3])):
                    g = ground.project(pt[0], pt[1], cams[cam])
                    w = world_ft(pt, cam, base, settings, facts=cams[cam])
                    if g is None or w is None or g[0] > max_forward_ft:
                        ok = False
                        break
                    worlds.append(w)
                if not ok:
                    continue
                if last_kept is not None:
                    moved = max(
                        math.hypot(worlds[0][0] - last_kept[0][0],
                                   worlds[0][1] - last_kept[0][1]),
                        math.hypot(worlds[1][0] - last_kept[1][0],
                                   worlds[1][1] - last_kept[1][1]),
                    )
                    if moved < _MIN_MOTION_FT:
                        continue
                last_kept = (worlds[0], worlds[1])
                candidates.append(pair)
            if len(candidates) > max_pairs_per_trackpair:
                stride = len(candidates) / max_pairs_per_trackpair
                candidates = [
                    candidates[int(k * stride)]
                    for k in range(max_pairs_per_trackpair)
                ]
            if candidates:
                key = (candidates[0][0], candidates[0][2])
                by_campair.setdefault(key, []).extend(candidates)

    pairs: list[Pair] = []
    counts: dict[str, int] = {}
    warnings: list[str] = []
    for (a, b), plist in sorted(by_campair.items()):
        if len(plist) < min_pairs_per_campair:
            warnings.append(f"{a}|{b}: only {len(plist)} pairs, skipped")
            continue
        counts[f"{a}|{b}"] = len(plist)
        pairs.extend(plist)
    return pairs, counts, warnings


def solve_landmarks(
    camera: str,
    matches: list[dict[str, float]],
    settings: dict[str, Any],
    *,
    az_bound_deg: float = 60.0,
    tilt_bound_deg: float = 25.0,
    hfov_span: tuple[float, float] = (0.5, 1.6),
    max_rounds: int = 60,
) -> dict[str, Any]:
    """Solve one camera's (hfov, azimuth, tilt) from landmark matches.

    Each match pairs an image click `{u, v}` (normalized frame coords)
    with the same physical spot clicked on the calibrated floorplan
    `{mx, my}` (normalized map coords). Camera position, mount height,
    and map scale are trusted facts; the three aim/lens numbers are
    solved by minimizing the mean squared distance in feet between where
    the model puts each image click and where the user clicked the map.
    Two matches determine the system; three or more overdetermine it and
    the residuals expose bad clicks. VFOV rides along with HFOV: a
    vendor-published vfov scales by the tan-ratio (both are set by the
    same sensor/lens geometry), otherwise it derives at 16:9.
    """
    facts = facts_for(settings, camera)
    layout = (settings.get("camera_layout") or {}).get(camera)
    scale_ft = settings.get("map_scale_ft")
    if facts is None or not layout or layout.get("azimuth") is None:
        raise ValueError(f"{camera}: needs optics and a placed, aimed layout entry")
    if not scale_ft or scale_ft <= 0:
        raise ValueError("map scale is not set")
    if len(matches) < 2:
        raise ValueError("need at least 2 landmark matches")
    aspect = ground.map_aspect(settings)
    hfov0, tilt0, az0 = facts["hfov"], facts["tilt_deg"], float(layout["azimuth"])
    vendor_vfov = (settings.get("camera_optics") or {}).get(camera, {}).get("vfov")
    mount = facts["mount_ft"]
    cam_x = layout.get("x", 0.0) * scale_ft
    cam_y = layout.get("y", 0.0) * scale_ft * aspect
    targets = [
        (m["mx"] * scale_ft, m["my"] * scale_ft * aspect) for m in matches
    ]

    def vfov_of(hfov: float) -> float:
        if vendor_vfov:
            t = math.tan(math.radians(hfov / 2)) / math.tan(math.radians(hfov0 / 2))
            return 2 * math.degrees(math.atan(
                math.tan(math.radians(vendor_vfov / 2)) * t
            ))
        return optics.vfov_from_hfov(hfov, *_DETECT_ASPECT)

    def residuals(hfov: float, az: float, tilt: float) -> list[float]:
        # Same pinhole ray/ground model as live projection (ground.project),
        # so a solved aim reproduces exactly what the map will then show.
        trial = {
            "hfov": hfov, "vfov": vfov_of(hfov),
            "tilt_deg": tilt, "mount_ft": mount,
        }
        rad = math.radians(az)
        out = []
        for m, (tx, ty) in zip(matches, targets, strict=True):
            g = ground.project(m["u"], m["v"], trial)
            if g is None:
                out.append(_OFFRANGE_MISS_FT * 2)
                continue
            forward, lateral = g
            wx = cam_x + forward * math.sin(rad) + lateral * math.cos(rad)
            wy = cam_y + forward * -math.cos(rad) + lateral * math.sin(rad)
            out.append(math.hypot(wx - tx, wy - ty))
        return out

    def score(hfov: float, az: float, tilt: float) -> float:
        return sum(r * r for r in residuals(hfov, az, tilt)) / len(matches)

    hfov, az, tilt = hfov0, az0, tilt0
    hfov_lo = max(10.5, hfov0 * hfov_span[0])
    hfov_hi = min(179.0, hfov0 * hfov_span[1])
    best = score(hfov, az, tilt)
    for _ in range(max_rounds):
        az = _golden_min(
            lambda v, h=hfov, t=tilt: score(h, v, t),
            az0 - az_bound_deg, az0 + az_bound_deg,
        )
        tilt = _golden_min(
            lambda v, h=hfov, a=az: score(h, a, v),
            max(0.5, tilt0 - tilt_bound_deg), min(89.0, tilt0 + tilt_bound_deg),
        )
        hfov = _golden_min(
            lambda v, a=az, t=tilt: score(v, a, t), hfov_lo, hfov_hi,
        )
        cur = score(hfov, az, tilt)
        if best - cur < 1e-6 * max(1.0, best):
            break
        best = cur

    res = residuals(hfov, az, tilt)
    # The map wedge shows GROUND coverage, which for a tilted camera is
    # wider than the lens HFOV: a frame-edge ray lands on the ground at
    # bearing atan(tan(hfov/2)/cos(tilt)) off the axis (at the axis row).
    wedge = 2 * math.degrees(math.atan(
        math.tan(math.radians(hfov) / 2) / max(1e-6, math.cos(math.radians(tilt)))
    ))
    report = {
        "camera": camera,
        "wedge_fov_after": round(min(360.0, wedge), 1),
        "hfov_before": round(hfov0, 1), "hfov_after": round(hfov, 1),
        "azimuth_before": round(az0, 1), "azimuth_after": round(az % 360.0, 1),
        "tilt_before": round(tilt0, 1), "tilt_after": round(tilt, 1),
        "vfov_after": round(vfov_of(hfov), 1) if vendor_vfov else None,
        "residual_ft": [round(r, 1) for r in res],
        "rms_ft": round(math.sqrt(sum(r * r for r in res) / len(res)), 1),
        "matches": len(matches),
    }
    return report


def _huber(d: float, delta: float) -> float:
    return d * d / 2.0 if d <= delta else delta * (d - delta / 2.0)


def objective(
    params: Params, pairs: list[Pair], settings: dict[str, Any], *,
    base: Params, reg_weight: float = 0.02, huber_delta_ft: float = 15.0,
) -> float:
    """Mean Huber loss over pair disagreement (ft) + gauge regularizer.

    Huber caps the pull of wrong pairings (two people at once) without
    the membership discontinuities of trimming. The regularizer pins the
    global-rotation gauge to the hand-set aims — a whole-map rotation
    that leaves pair distances unchanged still costs — and tilt gets 2×
    weight because it's physically measurable and trades off with mount
    height.
    """
    if not pairs:
        return 0.0
    total = 0.0
    for cam_a, pt_a, cam_b, pt_b in pairs:
        wa = world_ft(pt_a, cam_a, params, settings)
        wb = world_ft(pt_b, cam_b, params, settings)
        if wa is None or wb is None:
            total += _huber(_OFFRANGE_MISS_FT, huber_delta_ft)
            continue
        total += _huber(math.hypot(wa[0] - wb[0], wa[1] - wb[1]), huber_delta_ft)
    loss = total / len(pairs)
    for cam, (az, tilt) in params.items():
        az0, tilt0 = base[cam]
        d_az = (az - az0 + 180.0) % 360.0 - 180.0
        loss += reg_weight * (d_az * d_az + 2.0 * (tilt - tilt0) ** 2)
    return loss


def _rms_ft(params: Params, pairs: list[Pair], settings: dict[str, Any]) -> float:
    """Raw (unhuberized) RMS pair disagreement in feet; off-range counts
    as the fixed miss."""
    if not pairs:
        return 0.0
    total = 0.0
    for cam_a, pt_a, cam_b, pt_b in pairs:
        wa = world_ft(pt_a, cam_a, params, settings)
        wb = world_ft(pt_b, cam_b, params, settings)
        d = (
            _OFFRANGE_MISS_FT if wa is None or wb is None
            else math.hypot(wa[0] - wb[0], wa[1] - wb[1])
        )
        total += d * d
    return math.sqrt(total / len(pairs))


def _golden_min(f: Callable[..., float], lo: float, hi: float, iters: int = 20) -> float:
    """Golden-section minimum of f on [lo, hi]."""
    a, b = lo, hi
    c = b - (b - a) * _GOLDEN
    d = a + (b - a) * _GOLDEN
    fc, fd = f(c), f(d)
    for _ in range(iters):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - (b - a) * _GOLDEN
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + (b - a) * _GOLDEN
            fd = f(d)
    return c if fc < fd else d


def fit(
    pairs: list[Pair], settings: dict[str, Any], *,
    az_bound_deg: float = 15.0, tilt_bound_deg: float = 8.0,
    max_rounds: int = 30, eps: float = 1e-3,
) -> dict[str, Any]:
    """Tune (azimuth, tilt) per camera by cyclic coordinate descent.

    Each coordinate gets a golden-section line search within a shrinking
    window, clipped to `az0 ± az_bound` / `tilt0 ± tilt_bound` ∩ [-90, 90].
    Cameras contributing no pairs are left untouched. Azimuth is
    optimized in a locally-unwrapped frame around its starting value and
    normalized to [0, 360) on output.
    """
    layout = settings.get("camera_layout") or {}
    base: Params = {}
    involved = {c for p in pairs for c in (p[0], p[2])}
    for cam in sorted(involved):
        facts = facts_for(settings, cam)
        if facts is None:  # pairs only exist for cameras with facts
            continue
        base[cam] = (layout[cam]["azimuth"], facts["tilt_deg"])
    params: Params = dict(base)

    def score(p: Params) -> float:
        return objective(p, pairs, settings, base=base)

    rms_before = _rms_ft(base, pairs, settings)
    current = score(params)
    az_half, tilt_half = az_bound_deg, tilt_bound_deg
    for _ in range(max_rounds):
        round_start = current
        for cam in base:
            az0, tilt0 = base[cam]
            az, tilt = params[cam]

            def try_az(v: float, _cam: str = cam, _tilt: float = tilt) -> float:
                trial = dict(params)
                trial[_cam] = (v, _tilt)
                return score(trial)

            lo = max(az0 - az_bound_deg, az - az_half)
            hi = min(az0 + az_bound_deg, az + az_half)
            az = _golden_min(try_az, lo, hi)

            def try_tilt(v: float, _cam: str = cam, _az: float = az) -> float:
                trial = dict(params)
                trial[_cam] = (_az, v)
                return score(trial)

            lo = max(tilt0 - tilt_bound_deg, tilt - tilt_half, -90.0)
            hi = min(tilt0 + tilt_bound_deg, tilt + tilt_half, 90.0)
            tilt = _golden_min(try_tilt, lo, hi)
            params[cam] = (az, tilt)
            current = score(params)
        az_half = max(0.25, az_half * 0.5)
        tilt_half = max(0.25, tilt_half * 0.5)
        if round_start - current < eps * max(1.0, round_start):
            break

    cameras: dict[str, Any] = {}
    pair_count = {cam: 0 for cam in base}
    for p in pairs:
        pair_count[p[0]] += 1
        pair_count[p[2]] += 1
    for cam in base:
        az0, tilt0 = base[cam]
        az, tilt = params[cam]
        cameras[cam] = {
            "azimuth_before": round(az0, 1),
            "azimuth_after": round(az % 360.0, 1),
            "tilt_before": round(tilt0, 1),
            "tilt_after": round(tilt, 1),
            "pairs": pair_count[cam],
        }
    return {
        "cameras": cameras,
        "rms_before_ft": round(rms_before, 1),
        "rms_after_ft": round(_rms_ft(params, pairs, settings), 1),
    }
