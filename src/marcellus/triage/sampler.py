"""Stratified sampling of borderline events for human triage.

Approximate sampling strategy (matches the upstream tuning skill):
- 60% from the decision zone (top_score in [0.55, 0.85))
- 20% from the low tail (top_score < 0.55)
- 20% from the high tail (top_score >= 0.85)
- per-camera cap of 30% when no camera filter is supplied
- skip events already triaged (labels live in the sidecar DB)
- only events with `has_snapshot = 1`
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marcellus.db import (
    open_joined,
    parse_event_data,
    read_with_retry,
    time_window_clause,
)


@dataclass(frozen=True)
class SampleQuota:
    decision_zone: int
    low_tail: int
    high_tail: int

    @classmethod
    def for_n(cls, n: int) -> SampleQuota:
        dz = int(n * 0.6)
        lt = int(n * 0.2)
        return cls(decision_zone=dz, low_tail=lt, high_tail=n - dz - lt)


def score_band(top: float) -> str:
    if top >= 0.85:
        return "high"
    if top < 0.55:
        return "very_low"
    if top < 0.65:
        return "low"
    if top < 0.75:
        return "mid"
    return "midhigh"


_BAND_TO_GROUP = {
    "low": "decision_zone",
    "mid": "decision_zone",
    "midhigh": "decision_zone",
    "very_low": "low_tail",
    "high": "high_tail",
}


def _select_optional_columns(conn: Any) -> tuple[str, str]:
    """Return SELECT fragments for `area` and `ratio` if present, else NULL aliases.

    Some Frigate schemas / versions may omit these. Detecting at runtime keeps
    the sidecar portable.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
    area = "e.area" if "area" in cols else "NULL AS area"
    ratio = "e.ratio" if "ratio" in cols else "NULL AS ratio"
    return area, ratio


def sample(
    *,
    frigate_db: str | Path,
    sidecar_db: str | Path,
    api_base_url: str,
    days: int = 14,
    n: int = 30,
    camera: str | None = None,
    label: str | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """Return a list of sampled events with media URLs populated."""
    # Own RNG instance: seeding the global one made an explicit `--seed` here
    # reach into every other consumer of `random` in the process.
    rng = random.Random(seed) if seed is not None else random.Random()

    where, params = time_window_clause(days, "e.start_time")
    if camera:
        where += " AND e.camera = ?"
        params.append(camera)
    if label:
        where += " AND e.label = ?"
        params.append(label)

    def _fetch_rows() -> list[Any]:
        # A full read from scratch (column probe + main query, one
        # connection) so a `database is locked` retry gets a fresh
        # connection rather than reusing one that just failed.
        conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
        try:
            area_col, ratio_col = _select_optional_columns(conn)
            sql = f"""
                SELECT e.id, e.camera, e.label, e.start_time, e.end_time,
                       e.score, e.top_score, {area_col}, {ratio_col}, e.zones,
                       e.has_clip, e.has_snapshot, e.data
                  FROM event e
                  LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
                 WHERE {where}
                   AND t.event_id IS NULL
                   AND e.has_snapshot = 1
            """
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    bands: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_with_retry(_fetch_rows):
        ev = parse_event_data(row)
        top = ev["data_top_score"] if ev["data_top_score"] is not None else ev["top_score"]
        score = ev["data_score"] if ev["data_score"] is not None else ev["score"]
        if top is None:
            continue
        dtype = ev.get("data_type")
        # `data_type` is None on older events; skip rows where it is set
        # to something other than "object".
        if dtype is not None and dtype != "object":
            continue
        try:
            zones = json.loads(ev["zones"]) if ev.get("zones") else []
        except (TypeError, json.JSONDecodeError):
            zones = []
        band = score_band(float(top))
        bands[(ev["camera"], ev["label"], band)].append(
            {
                "id": ev["id"],
                "camera": ev["camera"],
                "label": ev["label"],
                "start_time": ev["start_time"],
                "end_time": ev["end_time"],
                "duration": (ev["end_time"] or ev["start_time"]) - ev["start_time"],
                "score": score,
                "top_score": top,
                "area": ev["area"],
                "ratio": ev["ratio"],
                "zones": zones,
                "has_clip": bool(ev["has_clip"]),
                "score_band": band,
            }
        )

    quotas = SampleQuota.for_n(n)
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for evs in bands.values():
        rng.shuffle(evs)
        for ev in evs:
            by_group[_BAND_TO_GROUP[ev["score_band"]]].append(ev)

    selected: list[dict[str, Any]] = []
    cam_count: dict[str, int] = defaultdict(int)
    cam_cap = n if camera else max(2, int(n * 0.30))

    for group, quota in {
        "decision_zone": quotas.decision_zone,
        "low_tail": quotas.low_tail,
        "high_tail": quotas.high_tail,
    }.items():
        pool = by_group.get(group, [])
        rng.shuffle(pool)
        picked = 0
        for ev in pool:
            if picked >= quota:
                break
            if cam_count[ev["camera"]] >= cam_cap:
                continue
            selected.append(ev)
            cam_count[ev["camera"]] += 1
            picked += 1

    if len(selected) < n:
        remaining: list[dict[str, Any]] = []
        for evs in by_group.values():
            remaining.extend(evs)
        rng.shuffle(remaining)
        seen = {ev["id"] for ev in selected}
        for ev in remaining:
            if len(selected) >= n:
                break
            if ev["id"] in seen:
                continue
            if cam_count[ev["camera"]] >= cam_cap:
                continue
            selected.append(ev)
            seen.add(ev["id"])
            cam_count[ev["camera"]] += 1

    selected.sort(key=lambda e: (e["camera"], e["start_time"]))

    base = api_base_url.rstrip("/")
    for ev in selected:
        ev["snapshot_url"] = f"{base}/api/events/{ev['id']}/snapshot.jpg"
        ev["thumbnail_url"] = f"{base}/api/events/{ev['id']}/thumbnail.jpg"
        ev["clip_url"] = f"{base}/api/events/{ev['id']}/clip.mp4" if ev["has_clip"] else None

    return selected
