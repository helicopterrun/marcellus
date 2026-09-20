"""A/B comparison of per-camera motion activity across two date ranges."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from marcellus.frigate_api import FrigateAPIError, FrigateClient


def parse_range(spec: str) -> tuple[str, str]:
    spec = spec.strip().lower()
    if spec == "today":
        d = date.today().isoformat()
        return d, d
    if spec == "yesterday":
        d = (date.today() - timedelta(days=1)).isoformat()
        return d, d
    if ".." in spec:
        a, b = spec.split("..", 1)
    else:
        a = b = spec
    for x in (a, b):
        datetime.strptime(x, "%Y-%m-%d")  # validate
    if a > b:
        a, b = b, a
    return a, b


def _aggregate(days_data: list[dict[str, Any]], lo: str, hi: str) -> dict[str, float]:
    out = {"motion": 0.0, "duration": 0.0, "events": 0.0, "objects": 0.0}
    for d in days_data:
        if not (lo <= d["day"] <= hi):
            continue
        for h in d["hours"]:
            if h["duration"] <= 0:
                continue
            out["motion"] += h["motion"]
            out["duration"] += h["duration"]
            out["events"] += h["events"]
            out["objects"] += h["objects"]
    return out


def _metrics(agg: dict[str, float]) -> dict[str, float]:
    if agg["duration"] == 0:
        return {"mu_per_hr": 0.0, "events_per_hr": 0.0, "yield_per_kmu": 0.0, "hours": 0.0}
    hrs = agg["duration"] / 3600.0
    return {
        "mu_per_hr": agg["motion"] / hrs,
        "events_per_hr": agg["events"] / hrs,
        "yield_per_kmu": (agg["events"] / agg["motion"] * 1000.0) if agg["motion"] else 0.0,
        "hours": hrs,
    }


def _classify(base: dict[str, float], tgt: dict[str, float]) -> tuple[str, str]:
    bm, tm = base["mu_per_hr"], tgt["mu_per_hr"]
    by, ty = base["yield_per_kmu"], tgt["yield_per_kmu"]
    if bm < 1 and tm < 1:
        return "flat", "-"
    ratio = (tm / bm) if bm > 0 else float("inf")
    if ratio < 0.5 and tm < bm - 50:
        return "quiet drop", "no action"
    if ratio > 2.0 and tm > bm + 100:
        yield_collapsed = (by > 0 and ty < by * 0.5) or (by > 5 and ty < 1)
        yield_held = ty >= max(1.0, by * 0.7)
        if yield_collapsed:
            return "noise spike", "+2 motion.threshold (then mask-candidate review)"
        if yield_held:
            return "real activity spike", "no action"
        return "mixed spike", "+1 motion.threshold, re-check next cycle"
    return "flat", "-"


def analyze(
    *,
    frigate_base_url: str,
    baseline: str,
    target: str,
) -> dict[str, Any]:
    b_lo, b_hi = parse_range(baseline)
    t_lo, t_hi = parse_range(target)
    rows: list[dict[str, Any]] = []
    with FrigateClient(frigate_base_url) as client:
        config = client.config()
        for cam, ccfg in sorted(config.get("cameras", {}).items()):
            if not ccfg.get("enabled", True):
                continue
            try:
                days_data = client.recordings_summary(cam)
            except FrigateAPIError:
                rows.append({"camera": cam, "error": "no recordings summary"})
                continue
            b = _metrics(_aggregate(days_data, b_lo, b_hi))
            t = _metrics(_aggregate(days_data, t_lo, t_hi))
            if b["hours"] < 0.05 or t["hours"] < 0.05:
                rows.append({"camera": cam, "skip": "insufficient data"})
                continue
            label, suggestion = _classify(b, t)
            low_conf = b["hours"] < 1.0 or t["hours"] < 1.0
            if low_conf:
                label += " (low-confidence)"
                if suggestion not in ("-", "no action"):
                    suggestion = "extend target window before acting"
            ratio = (t["mu_per_hr"] / b["mu_per_hr"]) if b["mu_per_hr"] > 0 else float("inf")
            rows.append(
                {
                    "camera": cam,
                    "class": label,
                    "base_mu_per_hr": round(b["mu_per_hr"], 0),
                    "tgt_mu_per_hr": round(t["mu_per_hr"], 0),
                    "ratio": "inf" if ratio == float("inf") else round(ratio, 1),
                    "base_yield_per_kmu": round(b["yield_per_kmu"], 2),
                    "tgt_yield_per_kmu": round(t["yield_per_kmu"], 2),
                    "base_hours": round(b["hours"], 1),
                    "tgt_hours": round(t["hours"], 1),
                    "motion_threshold": ccfg.get("motion", {}).get("threshold"),
                    "suggestion": suggestion,
                }
            )
    return {
        "baseline": f"{b_lo}..{b_hi}",
        "target": f"{t_lo}..{t_hi}",
        "rows": rows,
    }
