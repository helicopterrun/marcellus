"""FPS-budget page: live snapshot of detector inference budget vs demand."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from marcellus.analysis import fps_budget
from marcellus.frigate_api import FrigateAPIError
from marcellus.routes._cache import ttl_page_cache

router = APIRouter(tags=["fps-budget"])


def _util_css(util_pct: float) -> str:
    """Color band for the headline utilization number."""
    if util_pct > 85:
        return "noise"
    if util_pct > 65:
        return "warn"
    if util_pct < 20:
        return "muted"
    return "ok"


@router.get("/fps-budget", response_class=HTMLResponse)
@ttl_page_cache(seconds=60)
def fps_budget_view(request: Request) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    result: dict[str, Any] | None = None
    error: str | None = None
    util_css = "muted"
    status_code = 200
    try:
        result = fps_budget.analyze(frigate_base_url=settings.frigate.base_url)
        util_css = _util_css(float(result["utilization_pct"]))
    except FrigateAPIError as exc:
        error = f"Frigate API unreachable: {exc}"
        # Non-200 so `ttl_page_cache` skips it -- a 200 here stuck a stale
        # "unreachable" banner in the cache for the full TTL past the outage.
        status_code = 503

    return templates.TemplateResponse(
        request,
        "fps_budget.html",
        {
            "result": result,
            "error": error,
            "util_css": util_css,
            "counts": {},
        },
        status_code=status_code,
    )
