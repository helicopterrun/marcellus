"""Per-camera event rate + noise indicators (DB-only).

Tells you which cameras are firing too often (or not enough), informing
`motion.threshold` / `motion.contour_area` tuning. Frigate events are
downstream of the motion detector, so a flooded event stream is a proxy
for an overly sensitive motion config.
"""

from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from marcellus.db import open_frigate_ro, percentile, time_window_clause


def analyze(*, frigate_db: str | Path, days: int = 14) -> list[dict[str, Any]]:
    where, params = time_window_clause(days)
    floor_ts = time.time() - days * 86400

    sql = f"""
        SELECT camera, label,
               CAST((start_time - ?) / 3600 AS INTEGER) AS hour_bucket
          FROM event
         WHERE {where}
    """
    night_sql = f"""
        SELECT camera, COUNT(*) AS n
          FROM event
         WHERE {where}
           AND CAST(strftime('%H', start_time, 'unixepoch', 'localtime') AS INTEGER)
               NOT BETWEEN 6 AND 21
         GROUP BY camera
    """

    per_cam_hourly: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    per_cam_label: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    total_counts: dict[str, int] = defaultdict(int)
    night_counts: dict[str, int] = defaultdict(int)

    conn = open_frigate_ro(frigate_db)
    try:
        for row in conn.execute(sql, [floor_ts] + params):
            cam = row["camera"]
            per_cam_hourly[cam][row["hour_bucket"]] += 1
            per_cam_label[cam][row["label"]] += 1
            total_counts[cam] += 1
        for row in conn.execute(night_sql, params):
            night_counts[row["camera"]] = row["n"]
    finally:
        conn.close()

    rows: list[dict[str, Any]] = []
    total_hours = days * 24
    for cam in sorted(per_cam_hourly.keys()):
        hourly = list(per_cam_hourly[cam].values())
        if len(hourly) < total_hours:
            hourly.extend([0] * (total_hours - len(hourly)))
        avg = mean(hourly)
        p95 = percentile([float(x) for x in hourly], 95)
        peak = max(hourly)
        spikiness = (p95 / avg) if avg > 0 else 0.0
        total = total_counts[cam]
        night = night_counts.get(cam, 0)
        night_ratio = (night / total) if total else 0.0

        if spikiness > 4.0 or p95 > 30:
            suggestion = "+2 motion.threshold (noisy)"
        elif avg < 0.05:
            suggestion = "-2 motion.threshold (quiet - possibly missing motion)"
        else:
            suggestion = "-"
        if night_ratio > 0.6:
            suggestion += "; consider improve_contrast review"

        top_labels = sorted(per_cam_label[cam].items(), key=lambda x: -x[1])[:3]
        rows.append(
            {
                "camera": cam,
                "events_total": total,
                "events_per_hr_avg": round(avg, 2),
                "events_per_hr_p95": int(round(p95)),
                "peak_hour_count": peak,
                "spikiness": round(spikiness, 1),
                "night_ratio": round(night_ratio, 2),
                "top_labels": [{"label": lbl, "n": n} for lbl, n in top_labels],
                "suggestion": suggestion,
            }
        )

    return rows
