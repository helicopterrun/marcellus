"""Direction derivation for encounter members (docs/encounters.md
"Observations"): a pure, best-effort reading of "which way did this atom
move" from Frigate's own event rows, computed once at link time and stored
on `encounter_members` so routes never have to re-derive it.

Three tiers, first that yields a direction wins, cascading in order:

1. **Zones** -- Frigate's `event.zones` is already an ordered list of zone
   names the track passed through. Two different zones give a cheap, robust
   `out:<last_zone>` verdict without any geometry.
2. **Path** -- `data.path_data` (normalised (x, y, t) points) lets us fit a
   coarse heading and bucket it into l2r/r2l/toward/away.
3. **Box** -- first/last bounding box centroid (and area growth, as a
   toward/away tiebreaker) when there's no usable path.

Never raises: malformed/missing input at every stage degrades to the empty
`Direction`, because a bad direction guess must never block linking or
break an upsert.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from marcellus.db import parse_event_data, parse_path_data

#: Minimum frame-normalised displacement (path or box centroid) below which
#: a heading guess is noise, not motion.
_DISPLACEMENT_THRESHOLD = 0.08

#: Box-area growth ratio (end / start) above which "toward the camera" wins
#: over a path/box heading whose |dy| component is small.
_AREA_GROWTH_TOWARD = 1.25


@dataclass(frozen=True)
class Direction:
    first_zone: str
    last_zone: str
    direction: str
    heading_deg: float | None
    source: str


_EMPTY = Direction("", "", "", None, "")


def _row_get(row: Any, key: str) -> Any:
    """Read one field off either a `sqlite3.Row` or a plain mapping."""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return None


def _event_zones(row: Any) -> list[str]:
    raw = _row_get(row, "zones")
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(z) for z in parsed if z]


def _zones_direction(event_rows: Sequence[Any]) -> tuple[str, str, str, str]:
    """(first_zone, last_zone, direction, source) from concatenated,
    order-preserving deduped `event.zones` across every row."""
    seen: list[str] = []
    for row in event_rows:
        for zone in _event_zones(row):
            if zone not in seen:
                seen.append(zone)
    if not seen:
        return "", "", "", ""
    first_zone, last_zone = seen[0], seen[-1]
    if first_zone != last_zone:
        return first_zone, last_zone, f"out:{last_zone}", "zones"
    return first_zone, last_zone, "", "zones"


def _bucket(dx: float, dy: float, *, area_growth: float | None = None) -> str:
    """Coarse compass bucket shared by the path and box tiers: dominant axis
    wins, with box-area growth allowed to override a weak |dy| toward
    'toward'/'away' (rule 3's tiebreaker)."""
    weak_vertical = area_growth is not None and abs(dy) < _DISPLACEMENT_THRESHOLD
    if weak_vertical and area_growth is not None and area_growth > _AREA_GROWTH_TOWARD:
        return "toward"
    if abs(dx) >= abs(dy):
        return "l2r" if dx > 0 else "r2l"
    return "toward" if dy > 0 else "away"


def _heading_deg(dx: float, dy: float) -> float:
    deg = math.degrees(math.atan2(dy, dx))
    return deg % 360.0


def _path_direction(event_rows: Sequence[Any]) -> Direction | None:
    points: list[tuple[float, float, float]] = []
    for row in event_rows:
        try:
            parsed = parse_event_data(row) if not isinstance(row, Mapping) else _mapping_parsed(row)
        except Exception:  # noqa: BLE001 -- direction guessing must never raise
            continue
        path_data = None
        if isinstance(parsed, dict):
            path_data = parsed.get("_data_parsed", {}).get("path_data") or parsed.get("path_data")
        try:
            points.extend(parse_path_data(path_data))
        except Exception:  # noqa: BLE001
            continue
    if len(points) < 4:
        return None
    points.sort(key=lambda p: p[2])
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    ts = [p[2] for p in points]
    dx_total = xs[-1] - xs[0]
    dy_total = ys[-1] - ys[0]
    if math.hypot(dx_total, dy_total) < _DISPLACEMENT_THRESHOLD:
        return None
    dx_slope = _least_squares_slope(ts, xs)
    dy_slope = _least_squares_slope(ts, ys)
    if dx_slope is None or dy_slope is None:
        dx_slope, dy_slope = dx_total, dy_total
    heading = _heading_deg(dx_slope, dy_slope)
    bucket = _bucket(dx_total, dy_total)
    return Direction("", "", bucket, heading, "path")


def _mapping_parsed(row: Mapping[str, Any]) -> dict[str, Any]:
    """`parse_event_data` needs `.keys()`/`__getitem__`, which a plain dict
    already provides -- build the same `_data_parsed`/`path_data` shape by
    hand for the (mostly test-fixture) mapping case."""
    out = dict(row)
    raw = out.get("data")
    parsed: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            parsed = {}
    out["_data_parsed"] = parsed
    return out


def _least_squares_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Simple least-squares slope of y over x; None if x has no spread."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return numer / denom


def _event_box(row: Any) -> list[float] | None:
    try:
        parsed = parse_event_data(row) if not isinstance(row, Mapping) else _mapping_parsed(row)
    except Exception:  # noqa: BLE001
        return None
    box = parsed.get("_data_parsed", {}).get("box") if isinstance(parsed, dict) else None
    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except (TypeError, ValueError):
        return None


def _box_direction(event_rows: Sequence[Any]) -> Direction | None:
    """First/last event.data.box, normalised [x1, y1, x2, y2]. Boxes here are
    assumed already frame-normalised (0..1) -- that's what Frigate's own
    `data.box` stores (see db.parse_event_data). If a deployment ever stores
    pixel boxes this tier under/over-fires; there's no frame-size field on
    the event row to normalise against, so pixel boxes are out of scope for
    now."""
    boxes = [b for b in (_event_box(r) for r in event_rows) if b is not None]
    if len(boxes) < 2:
        return None
    first, last = boxes[0], boxes[-1]

    def _centroid(b: list[float]) -> tuple[float, float]:
        return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)

    def _area(b: list[float]) -> float:
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    cx0, cy0 = _centroid(first)
    cx1, cy1 = _centroid(last)
    dx, dy = cx1 - cx0, cy1 - cy0
    area0, area1 = _area(first), _area(last)
    growth = (area1 / area0) if area0 > 0 else None
    growth_signal = growth is not None and growth > _AREA_GROWTH_TOWARD
    if math.hypot(dx, dy) < _DISPLACEMENT_THRESHOLD and not growth_signal:
        return None
    bucket = _bucket(dx, dy, area_growth=growth)
    return Direction("", "", bucket, _heading_deg(dx, dy), "box")


