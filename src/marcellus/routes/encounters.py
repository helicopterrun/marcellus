"""Encounters (docs/encounters.md): HTML browsing pages plus the `/v1` JSON
read surface. No pin/split UI yet (slice 2) -- `encounter_decisions` exists
so the linker logic is complete, but nothing writes to it from a route.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from marcellus import db, zones
from marcellus.encounters import store
from marcellus.encounters.adjacency import Adjacency, build_adjacency
from marcellus.errors import error_detail
from marcellus.models.wire import EncounterResponse, EncountersResponse
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates

router = APIRouter(tags=["encounters"])
v1_router = APIRouter(prefix="/v1", tags=["v1"])

#: HTML page default window.
_DEFAULT_WINDOW_S = 48 * 3600.0
_MAX_LIMIT = 500


def _adjacency_for(request: Request) -> Adjacency:
    """The live service's adjacency graph when running, else one built fresh
    from config -- the adjacency endpoint and page work even before the
    background service has started (or when `encounters.enabled` is false)."""
    service = getattr(request.app.state, "encounters", None)
    if service is not None:
        adjacency: Adjacency = service.adjacency
        return adjacency
    settings = _settings(request)
    zones_by_camera = zones.load_camera_zones(settings.frigate.config_path)
    return build_adjacency(
        zones_by_camera,
        extra=settings.encounters.adjacency,
        removed=settings.encounters.not_adjacent,
    )


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "id": row["id"],
        "start": row["start_time"],
        "end": row["end_time"],
        "sealed": row["sealed_at"] is not None,
        "cameras": json.loads(row["cameras_json"]),
        "labels": json.loads(row["labels_json"]),
        "identities": json.loads(row["identities_json"]),
        "primary_event_id": row["primary_event_id"],
        "peak_severity": row["peak_severity"],
        "atom_count": row["atom_count"],
    }


def _member(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "atom_id": row["atom_id"],
        "camera": row["camera"],
        "start": row["start_time"],
        "end": row["end_time"],
        "severity": row["severity"],
        "labels": json.loads(row["labels_json"]),
        "zones": json.loads(row["zones_json"]),
        "event_ids": json.loads(row["event_ids_json"]),
        "sub_labels": json.loads(row["sub_labels_json"]),
        "link_reason": row["link_reason"],
        "confidence": row["confidence"],
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


@router.get("/encounters", response_class=HTMLResponse)
async def encounters_page(
    request: Request, camera: str | None = None, since: float | None = None
) -> Any:
    settings = _settings(request)
    window_since = since if since is not None else time.time() - _DEFAULT_WINDOW_S

    def _load(conn: Any) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        rows = store.list_recent(conn, since=window_since, limit=200, camera=camera)
        return [(row, store.members(conn, row["id"])) for row in rows]

    loaded = await db.with_sidecar(settings.sidecar.db_path, _load)
    encounters = [
        dict(_summary(row), members=[_member(m) for m in member_rows])
        for row, member_rows in loaded
    ]
    return _templates(request).TemplateResponse(
        request,
        "encounters.html",
        {
            "encounters": encounters,
            "camera": camera or "",
            "since": window_since,
            "enabled": settings.encounters.enabled,
            "active_page": "encounters",
            "adjacency": _adjacency_for(request).to_json(),
        },
    )


@router.get("/encounters/{encounter_id}", response_class=HTMLResponse)
async def encounter_detail_page(encounter_id: str, request: Request) -> Any:
    settings = _settings(request)

    def _load(conn: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        row = store.get(conn, encounter_id)
        if row is None:
            return None, []
        return row, store.members(conn, encounter_id)

    row, member_rows = await db.with_sidecar(settings.sidecar.db_path, _load)
    if row is None:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "no such encounter"))
    return _templates(request).TemplateResponse(
        request,
        "encounter_detail.html",
        {
            "encounter": _summary(row),
            "members": [_member(m) for m in member_rows],
            "active_page": "encounters",
        },
    )


# --------------------------------------------------------------------------
# /v1 JSON
# --------------------------------------------------------------------------


@v1_router.get("/encounters/adjacency")
async def encounters_adjacency(request: Request) -> dict[str, Any]:
    return _adjacency_for(request).to_json()


@v1_router.get("/encounters", response_model=EncountersResponse)
async def encounters_list(
    request: Request,
    since: float | None = None,
    limit: int = Query(200, ge=1, le=_MAX_LIMIT),
    camera: str | None = None,
) -> dict[str, Any]:
    settings = _settings(request)
    window_since = since if since is not None else time.time() - _DEFAULT_WINDOW_S

    def _load(conn: Any) -> list[dict[str, Any]]:
        return store.list_recent(conn, since=window_since, limit=limit, camera=camera)

    rows = await db.with_sidecar(settings.sidecar.db_path, _load)
    return {"t": time.time(), "encounters": [_summary(r) for r in rows]}


@v1_router.get("/encounters/{encounter_id}", response_model=EncounterResponse)
async def encounter_detail(encounter_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)

    def _load(conn: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        row = store.get(conn, encounter_id)
        if row is None:
            return None, []
        return row, store.members(conn, encounter_id)

    row, member_rows = await db.with_sidecar(settings.sidecar.db_path, _load)
    if row is None:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "no such encounter"))
    return {
        "encounter": _summary(row),
        "members": [_member(m) for m in member_rows],
    }
