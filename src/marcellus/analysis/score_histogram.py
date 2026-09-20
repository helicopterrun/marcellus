"""Per camera×label score distribution + suggested min_score and threshold.

We use:
- `top_score` distribution to inform `threshold` (smoothed-median proxy)
- `score` distribution to inform `min_score` (per-frame floor; conservative)

When sidecar triage labels exist for (camera, label), suggestions are
computed from the `tp`-labeled subset (confidence=high/med depending on
sample size). Without triage data, suggestions come from the whole
distribution with confidence=low.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from marcellus.db import (
    open_joined,
    parse_event_data,
    percentile,
    time_window_clause,
)

SCORE_BINS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01]


def bucket(score: float) -> str:
    """Label the 0.05-wide bin `score` falls in.

    Anything under the lowest bin edge gets its own label rather than being
    reported as `[0.45,0.50)`, which claimed a floor the score never had.
    """
    if score < SCORE_BINS[0]:
        return f"<{SCORE_BINS[0]:.2f}"
    for hi in SCORE_BINS:
        if score < hi:
            return f"[{hi-0.05:.2f},{hi:.2f})"
    return ">=0.95"


def analyze(
    *,
    frigate_db: str | Path,
    sidecar_db: str | Path,
    days: int = 14,
    camera: str | None = None,
    label: str | None = None,
    min_samples: int = 30,
) -> dict[str, Any]:
    where, params = time_window_clause(days)
    if camera:
        where += " AND e.camera = ?"
        params.append(camera)
    if label:
        where += " AND e.label = ?"
        params.append(label)

    sql = f"""
        SELECT e.id, e.camera, e.label, e.score, e.top_score, e.data,
               t.label AS triage
          FROM event e
          LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
         WHERE {where}
    """

    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    bucket_counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        for row in conn.execute(sql, params):
            ev = parse_event_data(row)
            score = ev["data_score"] if ev["data_score"] is not None else ev["score"]
            top = ev["data_top_score"] if ev["data_top_score"] is not None else ev["top_score"]
            if score is None or top is None:
                continue
            key = (ev["camera"], ev["label"])
            cells[key].append({"score": score, "top_score": top, "triage": row["triage"]})
            bucket_counts[key][bucket(float(top))] += 1
    finally:
        conn.close()

    rows: list[dict[str, Any]] = []
    for (cam, lbl), evs in sorted(cells.items()):
        n = len(evs)
        n_tp = sum(1 for e in evs if e["triage"] == "tp")
        n_fp = sum(1 for e in evs if e["triage"] == "fp")

        if n_tp >= max(10, min_samples // 3):
            sample = [e for e in evs if e["triage"] == "tp"]
            confidence = "high" if n_tp >= min_samples else "med"
        else:
            sample = evs
            confidence = "low" if n < min_samples else "med"

        if n < 10:
            rows.append(
                {
                    "camera": cam, "label": lbl, "n": n, "n_tp": n_tp, "n_fp": n_fp,
                    "median_score": None, "p10_top": None, "p25_top": None,
                    "p50_top": None, "p75_top": None,
                    "suggested_min_score": None, "suggested_threshold": None,
                    "confidence": "sparse",
                }
            )
            continue

        scores = [e["score"] for e in sample]
        tops = [e["top_score"] for e in sample]
        p10 = percentile(tops, 10)
        p25 = percentile(tops, 25)
        p50 = median(tops)
        p75 = percentile(tops, 75)
        med_score = median(scores)

        suggested_min_score = max(0.50, round(p10 - 0.05, 2))
        suggested_threshold = max(0.55, round(p25, 2))
        if suggested_min_score >= suggested_threshold:
            suggested_min_score = max(0.50, round(suggested_threshold - 0.05, 2))

        rows.append(
            {
                "camera": cam, "label": lbl, "n": n, "n_tp": n_tp, "n_fp": n_fp,
                "median_score": round(med_score, 2),
                "p10_top": round(p10, 2),
                "p25_top": round(p25, 2),
                "p50_top": round(p50, 2),
                "p75_top": round(p75, 2),
                "suggested_min_score": suggested_min_score,
                "suggested_threshold": suggested_threshold,
                "confidence": confidence,
            }
        )

    buckets_out: dict[str, dict[str, int]] = {}
    for (cam, lbl), counts in bucket_counts.items():
        buckets_out[f"{cam}|{lbl}"] = dict(counts)

    return {"rows": rows, "buckets": buckets_out, "days": days}