def derive_direction(event_rows: Sequence[sqlite3.Row | Mapping[str, Any]]) -> Direction:
    """Best-effort direction for one atom, from its Frigate `event` rows
    (already ordered by start_time by the caller). See module docstring for
    the tier order. Never raises."""
    try:
        rows = list(event_rows)
    except Exception:  # noqa: BLE001
        return _EMPTY
    if not rows:
        return _EMPTY

    try:
        first_zone, last_zone, zones_direction, zones_source = _zones_direction(rows)
    except Exception:  # noqa: BLE001
        first_zone, last_zone, zones_direction, zones_source = "", "", "", ""

    if zones_direction:
        return Direction(first_zone, last_zone, zones_direction, None, zones_source)

    # Single (or no) zone: keep first/last_zone if we have them, but keep
    # looking for an actual direction from path/box.
    path = None
    try:
        path = _path_direction(rows)
    except Exception:  # noqa: BLE001
        path = None
    if path is not None:
        return Direction(first_zone, last_zone, path.direction, path.heading_deg, path.source)

    box = None
    try:
        box = _box_direction(rows)
    except Exception:  # noqa: BLE001
        box = None
    if box is not None:
        return Direction(first_zone, last_zone, box.direction, box.heading_deg, box.source)

    if first_zone or last_zone:
        return Direction(first_zone, last_zone, "", None, "zones")
    return _EMPTY


def load_direction(
    frigate_conn: sqlite3.Connection, event_ids: Sequence[str]
) -> Direction:
    """Load `event` rows for `event_ids` from Frigate (read-only) ordered by
    start_time, and derive a `Direction`. Missing rows/table -> empty
    Direction; never raises (the caller's upsert must never fail on this)."""
    ids = [str(e) for e in event_ids if e]
    if not ids:
        return _EMPTY
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = frigate_conn.execute(
            f"SELECT id, zones, data FROM event WHERE id IN ({placeholders}) "
            "ORDER BY start_time",
            ids,
        ).fetchall()
    except sqlite3.Error:
        return _EMPTY
    return derive_direction(rows)
