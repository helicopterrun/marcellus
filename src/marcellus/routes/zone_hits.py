"""Zone-hits page: per-camera zone hit counts + mask candidates.

Same compute as `fsc analysis zone-hits` / `GET /analysis/zone-hits`, surfaced
as a page (mirrors routes/fps_budget.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from marcellus.analysis import zone_hits
from marcellus.routes._cache import ttl_page_cache

router = APIRouter(tags=["zone-hits"])


@router.get("/zone-hits", response_class=HTMLResponse)
@ttl_page_cache(seconds=60)
def zone_hits_view(
    request: Request, days: int = 30, camera: str | None = None
) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    result: dict[str, Any] | None = None
    error: str | None = None
    status_code = 200
    try:
        result = zone_hits.analyze(
            frigate_db=settings.frigate.db_path,
            sidecar_db=settings.sidecar.db_path,
            days=days,
            camera=camera,
        )
    except Exception as exc:  # noqa: BLE001 -- surface, don't 500, like /motion
        error = str(exc)
        # Non-200 so `ttl_page_cache` skips it -- a 200 here stuck a stale
        # error banner in the cache for the full TTL past the outage.
        status_code = 503

    cameras = sorted({h["camera"] for h in result["hits"]}) if result else []
    return templates.TemplateResponse(
        request,
        "zone_hits.html",
        {
            "result": result,
            "error": error,
            "days": days,
            "camera": camera or "",
            "cameras": cameras,
            "counts": {},
        },
        status_code=status_code,
    )
