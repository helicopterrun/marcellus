"""Raw motion activity puller via /api/<camera>/recordings/summary.

`motion` is an accumulator over motion regions per segment, NOT wall-clock
seconds. mu/hr is per-camera-relative only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from marcellus.frigate_api import FrigateAPIError, FrigateClient


def classify(mu_per_hr: float, yield_per_kmu: float) -> str:
    if mu_per_hr < 50:
        return "quiet"
    if mu_per_hr < 500:
        return "healthy" if yield_per_kmu >= 5 else "low-yield"
    if mu_per_hr < 3000:
        return "busy" if yield_per_kmu >= 2 else "noisy"
    return "very busy" if yield_per_kmu >= 1 else "noise-dominated"


def _aggregate(
    days_data: list[dict[str, Any]], since_day: str, until_day: str
) -> dict[str, float]:
    out = {"motion": 0.0, "duration": 0.0, "events": 0.0, "objects": 0.0, "hours_with_data": 0}
    for d in days_data:
        if not (since_day <= d["day"] <= until_day):
            continue
        for h in d["hours"]:
            if h["duration"] <= 0:
                continue
            out["motion"] += h["motion"]
            out["duration"] += h["duration"]
            out["events"] += h["events"]
            out["objects"] += h["objects"]
            out["hours_with_data"] += 1
    return out


def analyze(
    *, frigate_base_url: str, days: int = 14, until: str | None = None
) -> dict[str, Any]:
    with FrigateClient(frigate_base_url) as client:
        config = client.config()
        stats = client.stats()
        cameras_cfg = config.get("cameras", {})
        cam_stats = stats.get("cameras", {})
        since = (date.today() - timedelta(days=days - 1)).isoformat()
        until_day = until or date.today().isoformat()

        rows: list[dict[str, Any]] = []
        for cam, ccfg in sorted(cameras_cfg.items()):
            if not ccfg.get("enabled", True):
                continue
            try:
                days_data = client.recordings_summary(cam)
            except FrigateAPIError:
                rows.append({"camera": cam, "error": "no recordings summary"})
                continue
            agg = _aggregate(days_data, since, until_day)
            if agg["duration"] == 0:
                continue
            hrs = agg["duration"] / 3600.0
            mu_per_hr = agg["motion"] / hrs
            events_per_hr = agg["events"] / hrs
            objects_per_hr = agg["objects"] / hrs
            yield_per_kmu = (
                (agg["events"] / agg["motion"] * 1000.0) if agg["motion"] else 0.0
            )
            obs = cam_stats.get(cam, {})
            obs_det_fps = float(obs.get("detection_fps") or 0.0)
            cfg_det_fps = (
                ccfg.get("detect", {}).get("fps")
                or config.get("detect", {}).get("fps", 5)
            )
            mcfg = ccfg.get("motion", {}) or {}
            rows.append(
                {
                    "camera": cam,
                    "class": classify(mu_per_hr, yield_per_kmu),
                    "mu_per_hr": round(mu_per_hr, 0),
                    "events_per_hr": round(events_per_hr, 2),
                    "objects_per_hr": round(objects_per_hr, 1),
                    "yield_per_kmu": round(yield_per_kmu, 2),
                    "obs_det_fps": round(obs_det_fps, 2),
                    "cfg_det_fps": cfg_det_fps,
                    "motion_threshold": mcfg.get("threshold"),
                    "motion_contour_area": mcfg.get("contour_area"),
                    "motion_improve_contrast": mcfg.get("improve_contrast"),
                    "hours_with_data": agg["hours_with_data"],
                }
            )

    return {"days": days, "since": since, "rows": rows}
