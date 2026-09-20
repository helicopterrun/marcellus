"""Browser scrub viewer: timeline + sprite scrubbing over the /v1 API.

A thin page shell -- all data comes from the same `/v1/reel` and
`/v1/scrub/{camera}/sheets` endpoints the iOS client uses, so the page doubles
as a live exerciser of the app contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from marcellus.routes import scrub as scrub_routes

router = APIRouter(tags=["scrub-ui"])


@router.get("/scrub", response_class=HTMLResponse)
async def scrub_view(request: Request, camera: str | None = None) -> Any:
    templates = request.app.state.templates
    caps = await scrub_routes.capabilities(request)
    cameras = caps["scrub_cache"]["cameras"]
    return templates.TemplateResponse(
        request,
        "scrub.html",
        {
            "cameras": cameras,
            "camera": camera or (cameras[0] if cameras else ""),
            "enabled": caps["scrub_cache"]["enabled"],
            "counts": {},
        },
    )
