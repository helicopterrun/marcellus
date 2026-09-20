"""Placement planner: focal/zoom -> HFOV -> object pixels at distance.

A pure-physics, fully client-side calculator. The route just renders the page
with the optics presets injected as JSON; all interactivity lives in
`static/js/placement.js`. No Frigate API or DB access (intentionally — event-
data calibration of the object target-px defaults is a planned follow-up).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from marcellus.analysis import optics

router = APIRouter(tags=["placement"])


@router.get("/placement", response_class=HTMLResponse)
def placement_view(request: Request) -> Any:
    templates = request.app.state.templates
    payload = optics.presets_payload()
    return templates.TemplateResponse(
        request,
        "placement.html",
        {"presets_json": json.dumps(payload), "counts": {}},
    )
