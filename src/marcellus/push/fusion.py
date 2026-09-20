"""Geometric fusion: zone polygons and live tracks projected onto the map.

Everything here is a pure function over a settings document plus either a
zone polygon (normalized image coords) or a TrackStore snapshot. Two jobs:

* `project_polygon` — a Frigate zone's image-space polygon → floorplan-map
  coordinates, densified (the ground mapping is nonlinear) and honestly
  clipped where vertices sit at/above the horizon or past the projection
  range.
* `track_world_positions` + `cluster` — every live track's latest path
  point placed on the map, then cross-camera sightings within a
  distance-scaled threshold merged into one fused object. The ±25% model
  error grows with forward distance, so the merge threshold does too.

Display + log-only for now: the live-map endpoint draws clusters, and
`delivery_wire` logs what geometry *would* have linked — routing is
untouched until those logs validate against real walks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from marcellus.push import ground

if TYPE_CHECKING:
    from marcellus.push.situations import TrackStore

#: Densify polygon edges so no segment spans more than this in image space.
_MAX_EDGE_STEP = 0.05
#: Cap on interpolated samples per edge (safety for degenerate coords).
_MAX_EDGE_SAMPLES = 20
#: Bisection steps when an edge straddles the projectable boundary.
_BISECT_STEPS = 5
#: A track's last path point must be at least this fresh to appear live.
LIVE_WINDOW_S = 3.0
#: Merge floor: two cameras placing an object within this many feet always
#: count as one object, however close the object is.
_MIN_MERGE_FT = 10.0
#: Fraction of mean forward distance added to the merge threshold — the
#: first-order model's stated error band.
_MERGE_ERROR_FRAC = 0.25


def _lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def project_polygon(
    coords: list[tuple[float, float]],
    *,
    camera: str,
    layout_entry: dict[str, float],
    scale_ft: float,
    aspect_h_over_w: float = 1.0,
) -> list[tuple[float, float]] | None:
    """Zone polygon (normalized image coords) → map coords, or None.

    Edges are densified before projection because equal image steps are
    wildly unequal ground steps near the horizon. Samples that don't
    project (at/above the horizon, past range) are dropped and the polygon
    closes across the gap — the honest clip. Where an edge straddles the
    boundary, bisection finds the last projectable point so the clipped
    edge is smooth. None when fewer than 3 points survive.
    """
    facts = ground.camera_ground(camera)
    if facts is None or len(coords) < 3:
        return None

    def to_map(pt: tuple[float, float]) -> tuple[float, float] | None:
        return ground.world_position(
            pt[0], pt[1], camera=camera, layout_entry=layout_entry,
            scale_ft=scale_ft, aspect_h_over_w=aspect_h_over_w,
        )

    out: list[tuple[float, float]] = []
    n = len(coords)
    for i in range(n):
        a, b = coords[i], coords[(i + 1) % n]
        span = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = min(_MAX_EDGE_SAMPLES, max(1, math.ceil(span / _MAX_EDGE_STEP)))
        samples = [_lerp(a, b, k / steps) for k in range(steps)]  # excl. b: next edge owns it
        projected = [to_map(s) for s in samples]
        for j, (img_pt, map_pt) in enumerate(zip(samples, projected, strict=True)):
            if map_pt is not None:
                out.append(map_pt)
                continue
            # Straddle refinement: bisect toward whichever neighbor sample
            # projects, appending the last projectable point found.
            for other in (samples[j - 1] if j > 0 else None,
                          samples[j + 1] if j + 1 < len(samples) else None):
                if other is None or to_map(other) is None:
                    continue
                lo, hi = other, img_pt  # lo projects, hi doesn't
                for _ in range(_BISECT_STEPS):
                    mid = _lerp(lo, hi, 0.5)
                    if to_map(mid) is not None:
                        lo = mid
                    else:
                        hi = mid
                edge_pt = to_map(lo)
                if edge_pt is not None:
                    out.append(edge_pt)
                break
    if len(out) < 3:
        return None
    return out


@dataclass
class TrackPos:
    """One live track's latest position, in map coords + camera-forward ft."""

    camera: str
    track_id: str
    label: str | None
    x: float
    y: float
    forward_ft: float
    stationary: bool
    age_s: float


