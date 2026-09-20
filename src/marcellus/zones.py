"""Read Frigate camera/zone definitions from `config.yml` for UI overlays."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

ZONE_PALETTE = (
    "#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899",
    "#14b8a6", "#eab308", "#22d3ee", "#f97316", "#84cc16", "#a855f7",
)


def _parse_coords(coords: object) -> list[tuple[float, float]] | None:
    if not coords:
        return None
    try:
        nums = [float(x) for x in str(coords).split(",")]
    except ValueError:
        return None
    if len(nums) < 6 or len(nums) % 2 != 0:
        return None
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


def is_full_frame(coords: list[tuple[float, float]], eps: float = 0.02) -> bool:
    """A polygon that spans the entire frame (e.g. per-camera `animal` zones)."""
    if len(coords) != 4:
        return False
    xs = [p[0] for p in coords]
    ys = [p[1] for p in coords]
    return (
        min(xs) < eps
        and max(xs) > 1 - eps
        and min(ys) < eps
        and max(ys) > 1 - eps
    )


def color_for_zone(name: str) -> str:
    h = int(hashlib.md5(name.encode()).hexdigest(), 16)
    return ZONE_PALETTE[h % len(ZONE_PALETTE)]


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting PiP. Matches Frigate's own zone-presence eval."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def zones_containing_box(
    zones: list[dict[str, Any]], box: list[float] | None
) -> set[str]:
    """Zones whose polygon contains the bbox's bottom-center point.

    Mirrors Frigate's own semantic: zone presence is evaluated at the
    bottom-center of the bbox, not the centroid.

    `box` is Frigate's `event.data.box`: normalized **[x, y, w, h]** (that is
    what `to_relative_box` writes), so bottom-center is `(x + w/2, y + h)`.
    Reading it as `[x1, y1, x2, y2]` — which this did — put the test point at
    `((x+w)/2, h)`, a place unrelated to the object, so the detail page's
    "active zone" highlighting was wrong whenever it mattered.
    `analysis/zone_hits.py` has always read the same field as [x, y, w, h].

    Returns empty set if box is missing/invalid. Full-frame zones (e.g.
    `*_critter` regional gates) are excluded — they're trivially active for any
    in-frame box and add no spatial signal.
    """
    if not box or len(box) != 4:
        return set()
    x, y, w, h = box
    cx = x + w / 2.0
    by = y + h
    return {
        z["name"]
        for z in zones
        if not is_full_frame(z["coords"])
        and point_in_polygon(cx, by, z["coords"])
    }


def configured_camera_names(config_path: str | Path) -> set[str] | None:
    """Camera names in Frigate's config.yml, or None if it can't be read.

    Auto-discovery from the recordings table alone resurrects renamed cameras:
    rows under the old name outlive the rename by the retention window, so the
    scrub generator ground through ghost cameras (and advertised them to
    clients) for days after a rename. Callers intersect discovered names with
    this set; None (missing/unparseable config) means "can't tell, don't
    filter".
    """
    p = Path(config_path)
    try:
        with p.open() as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(cfg, dict):
        return None
    cams = cfg.get("cameras")
    if not isinstance(cams, dict) or not cams:
        return None
    return set(cams.keys())


def load_camera_zones(config_path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Return `{camera: [{name, coords, color, objects, inertia, loitering_time}, ...]}`.

    Reads Frigate's config.yml. Missing file or unparseable file -> {} (no crash).
    """
    p = Path(config_path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        # A malformed config.yml is Frigate's problem, not a reason to 500 the
        # triage detail page -- render it without the zone overlay instead.
        return {}
    if not isinstance(cfg, dict):
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for cam_name, cam in (cfg.get("cameras") or {}).items():
        if not isinstance(cam, dict):
            continue
        cam_zones: list[dict[str, Any]] = []
        for zname, z in (cam.get("zones") or {}).items():
            zd = z or {}
            pts = _parse_coords(zd.get("coordinates"))
            if not pts:
                continue
            objs = zd.get("objects")
            if isinstance(objs, str):
                objs = [objs]
            cam_zones.append(
                {
                    "name": zname,
                    "coords": pts,
                    "color": color_for_zone(zname),
                    "objects": objs or [],
                    "inertia": zd.get("inertia"),
                    "loitering_time": zd.get("loitering_time"),
                    # Frigate's optional display name — push copy prefers it
                    # over humanizing the rule-shaped zone key.
                    "friendly_name": zd.get("friendly_name"),
                }
            )
        if cam_zones:
            out[cam_name] = cam_zones
    return out
