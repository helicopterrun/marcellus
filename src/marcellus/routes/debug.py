"""Debug index: utility corner for things that don't belong in the main nav.

Hosts links to the toybox (deliberately tucked away here), the OpenAPI docs,
and a live capabilities dump -- the same payload the iOS client probes.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from marcellus import __version__
from marcellus.routes import scrub as scrub_routes

router = APIRouter(tags=["debug"])


@router.get("/debug", response_class=HTMLResponse)
async def debug_index(request: Request) -> Any:
    templates = request.app.state.templates
    caps = await scrub_routes.capabilities(request)
    return templates.TemplateResponse(
        request,
        "debug.html",
        {
            "version": __version__,
            "capabilities_json": json.dumps(caps, indent=2),
            "cap_sections": [
                (k, json.dumps(v, indent=2, sort_keys=True)) for k, v in sorted(caps.items())
            ],
            "counts": {},
        },
    )
