"""Measured `detect.annotation_offset` (ms) per camera, via template matching.

This module is heavier than the rest: it imports cv2 + numpy + httpx and
issues many HTTP calls per event. Install the optional extra:

    pip install "marcellus[annotation]"

The CLI / HTTP entry points raise `AnnotationOffsetUnavailable` if the
extra isn't present so users get a clean error rather than a stack trace.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from marcellus.db import (
    open_frigate_ro,
    parse_event_data,
    parse_path_data,
    percentile,
    time_window_clause,
)


class AnnotationOffsetUnavailable(RuntimeError):
    """Optional cv2/numpy/httpx-image stack not installed."""


def _require_deps() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AnnotationOffsetUnavailable(
            "cv2 / numpy not installed. Install with `pip install \"marcellus[annotation]\"`."
        ) from exc
    return cv2, np


# Canonical implementation moved to db.parse_path_data (the /v1 read layer
# needs it without the [annotation] extras); kept as an alias for callers here.
_parse_path_data = parse_path_data


def _event_qualifies(
    path: list[tuple[float, float, float]],
    duration_min_s: float,
    motion_min_norm: float,
) -> bool:
    if len(path) < 6:
        return False
    duration = path[-1][2] - path[0][2]
    if duration < duration_min_s:
        return False
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    return extent >= motion_min_norm


def _sample_trajectory(
    path: list[tuple[float, float, float]], n: int
) -> list[tuple[float, float, float]]:
    if len(path) <= n:
        return list(path)
    step = (len(path) - 1) / (n - 1)
    return [path[round(i * step)] for i in range(n)]


def _fetch_image(client: Any, url: str, np_mod: Any, cv2_mod: Any) -> Any:
    try:
        r = client.get(url)
    except Exception:
        return None
    if r.status_code != 200 or not r.content:
        return None
    arr = np_mod.frombuffer(r.content, dtype=np_mod.uint8)
    return cv2_mod.imdecode(arr, cv2_mod.IMREAD_COLOR)


def _fit_template_to_frame(
    template: Any, frame_shape: tuple[int, int], box_norm: list[float] | None, cv2_mod: Any
) -> Any:
    h, w = frame_shape[:2]
    th, tw = template.shape[:2]
    if box_norm and len(box_norm) == 4:
        box_w_px = max(8, int(box_norm[2] * w))
        box_h_px = max(8, int(box_norm[3] * h))
        target = max(box_w_px, box_h_px)
        scale = target / max(th, tw)
        scale = min(scale, min(w, h) * 0.25 / max(th, tw))
        scale = max(scale, 32 / max(th, tw))
        new_w = max(8, int(tw * scale))
        new_h = max(8, int(th * scale))
        return cv2_mod.resize(template, (new_w, new_h), interpolation=cv2_mod.INTER_AREA)
    max_t = min(w // 4, h // 4, 240)
    if max(th, tw) <= max_t:
        return template
    scale = max_t / max(th, tw)
    return cv2_mod.resize(
        template, (max(8, int(tw * scale)), max(8, int(th * scale))),
        interpolation=cv2_mod.INTER_AREA,
    )


def _match_score(template: Any, frame: Any, norm_xy: tuple[float, float], cv2_mod: Any) -> float:
    h, w = frame.shape[:2]
    cx, cy = int(norm_xy[0] * w), int(norm_xy[1] * h)
    th, tw = template.shape[:2]
    pad_x = int(tw * 2.0)
    pad_y = int(th * 2.0)
    x0, y0 = max(0, cx - pad_x), max(0, cy - pad_y)
    x1, y1 = min(w, cx + pad_x), min(h, cy + pad_y)
    roi = frame[y0:y1, x0:x1]
    if roi.shape[0] < th or roi.shape[1] < tw:
        return float("nan")
    result = cv2_mod.matchTemplate(roi, template, cv2_mod.TM_CCOEFF_NORMED)
    return float(result.max())


def _measure_event(
    client: Any,
    cv2_mod: Any,
    np_mod: Any,
    camera: str,
    event_id: str,
    path: list[tuple[float, float, float]],
    box_norm: list[float] | None,
    samples_per_event: int,
    search_window_ms: int,
    step_ms: int,
    min_match_score: float,
) -> int | None:
    template = _fetch_image(
        client, f"/api/events/{event_id}/thumbnail.jpg", np_mod, cv2_mod
    )
    if template is None:
        return None
    probe_t = path[len(path) // 2][2]
    probe = _fetch_image(
        client, f"/api/{camera}/recordings/{probe_t:.3f}/snapshot.jpg", np_mod, cv2_mod
    )
    if probe is None:
        return None
    template = _fit_template_to_frame(template, probe.shape, box_norm, cv2_mod)
    samples = _sample_trajectory(path, samples_per_event)
    candidates = list(range(-search_window_ms, search_window_ms + 1, step_ms))
    score_sum = {off: 0.0 for off in candidates}
    n_used = {off: 0 for off in candidates}
    for (nx, ny, t_det) in samples:
        for off_ms in candidates:
            frame = _fetch_image(
                client,
                f"/api/{camera}/recordings/{t_det + off_ms / 1000.0:.3f}/snapshot.jpg",
                np_mod,
                cv2_mod,
            )
            if frame is None:
                continue
            s = _match_score(template, frame, (nx, ny), cv2_mod)
            if s != s:  # nan
                continue
            score_sum[off_ms] += s
            n_used[off_ms] += 1
    best_off, best_mean = None, float("-inf")
    for off_ms in candidates:
        if n_used[off_ms] == 0:
            continue
        m = score_sum[off_ms] / n_used[off_ms]
        if m > best_mean:
            best_off, best_mean = off_ms, m
    if best_off is None or best_mean < min_match_score:
        return None
    return best_off


def analyze(
    *,
    frigate_db: str | Path,
    frigate_base_url: str,
    days: int = 7,
    camera: str | None = None,
    min_events: int = 10,
    target_events: int = 20,
    min_top_score: float = 0.80,
    min_duration_s: float = 3.0,
    min_motion_norm: float = 0.30,
    samples_per_event: int = 5,
    search_window_ms: int = 3000,
    step_ms: int = 250,
    min_match_score: float = 0.40,
    round_to_ms: int = 50,
) -> list[dict[str, Any]]:
    cv2_mod, np_mod = _require_deps()
    import httpx

    where, params = time_window_clause(days)
    where += " AND has_snapshot = 1 AND label = 'person'"
    if camera:
        where += " AND camera = ?"
        params.append(camera)
    sql = f"""
        SELECT id, camera, label, start_time, end_time, top_score, data
          FROM event
         WHERE {where}
         ORDER BY start_time DESC
    """

    by_cam: dict[str, list[dict[str, Any]]] = defaultdict(list)
    conn = open_frigate_ro(frigate_db)
    try:
        for row in conn.execute(sql, params):
            ev = parse_event_data(row)
            top = ev["data_top_score"] if ev["data_top_score"] is not None else ev["top_score"]
            if top is None or top < min_top_score:
                continue
            raw = ev["_data_parsed"].get("path_data")
            path = _parse_path_data(raw)
            if not _event_qualifies(path, min_duration_s, min_motion_norm):
                continue
            by_cam[ev["camera"]].append(
                {"id": ev["id"], "path": path, "box": ev["data_box"]}
            )
    finally:
        conn.close()

    base = frigate_base_url.rstrip("/")
    results: list[dict[str, Any]] = []
    with httpx.Client(base_url=base, timeout=10.0) as client:
        for cam, evs in sorted(by_cam.items()):
            evs = evs[:target_events]
            offsets: list[int] = []
            for ev in evs:
                off = _measure_event(
                    client, cv2_mod, np_mod, cam, ev["id"], ev["path"], ev.get("box"),
                    samples_per_event, search_window_ms, step_ms, min_match_score,
                )
                if off is not None:
                    offsets.append(off)
            if not offsets:
                results.append(
                    {
                        "camera": cam,
                        "n_qualifying_events": len(evs),
                        "n_contributing_events": 0,
                        "median_offset_ms": None,
                        "iqr_ms": None,
                        "suggested_offset_ms": None,
                        "confidence": "insufficient",
                    }
                )
                continue
            med = statistics.median(offsets)
            p25 = percentile([float(x) for x in offsets], 25)
            p75 = percentile([float(x) for x in offsets], 75)
            iqr = p75 - p25
            suggested = int(round(med / round_to_ms) * round_to_ms)
            if len(offsets) < min_events or iqr > 500:
                conf = "low"
            elif iqr > 250:
                conf = "med"
            else:
                conf = "high"
            results.append(
                {
                    "camera": cam,
                    "n_qualifying_events": len(evs),
                    "n_contributing_events": len(offsets),
                    "median_offset_ms": int(med),
                    "p25_ms": int(p25),
                    "p75_ms": int(p75),
                    "iqr_ms": int(iqr),
                    "suggested_offset_ms": suggested,
                    "confidence": conf,
                }
            )
    return results
