"""Detector inference budget vs configured detect.fps demand.

Live via Frigate's /api/stats and /api/config. Refuses to suggest raising
any camera above detect.fps=10 (matches the upstream tuning guardrail).
"""

from __future__ import annotations

from typing import Any

from marcellus.frigate_api import FrigateClient

MAX_DETECT_FPS = 10


def analyze(*, frigate_base_url: str) -> dict[str, Any]:
    with FrigateClient(frigate_base_url) as client:
        stats = client.stats()
        config = client.config()

    detectors_out: list[dict[str, Any]] = []
    total_budget = 0.0
    for name, d in sorted(stats.get("detectors", {}).items()):
        inf = float(d.get("inference_speed", 0.0))
        capacity = (1000.0 / inf) if inf > 0 else 0.0
        total_budget += capacity
        detectors_out.append(
            {
                "name": name,
                "inference_ms": round(inf, 2),
                "implied_fps_per_detector": round(capacity, 1),
                "thermal_flag": inf > 25,
            }
        )

    cameras = config.get("cameras", {})
    cam_stats = stats.get("cameras", {})
    cameras_out: list[dict[str, Any]] = []
    total_demand = 0.0
    for cam, ccfg in sorted(cameras.items()):
        if not ccfg.get("enabled", True):
            continue
        det_fps = ccfg.get("detect", {}).get("fps")
        if det_fps is None:
            det_fps = config.get("detect", {}).get("fps", 5)
        total_demand += det_fps

        obs = cam_stats.get(cam, {})
        obs_det = float(obs.get("detection_fps") or 0.0)
        obs_skip = float(obs.get("skipped_fps") or 0.0)
        gap = ((det_fps - obs_det) / det_fps * 100) if det_fps > 0 else 0.0
        cameras_out.append(
            {
                "camera": cam,
                "configured_detect_fps": det_fps,
                "observed_detection_fps": round(obs_det, 2),
                "observed_skipped_fps": round(obs_skip, 2),
                "gap_pct": round(gap, 0),
            }
        )

    headroom = total_budget - total_demand
    util = (total_demand / total_budget * 100) if total_budget > 0 else 0.0

    recs: list[str] = []
    if util > 85:
        recs.append(
            f"Detectors at {util:.0f}% util. Reduce detect.fps on the largest consumers first."
        )
        sorted_by_fps = sorted(cameras_out, key=lambda r: -r["configured_detect_fps"])
        cuts = 0
        for row in sorted_by_fps:
            if cuts >= 2:
                break
            if row["configured_detect_fps"] >= 5:
                recs.append(
                    f"  cameras.{row['camera']}.detect.fps: "
                    f"{row['configured_detect_fps']} -> {row['configured_detect_fps'] - 1}"
                )
                cuts += 1
    elif util < 40:
        bumps = [
            r for r in cameras_out
            if r["observed_skipped_fps"] > 1.0 and r["configured_detect_fps"] < MAX_DETECT_FPS
        ]
        if bumps:
            recs.append(f"Detectors at {util:.0f}% util. Cameras with skipped frames could go up.")
            for row in bumps[:3]:
                new = min(MAX_DETECT_FPS, row["configured_detect_fps"] + 1)
                recs.append(
                    f"  cameras.{row['camera']}.detect.fps: "
                    f"{row['configured_detect_fps']} -> {new} "
                    f"(skipping ~{row['observed_skipped_fps']:.1f}fps)"
                )
        else:
            recs.append(f"Detectors at {util:.0f}% util. No skipped frames; nothing to change.")
    else:
        recs.append(f"Detectors at {util:.0f}% util. Healthy band.")
    recs.append(f"Hard guardrail: never propose detect.fps > {MAX_DETECT_FPS}.")

    return {
        "detectors": detectors_out,
        "cameras": cameras_out,
        "total_budget_fps": round(total_budget, 1),
        "total_demand_fps": round(total_demand, 1),
        "headroom_fps": round(headroom, 1),
        "utilization_pct": round(util, 0),
        "recommendations": recs,
    }
