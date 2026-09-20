"""Score-histogram page: per (camera, label) score distribution + suggestions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from marcellus.analysis import score_histogram
from marcellus.db import open_joined
from marcellus.routes._cache import ttl_page_cache

router = APIRouter(tags=["score-histogram"])


def _event_filter_options(frigate_db: str, sidecar_db: str) -> dict[str, list[str]]:
    """Distinct camera/label values from the event table for dropdowns."""
    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        cam_q = "SELECT DISTINCT camera FROM event ORDER BY camera"
        lbl_q = "SELECT DISTINCT label FROM event ORDER BY label"
        cams = [r["camera"] for r in conn.execute(cam_q)]
        labels = [r["label"] for r in conn.execute(lbl_q)]
    finally:
        conn.close()
    return {"cameras": cams, "labels": labels}


def _confidence_css(confidence: str) -> str:
    return {
        "high": "ok",
        "med": "warn",
        "low": "noise",
        "sparse": "muted",
    }.get(confidence, "muted")


@router.get("/score-histogram", response_class=HTMLResponse)
@ttl_page_cache(seconds=60)
def score_histogram_view(
    request: Request,
    days: int = Query(default=14, ge=1, le=365),
    camera: str = "",
    label: str = "",
    min_samples: int = Query(default=30, ge=1),
) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    # Unlike its siblings (motion/zone-hits/fps-budget), this had no
    # try/except at all -- a locked or unreachable DB 500ed here instead of
    # showing the same error panel. Guarded the same way, including the
    # non-200 status so `ttl_page_cache` doesn't stick a stale error for the
    # TTL.
    result: dict[str, Any] = {"rows": [], "buckets": {}}
    options: dict[str, list[str]] = {"cameras": [], "labels": []}
    error: str | None = None
    status_code = 200
    try:
        result = score_histogram.analyze(
            frigate_db=settings.frigate.db_path,
            sidecar_db=settings.sidecar.db_path,
            days=days,
            camera=camera or None,
            label=label or None,
            min_samples=min_samples,
        )
        # Attach a css class per row based on confidence.
        for row in result["rows"]:
            row["css"] = _confidence_css(row["confidence"])

        options = _event_filter_options(
            str(settings.frigate.db_path), str(settings.sidecar.db_path)
        )
    except Exception as exc:  # noqa: BLE001 -- surface, don't 500, like /motion
        error = str(exc)
        status_code = 503

    return templates.TemplateResponse(
        request,
        "score_histogram.html",
        {
            "result": result,
            "error": error,
            "filters": {
                "days": days,
                "camera": camera,
                "label": label,
                "min_samples": min_samples,
            },
            "cameras": options["cameras"],
            "labels": options["labels"],
            "counts": {},
        },
        status_code=status_code,
    )
