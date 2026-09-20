"""Motion tuning page: per-camera activity (single-window) or A/B comparison.

Single-window mode (no `baseline`): one row per camera with class label and
motion-units-per-hour, sorted by activity.

Compare mode (`baseline` set): A/B with delta classification (noise spike,
real activity spike, quiet drop, flat).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from marcellus.analysis import motion_active, motion_compare
from marcellus.errors import error_detail
from marcellus.frigate_api import FrigateAPIError
from marcellus.routes._cache import ttl_page_cache

router = APIRouter(tags=["motion"])


def _classify_single(mu_per_hr: float, yield_per_kmu: float) -> str:
    """css class for the activity bucket (mirrors motion_active.classify rubric)."""
    if mu_per_hr < 50:
        return "muted"
    if mu_per_hr < 500:
        return "ok" if yield_per_kmu >= 5 else "warn"
    if mu_per_hr < 3000:
        return "ok" if yield_per_kmu >= 2 else "noise"
    return "ok" if yield_per_kmu >= 1 else "noise"


def _delta_css(label: str) -> str:
    if "noise spike" in label:
        return "noise"
    if "mixed spike" in label:
        return "mixed"
    if "real activity spike" in label or "quiet drop" in label:
        return "muted"
    return "muted"


@router.get("/motion", response_class=HTMLResponse)
@ttl_page_cache(seconds=60)
def motion_view(
    request: Request,
    baseline: str = "",
    target: str = "",
) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    today = date.today()
    presets = {
        "today_only": ("", "today"),
        "yesterday_only": ("", "yesterday"),
        "today_vs_prior3": (
            f"{(today - timedelta(days=3)).isoformat()}..{(today - timedelta(days=1)).isoformat()}",
            "today",
        ),
        "last7_vs_prior7": (
            f"{(today - timedelta(days=14)).isoformat()}"
            f"..{(today - timedelta(days=8)).isoformat()}",
            f"{(today - timedelta(days=7)).isoformat()}"
            f"..{(today - timedelta(days=1)).isoformat()}",
        ),
    }

    mode: str | None = None
    rows: list[dict[str, Any]] = []
    error: str | None = None
    status_code = 200
    range_labels = {"baseline": "", "target": ""}

    if baseline or target:
        try:
            if baseline:
                mode = "compare"
                result = motion_compare.analyze(
                    frigate_base_url=settings.frigate.base_url,
                    baseline=baseline,
                    target=target or "today",
                )
                range_labels["baseline"] = result["baseline"]
                range_labels["target"] = result["target"]
                for row in result["rows"]:
                    if "error" in row or "skip" in row:
                        continue
                    row["css"] = _delta_css(row.get("class", ""))
                    rows.append(row)
            else:
                mode = "single"
                # `target` promises a date/preset -- "" or "today" (today
                # only), "yesterday", a single `YYYY-MM-DD`, or a
                # `YYYY-MM-DD..YYYY-MM-DD` range -- but this used to hard-code
                # every one of those except "today" to a flat 14 days, so
                # "yesterday only" silently became "the last 14 days".
                # `parse_range` (already used by Compare mode below) knows
                # this vocabulary; reuse it rather than re-parsing target here.
                try:
                    lo, hi = motion_compare.parse_range(target or "today")
                except ValueError as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=error_detail(
                            "invalid_target", f"bad target {target!r}: {exc}"
                        ),
                    ) from exc
                # `days`/`until` bound the query on both ends: `days` counts
                # back from today to `lo` (the start), and `until=hi` caps
                # the end -- e.g. "yesterday" must not pick up today's
                # partial data. `max(..., 1)` guards a `target` in the
                # future, where the subtraction would otherwise go negative.
                days = max((date.today() - date.fromisoformat(lo)).days + 1, 1)
                result = motion_active.analyze(
                    frigate_base_url=settings.frigate.base_url,
                    days=days,
                    until=hi,
                )
                range_labels["target"] = result.get("since", target or "today")
                for row in result["rows"]:
                    if "error" in row:
                        continue
                    row["css"] = _classify_single(
                        float(row.get("mu_per_hr", 0)),
                        float(row.get("yield_per_kmu", 0)),
                    )
                    rows.append(row)
                rows.sort(key=lambda r: -float(r.get("mu_per_hr", 0)))
        except FrigateAPIError as exc:
            error = f"Frigate API unreachable: {exc}"
            status_code = 503
        except ValueError as exc:
            error = f"date parse error: {exc}"
            status_code = 503

    return templates.TemplateResponse(
        request,
        "motion.html",
        {
            "baseline": baseline,
            "target": target,
            "mode": mode,
            "rows": rows,
            "error": error,
            "presets": presets,
            "range_labels": range_labels,
            "counts": {},  # header expects this; motion page doesn't need counts
        },
        status_code=status_code,
    )
