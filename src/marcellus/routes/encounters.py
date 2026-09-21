"""Encounters (docs/encounters.md): HTML browsing pages, the human-decision
POST routes (split/pin/merge/undo -- `encounters/store.py`'s
`split_atom`/`pin_atom`/`merge_encounters`/`undo_decisions`), and the `/v1`
JSON read + write surface. Auth is the same `FrigateAuthMiddleware` that
gates every non-exempt route (see `auth.EXEMPT_PATHS`) -- no per-route
decorator, no CSRF token, matching every other admin POST route in this repo
(e.g. `routes/tuning.py`, `routes/push_settings.py`)."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import parse_qsl, quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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
        "first_zone": row.get("first_zone", ""),
        "last_zone": row.get("last_zone", ""),
        "direction": row.get("direction", ""),
        "heading_deg": row.get("heading_deg"),
        "dir_source": row.get("dir_source", ""),
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


async def _urlencoded_form(request: Request) -> dict[str, str]:
    """Parse an `application/x-www-form-urlencoded` POST body without
    pulling in `python-multipart` (Starlette's `Request.form()` requires it
    even for the urlencoded case) -- the only forms these routes accept."""
    body = await request.body()
    return {k: v for k, v in parse_qsl(body.decode("utf-8"))}


def _flash_redirect(path: str, msg: str) -> RedirectResponse:
    """PRG redirect carrying a one-shot flash message in the querystring --
    this repo has no session/flash-cookie machinery, so the message just
    rides the URL like the rest of the HTML pages' filter state (`camera`/
    `since`)."""
    return RedirectResponse(url=f"{path}?msg={quote(msg)}", status_code=303)


@router.get("/encounters", response_class=HTMLResponse)
async def encounters_page(
    request: Request, camera: str | None = None, since: float | None = None, msg: str | None = None
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
            "msg": msg,
        },
    )


@router.get("/encounters/{encounter_id}", response_class=HTMLResponse)
async def encounter_detail_page(encounter_id: str, request: Request, msg: str | None = None) -> Any:
    settings = _settings(request)

    def _load(
        conn: Any,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        row = store.get(conn, encounter_id)
        if row is None:
            return None, [], {}
        member_rows = store.members(conn, encounter_id)
        decisions = {m["atom_id"]: store.decisions_for(conn, m["atom_id"]) for m in member_rows}
        return row, member_rows, decisions

    row, member_rows, decisions = await db.with_sidecar(settings.sidecar.db_path, _load)
    if row is None:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "no such encounter"))
    return _templates(request).TemplateResponse(
        request,
        "encounter_detail.html",
        {
            "encounter": _summary(row),
            "members": [_member(m) for m in member_rows],
            "decisions": decisions,
            "active_page": "encounters",
            "msg": msg,
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


# --------------------------------------------------------------------------
# Human decisions: split / pin / merge / undo (docs/encounters.md
# "Correcting encounters"). HTML routes do the browser form + PRG redirect;
# the `/v1` twins below do the same store calls and return the resulting
# encounter as JSON, same auth as the rest of `/v1/encounters`.
# --------------------------------------------------------------------------


async def _load_encounter_json(settings: Any, encounter_id: str) -> dict[str, Any]:
    def _load(conn: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        row = store.get(conn, encounter_id)
        if row is None:
            return None, []
        return row, store.members(conn, encounter_id)

    row, member_rows = await db.with_sidecar(settings.sidecar.db_path, _load)
    if row is None:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "no such encounter"))
    return {"encounter": _summary(row), "members": [_member(m) for m in member_rows]}


@router.post("/encounters/{encounter_id}/atoms/{atom_id}/split")
async def split_atom_page(encounter_id: str, atom_id: str, request: Request) -> Any:
    settings = _settings(request)

    def _do(conn: Any) -> str:
        return store.split_atom(conn, atom_id, time.time())

    try:
        new_encounter_id = await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=error_detail("not_found", str(exc))) from exc
    return _flash_redirect(
        f"/encounters/{new_encounter_id}", f"Split {atom_id} into a new encounter"
    )


@router.post("/encounters/{encounter_id}/atoms/{atom_id}/pin")
async def pin_atom_page(encounter_id: str, atom_id: str, request: Request) -> Any:
    settings = _settings(request)
    form = await _urlencoded_form(request)
    target = form.get("target") or ""
    if not target:
        raise HTTPException(status_code=400, detail=error_detail("invalid", "target is required"))

    def _do(conn: Any) -> str:
        return store.pin_atom(conn, atom_id, target, time.time())

    try:
        target_encounter_id = await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=error_detail("invalid", str(exc))) from exc
    return _flash_redirect(f"/encounters/{target_encounter_id}", f"Pinned {atom_id} here")


@router.post("/encounters/{encounter_id}/merge")
async def merge_encounters_page(encounter_id: str, request: Request) -> Any:
    settings = _settings(request)
    form = await _urlencoded_form(request)
    source = form.get("source") or ""
    if not source:
        raise HTTPException(status_code=400, detail=error_detail("invalid", "source is required"))

    def _do(conn: Any) -> int:
        return store.merge_encounters(conn, source, encounter_id, time.time())

    try:
        count = await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=error_detail("invalid", str(exc))) from exc
    return _flash_redirect(f"/encounters/{encounter_id}", f"Merged {count} atom(s) from {source}")


@router.post("/encounters/{encounter_id}/atoms/{atom_id}/undo")
async def undo_decisions_page(encounter_id: str, atom_id: str, request: Request) -> Any:
    settings = _settings(request)

    def _do(conn: Any) -> None:
        store.undo_decisions(conn, atom_id, time.time())

    await db.with_sidecar(settings.sidecar.db_path, _do)
    return _flash_redirect(f"/encounters/{encounter_id}", f"Cleared decisions for {atom_id}")


@v1_router.post("/encounters/{encounter_id}/atoms/{atom_id}/split")
async def v1_split_atom(encounter_id: str, atom_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)

    def _do(conn: Any) -> str:
        return store.split_atom(conn, atom_id, time.time())

    try:
        new_encounter_id = await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=error_detail("not_found", str(exc))) from exc
    return await _load_encounter_json(settings, new_encounter_id)


@v1_router.post("/encounters/{encounter_id}/atoms/{atom_id}/pin")
async def v1_pin_atom(encounter_id: str, atom_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    body = await request.json()
    target = body.get("target") if isinstance(body, dict) else None
    if not target:
        raise HTTPException(status_code=400, detail=error_detail("invalid", "target is required"))

    def _do(conn: Any) -> str:
        return store.pin_atom(conn, atom_id, target, time.time())

    try:
        target_encounter_id = await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=error_detail("invalid", str(exc))) from exc
    return await _load_encounter_json(settings, target_encounter_id)


@v1_router.post("/encounters/{encounter_id}/merge")
async def v1_merge_encounters(encounter_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    body = await request.json()
    source = body.get("source") if isinstance(body, dict) else None
    if not source:
        raise HTTPException(status_code=400, detail=error_detail("invalid", "source is required"))

    def _do(conn: Any) -> int:
        return store.merge_encounters(conn, source, encounter_id, time.time())

    try:
        await db.with_sidecar(settings.sidecar.db_path, _do)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=error_detail("invalid", str(exc))) from exc
    return await _load_encounter_json(settings, encounter_id)


@v1_router.post("/encounters/{encounter_id}/atoms/{atom_id}/undo")
async def v1_undo_decisions(encounter_id: str, atom_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)

    def _do(conn: Any) -> None:
        store.undo_decisions(conn, atom_id, time.time())

    await db.with_sidecar(settings.sidecar.db_path, _do)
    return await _load_encounter_json(settings, encounter_id)
