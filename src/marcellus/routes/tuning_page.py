"""`/tuning`: landing page for the five measurement pages (Scores, Motion,
FPS budget, Zone hits, Placement). Each is also a tab on the others via
`templates/_tuning_tabs.html`. Static content; the auth middleware gates it
like every other page."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["tuning-page"])

#: (name, href, the question the page answers)
_PAGES: tuple[tuple[str, str, str], ...] = (
    (
        "Scores",
        "/score-histogram",
        "Where do false alarms stop and real detections start, so what should each "
        "camera and object's minimum score and threshold be?",
    ),
    (
        "Motion",
        "/motion",
        "Which cameras are busy with noise (wind, shadows) rather than real activity, "
        "and did a motion-mask change help?",
    ),
    (
        "FPS budget",
        "/fps-budget",
        "Does the detector have enough inference capacity for what the cameras ask of it?",
    ),
    (
        "Zone hits",
        "/zone-hits",
        "How often is each zone entered, and which regions look like candidates for a mask?",
    ),
    (
        "Placement",
        "/placement",
        "How many pixels will a person, face or plate get from a camera at a given lens, "
        "distance and mounting height?",
    ),
)


@router.get("/tuning", response_class=HTMLResponse)
def tuning_view(request: Request) -> Any:
    pages = [{"name": n, "href": h, "desc": d} for n, h, d in _PAGES]
    return request.app.state.templates.TemplateResponse(
        request, "tuning.html", {"pages": pages, "counts": {}}
    )