@dataclass
class Cluster:
    """One fused physical object: weighted-mean position + members."""

    x: float
    y: float
    label: str | None
    stationary: bool
    members: list[TrackPos] = field(default_factory=list)


def track_world_positions(
    tracks: TrackStore, settings: dict[str, Any], *,
    now: float, window_s: float = LIVE_WINDOW_S,
) -> list[TrackPos]:
    """Project every live track's newest path point onto the map.

    Skips tracks with no timestamped path point newer than `window_s`, and
    cameras missing layout/optics/scale. `tracks` is a TrackStore (or
    anything with `.items()` yielding `((camera, track_id), state)`).
    """
    layout_table = settings.get("camera_layout") or {}
    scale_ft = settings.get("map_scale_ft")
    if not scale_ft or scale_ft <= 0:
        return []
    aspect = ground.map_aspect(settings)
    out: list[TrackPos] = []
    for (camera, track_id), state in tracks.items():
        layout = layout_table.get(camera)
        if not layout or not state.path_data:
            continue
        pt = state.path_data[-1]
        if len(pt) < 3 or pt[2] <= 0 or now - pt[2] > window_s:
            continue
        facts = ground.camera_ground(camera)
        if facts is None:
            continue
        g = ground.project(pt[0], pt[1], facts)
        wp = ground.world_position(
            pt[0], pt[1], camera=camera, layout_entry=layout,
            scale_ft=scale_ft, aspect_h_over_w=aspect,
        )
        if g is None or wp is None:
            continue
        out.append(TrackPos(
            camera=camera, track_id=track_id, label=state.label,
            x=wp[0], y=wp[1], forward_ft=g[0],
            stationary=state.stationary, age_s=max(0.0, now - pt[2]),
        ))
    return out


def merge_threshold_ft(a: TrackPos, b: TrackPos) -> float:
    return max(_MIN_MERGE_FT, _MERGE_ERROR_FRAC * (a.forward_ft + b.forward_ft) / 2.0)


def _dist_ft(a: TrackPos, b: TrackPos, scale_ft: float, aspect: float) -> float:
    return math.hypot((a.x - b.x) * scale_ft, (a.y - b.y) * scale_ft * aspect)


def cluster(
    positions: list[TrackPos], *, scale_ft: float, aspect_h_over_w: float = 1.0,
) -> list[Cluster]:
    """Greedy union-find over cross-camera pairs, nearest first.

    Two tracks merge only when they come from different cameras, carry the
    same label (unknown labels never merge), and sit within the
    distance-scaled threshold. Same-camera tracks never merge — Frigate
    already separates those. Track counts are single-digit; O(n²) is fine.
    """
    n = len(positions)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    pairs: list[tuple[float, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = positions[i], positions[j]
            if a.camera == b.camera or a.label is None or a.label != b.label:
                continue
            d = _dist_ft(a, b, scale_ft, aspect_h_over_w)
            if d <= merge_threshold_ft(a, b):
                pairs.append((d, i, j))
    for _, i, j in sorted(pairs):
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        # Keep clusters one-track-per-camera: merging two roots that both
        # contain the same camera would chain distinct objects together.
        cams_i = {positions[k].camera for k in range(n) if find(k) == ri}
        cams_j = {positions[k].camera for k in range(n) if find(k) == rj}
        if cams_i & cams_j:
            continue
        parent[rj] = ri
    groups: dict[int, list[TrackPos]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(positions[i])
    out: list[Cluster] = []
    for members in groups.values():
        # Nearer camera = smaller error: weight by inverse forward distance.
        weights = [1.0 / max(1.0, m.forward_ft) for m in members]
        total = sum(weights)
        out.append(Cluster(
            x=sum(m.x * w for m, w in zip(members, weights, strict=True)) / total,
            y=sum(m.y * w for m, w in zip(members, weights, strict=True)) / total,
            label=members[0].label,
            stationary=all(m.stationary for m in members),
            members=sorted(members, key=lambda m: m.camera),
        ))
    out.sort(key=lambda c: (c.label or "", c.x, c.y))
    return out
