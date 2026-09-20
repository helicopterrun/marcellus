"""User guide pages: TOC, topic pages, and the live-stats endpoint.

Topic content is loaded once at startup (guide.load_guide in server.py's app
factory) into app.state.guide; these routes only look things up. Live numbers
are served separately by /guide/stats.json so topic HTML stays a pure render
of the markdown — static/js/guide.js fills the `.guide-stat` placeholders.
Every stat is computed inside its own try/except: a missing table or absent
Frigate DB shows "?" in the guide instead of failing the page.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from marcellus.db import open_frigate_ro, open_sidecar
from marcellus.errors import error_detail
from marcellus.guide import SECTION_TITLES, GuideRegistry
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["guide"])


def _registry(request: Request) -> GuideRegistry:
    return cast(GuideRegistry, request.app.state.guide)


def _sidecar_count(request: Request, sql: str, args: tuple[object, ...] = ()) -> str:
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        row = conn.execute(sql, args).fetchone()
        return str(row[0] if row else 0)
    finally:
        conn.close()


def _stat_clusters_total(request: Request) -> str:
    return _sidecar_count(request, "SELECT COUNT(*) FROM face_clusters")


def _stat_clusters_named(request: Request) -> str:
    return _sidecar_count(request, "SELECT COUNT(*) FROM face_clusters WHERE name IS NOT NULL")


def _stat_faces_captured_24h(request: Request) -> str:
    return _sidecar_count(
        request,
        "SELECT COUNT(*) FROM face_captures WHERE status = 'saved' AND frame_ts >= ?",
        (time.time() - 86400,),
    )


def _stat_scrub_sheets_24h(request: Request) -> str:
    return _sidecar_count(
        request,
        "SELECT COUNT(*) FROM scrub_sheets WHERE start_ts >= ?",
        (time.time() - 86400,),
    )


def _stat_push_devices(request: Request) -> str:
    return _sidecar_count(request, "SELECT COUNT(*) FROM push_devices")


def _stat_events_today(request: Request) -> str:
    conn = open_frigate_ro(_settings(request).frigate.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM event WHERE start_time >= ?",
            (time.time() - 86400,),
        ).fetchone()
        return str(row[0] if row else 0)
    finally:
        conn.close()


def _stat_uptime(request: Request) -> str:
    started = cast(float, request.app.state.started_at)
    seconds = int(time.time() - started)
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{max(seconds // 60, 0)}m"


_STAT_FNS: dict[str, Callable[[Request], str]] = {
    "clusters_total": _stat_clusters_total,
    "clusters_named": _stat_clusters_named,
    "faces_captured_24h": _stat_faces_captured_24h,
    "scrub_sheets_24h": _stat_scrub_sheets_24h,
    "push_devices": _stat_push_devices,
    "events_today": _stat_events_today,
    "uptime": _stat_uptime,
}

#: The full vocabulary `{{stat:...}}` placeholders may use — test_guide.py
#: rejects a topic referencing anything else.
STAT_KEYS = frozenset(_STAT_FNS)


@router.get("/guide", response_class=HTMLResponse)
def guide_index(request: Request) -> object:
    registry = _registry(request)
    return _templates(request).TemplateResponse(
        request,
        "guide_index.html",
        {
            "sections": registry.by_section(),
            "numbers": registry.numbers(),
            "current_slug": None,
            "topic_count": len(registry.topics),
        },
    )


@router.get("/guide/search.json")
def guide_search(request: Request) -> JSONResponse:
    """Full-text index for the client-side search box: one entry per topic."""
    registry = _registry(request)
    numbers = registry.numbers()
    return JSONResponse(
        {
            "topics": [
                {
                    "slug": t.slug,
                    "title": t.meta.title,
                    "number": numbers.get(t.slug, ""),
                    "section": SECTION_TITLES.get(t.meta.section, t.meta.section),
                    "text": t.search_text,
                }
                for t in registry.ordered()
            ]
        }
    )


@router.get("/guide/stats.json")
def guide_stats(request: Request) -> JSONResponse:
    stats: dict[str, str] = {}
    for key, fn in _STAT_FNS.items():
        try:
            stats[key] = fn(request)
        except Exception:
            logger.debug("guide stat %s unavailable", key, exc_info=True)
            stats[key] = "?"
    return JSONResponse({"stats": stats})


@router.get("/guide/{slug}", response_class=HTMLResponse)
def guide_topic(request: Request, slug: str) -> object:
    registry = _registry(request)
    topic = registry.topics.get(slug)
    if topic is None:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "unknown guide topic")
        )
    prev_t, next_t = registry.neighbors(slug)
    numbers = registry.numbers()
    return _templates(request).TemplateResponse(
        request,
        "guide_topic.html",
        {
            "topic": topic,
            "prev_topic": prev_t,
            "next_topic": next_t,
            "sections": registry.by_section(),
            "numbers": numbers,
            "current_slug": slug,
            "number": numbers.get(slug, ""),
            "section_title": SECTION_TITLES.get(topic.meta.section, topic.meta.section),
        },
    )
