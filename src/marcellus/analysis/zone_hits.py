"""Per-camera zone hit-map + mask candidates.

A "mask candidate" is flagged when a (camera, label) cluster has >=5 events
with similar centroids and either a high triage fp-rate (>=0.7) or no zone
assignment (orphan detections).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from marcellus.db import (
    open_joined,
    parse_event_data,
    read_with_retry,
    time_window_clause,
)


def _centroid(box: list[float] | None) -> tuple[float, float]:
    if not box or len(box) < 4:
        return (0.0, 0.0)
    return (box[0] + box[2] / 2, box[1] + box[3] / 2)


def _cluster_centroids(
    events: list[dict[str, Any]], tol: float = 0.10
) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for ev in events:
        cx, cy = _centroid(ev.get("box"))
        placed = False
        for cl in clusters:
            ccx, ccy = cl[0]["_cx"], cl[0]["_cy"]
            if abs(cx - ccx) < tol and abs(cy - ccy) < tol:
                cl.append({**ev, "_cx": cx, "_cy": cy})
                placed = True
                break
        if not placed:
            clusters.append([{**ev, "_cx": cx, "_cy": cy}])
    return clusters


def analyze(
    *,
    frigate_db: str | Path,
    sidecar_db: str | Path,
    days: int = 30,
    camera: str | None = None,
) -> dict[str, Any]:
    where, params = time_window_clause(days, "e.start_time")
    if camera:
        where += " AND e.camera = ?"
        params.append(camera)

    def _fetch_rows() -> list[Any]:
        # A full read from scratch (column probe + main query, one
        # connection) so a `database is locked` retry gets a fresh
        # connection rather than reusing one that just failed.
        conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
            area_col = "e.area" if "area" in cols else "NULL AS area"
            sql = f"""
                SELECT e.id, e.camera, e.label, e.zones, {area_col}, e.data,
                       t.label AS triage
                  FROM event e
                  LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
                 WHERE {where}
            """
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    zone_hits: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    zone_fps: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    cam_label_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for row in read_with_retry(_fetch_rows):
        ev = parse_event_data(row)
        try:
            zones = json.loads(ev["zones"]) if ev.get("zones") else []
        except (TypeError, json.JSONDecodeError):
            zones = []
        cam, lbl, triage = ev["camera"], ev["label"], row["triage"]
        if not zones:
            zone_hits[cam]["__none__"][lbl] += 1
            if triage == "fp":
                zone_fps[cam]["__none__"][lbl] += 1
        else:
            for z in zones:
                zone_hits[cam][z][lbl] += 1
                if triage == "fp":
                    zone_fps[cam][z][lbl] += 1
        cam_label_events[(cam, lbl)].append(
            {"id": ev["id"], "box": ev["data_box"], "zones": zones, "triage": triage}
        )

    hits_rows: list[dict[str, Any]] = []
    for cam in sorted(zone_hits.keys()):
        for z in sorted(zone_hits[cam].keys()):
            for lbl, n in sorted(zone_hits[cam][z].items(), key=lambda x: -x[1]):
                hits_rows.append(
                    {
                        "camera": cam,
                        "zone": z if z != "__none__" else "(no zone)",
                        "label": lbl,
                        "n": n,
                        "fp_in_triage": zone_fps[cam][z].get(lbl, 0),
                    }
                )

    candidates: list[dict[str, Any]] = []
    for (cam, lbl), evs in cam_label_events.items():
        target = [e for e in evs if (not e["zones"] or e["triage"] == "fp") and e["box"]]
        if len(target) < 5:
            continue
        for cl in _cluster_centroids(target, tol=0.10):
            if len(cl) < 5:
                continue
            fp_count = sum(1 for e in cl if e["triage"] == "fp")
            no_zone_count = sum(1 for e in cl if not e["zones"])
            triaged_count = sum(1 for e in cl if e["triage"] in ("fp", "tp"))
            fp_rate = (fp_count / triaged_count) if triaged_count > 0 else None
            reason: str | None = None
            if fp_rate is not None and fp_rate >= 0.7:
                reason = f"fp_rate={fp_rate:.0%} of {triaged_count} triaged"
            elif no_zone_count >= 5 and triaged_count == 0:
                reason = f"{no_zone_count} events outside any zone (no triage yet)"
            if not reason:
                continue
            cx, cy = cl[0]["_cx"], cl[0]["_cy"]
            candidates.append(
                {
                    "camera": cam,
                    "label": lbl,
                    "cluster_size": len(cl),
                    "centroid_x": round(cx, 3),
                    "centroid_y": round(cy, 3),
                    "sample_event_id": cl[0]["id"],
                    "reason": reason,
                }
            )

    return {"days": days, "hits": hits_rows, "mask_candidates": candidates}
