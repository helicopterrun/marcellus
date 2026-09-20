"""Triage list, detail, label/clear endpoints."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from marcellus.config import Settings
from marcellus.db import open_joined
from marcellus.errors import error_detail
from marcellus.frigate_api import FrigateClient, FrigatePlusError
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates
from marcellus.triage.recorder import (
    AlreadyLabeledError,
    EventNotFoundError,
)
from marcellus.triage.recorder import (
    clear as recorder_clear,
)
from marcellus.triage.recorder import (
    record as recorder_record,
)
from marcellus.zones import (
    is_full_frame,
    load_camera_zones,
    zones_containing_box,
)

router = APIRouter(tags=["triage"])

_LABELS = ("tp", "fp", "skip")


# ----- HTML pages -----


def _query_events(
    *,
    frigate_db: str | Path,
    sidecar_db: str | Path,
    camera: str | None,
    label: str | None,
    triage: str,
    days: int,
    limit: int,
    order: str,
    q: str = "",
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if camera:
        where.append("e.camera = ?")
        params.append(camera)
    if label:
        where.append("e.label = ?")
        params.append(label)
    if days:
        cutoff = time.time() - days * 86400
        where.append("e.start_time >= ?")
        params.append(cutoff)
    if triage == "untriaged":
        where.append("t.event_id IS NULL")
    elif triage == "triaged":
        where.append("t.event_id IS NOT NULL")
    elif triage in _LABELS:
        where.append("t.label = ?")
        params.append(triage)

    # Free-text search from the header's search box: same substring match
    # (label/camera/sub_label) as /v1/events/search's `q`, applied token by
    # token so a multi-word query narrows rather than requiring an exact hit.
    for token in q.split():
        like = f"%{token}%"
        where.append("(e.camera LIKE ? OR e.label LIKE ? OR e.sub_label LIKE ?)")
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "DESC" if order == "newest" else "ASC"
    limit = max(1, min(int(limit), 500))

    sql = f"""
        SELECT
            e.id, e.camera, e.label, e.start_time, e.end_time,
            e.zones, e.has_clip, e.has_snapshot, e.sub_label, e.data,
            e.top_score AS col_top_score, e.score AS col_score,
            e.plus_id, e.false_positive,
            t.label AS triage_label, t.note AS triage_note,
            t.labeled_at AS triage_at, t.session AS triage_session
        FROM event e
        LEFT JOIN sidecar.triage_labels t ON e.id = t.event_id
        {where_sql}
        ORDER BY e.start_time {order_sql}
        LIMIT ?
    """
    params.append(limit)

    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = json.loads(r["data"]) if r["data"] else {}
        out.append(
            {
                "id": r["id"],
                "camera": r["camera"],
                "label": r["label"],
                "sub_label": r["sub_label"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "zones": json.loads(r["zones"]) if r["zones"] else [],
                "has_clip": bool(r["has_clip"]),
                "has_snapshot": bool(r["has_snapshot"]),
                "top_score": d.get("top_score") or r["col_top_score"],
                "score": d.get("score") or r["col_score"],
                "box": d.get("box"),
                "region": d.get("region"),
                "max_severity": d.get("max_severity"),
                "plus_id": r["plus_id"],
                "plus_false_positive": bool(r["false_positive"]),
                "triage_label": r["triage_label"],
                "triage_note": r["triage_note"],
                "triage_at": r["triage_at"],
                "triage_session": r["triage_session"],
            }
        )
    return out


def _get_event_full(
    *, frigate_db: str | Path, sidecar_db: str | Path, event_id: str
) -> dict[str, Any] | None:
    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        r = conn.execute(
            """
            SELECT e.*, t.label AS triage_label, t.note AS triage_note,
                   t.labeled_at AS triage_at, t.session AS triage_session
              FROM event e
              LEFT JOIN sidecar.triage_labels t ON e.id = t.event_id
             WHERE e.id = ?
            """,
            (event_id,),
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = json.loads(r["data"]) if r["data"] else {}
    return {
        "id": r["id"],
        "camera": r["camera"],
        "label": r["label"],
        "sub_label": r["sub_label"],
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "zones": json.loads(r["zones"]) if r["zones"] else [],
        "has_clip": bool(r["has_clip"]),
        "has_snapshot": bool(r["has_snapshot"]),
        "top_score": d.get("top_score") or r["top_score"],
        "score": d.get("score") or r["score"],
        "box": d.get("box"),
        "region": d.get("region"),
        "max_severity": d.get("max_severity"),
        "plus_id": r["plus_id"],
        "plus_false_positive": bool(r["false_positive"]),
        "triage_label": r["triage_label"],
        "triage_note": r["triage_note"],
        "triage_at": r["triage_at"],
        "triage_session": r["triage_session"],
    }


def _get_filter_options(frigate_db: str | Path, sidecar_db: str | Path) -> dict[str, Any]:
    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        cams = [
            r["camera"]
            for r in conn.execute("SELECT DISTINCT camera FROM event ORDER BY camera")
        ]
        labels = [
            r["label"]
            for r in conn.execute("SELECT DISTINCT label FROM event ORDER BY label")
        ]
        count_rows = conn.execute(
            "SELECT label AS l, COUNT(*) AS c FROM sidecar.triage_labels GROUP BY label"
        ).fetchall()
    finally:
        conn.close()
    return {
        "cameras": cams,
        "labels": labels,
        "counts": {r["l"]: r["c"] for r in count_rows},
    }








def _zone_overlay(
    settings: Settings, camera: str, box: list[float] | None, cumulative: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (polygons, legend_items) for the given camera.

    A zone is "active" if the snapshot bbox's bottom-center point falls inside
    it (Frigate's own zone-eval semantic). Falls back to the cumulative
    event.zones list when the event has no box.
    """
    zones = load_camera_zones(settings.frigate.config_path).get(camera, [])
    if not zones:
        return [], []
    active_set = zones_containing_box(zones, box) or set(cumulative or [])
    polygons: list[dict[str, Any]] = []
    legend: list[dict[str, Any]] = []
    for z in zones:
        is_active = z["name"] in active_set
        full_frame = is_full_frame(z["coords"])
        polygons.append(
            {
                "name": z["name"],
                "color": z["color"],
                "active": is_active,
                "full_frame": full_frame,
                "points": " ".join(f"{x:.4f},{y:.4f}" for x, y in z["coords"]),
            }
        )
        legend.append({"name": z["name"], "color": z["color"], "active": is_active})
    return polygons, legend


@router.get("/triage", response_class=HTMLResponse)
def list_view(
    request: Request,
    camera: str | None = None,
    label: str | None = None,
    triage: str = "any",
    days: int = Query(default=14, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=500),
    order: str = "newest",
    q: str = "",
) -> Any:
    s = _settings(request)
    events = _query_events(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        camera=camera, label=label, triage=triage,
        days=days, limit=limit, order=order, q=q,
    )
    opts = _get_filter_options(s.frigate.db_path, s.sidecar.db_path)
    filters = {
        "camera": camera or "", "label": label or "", "triage": triage,
        "days": days, "limit": limit, "order": order, "q": q,
    }
    return _templates(request).TemplateResponse(
        request,
        "list.html",
        {
            "events": events,
            "cameras": opts["cameras"],
            "labels": opts["labels"],
            "counts": opts["counts"],
            "filters": filters,
            "frigate_base": s.frigate.base_url,
        },
    )


@router.get("/event/{event_id}", response_class=HTMLResponse)
def detail_view(
    event_id: str,
    request: Request,
    camera: str | None = None,
    label: str | None = None,
    triage: str = "any",
    days: int = Query(default=14, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=500),
    order: str = "newest",
) -> Any:
    s = _settings(request)
    ev = _get_event_full(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        event_id=event_id,
    )
    if not ev:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "event not found"))

    ordered = _query_events(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        camera=camera, label=label, triage=triage,
        days=days, limit=limit, order=order,
    )
    ids = [e["id"] for e in ordered]
    prev_id = next_id = None
    position: tuple[int, int] | None = None
    if event_id in ids:
        i = ids.index(event_id)
        prev_id = ids[i - 1] if i > 0 else None
        next_id = ids[i + 1] if i < len(ids) - 1 else None
        position = (i + 1, len(ids))

    opts = _get_filter_options(s.frigate.db_path, s.sidecar.db_path)
    polygons, zone_legend = _zone_overlay(s, ev["camera"], ev["box"], ev["zones"])
    filters_qs = {
        "camera": camera or "", "label": label or "", "triage": triage,
        "days": days, "limit": limit, "order": order,
    }
    return _templates(request).TemplateResponse(
        request,
        "detail.html",
        {
            "event": ev,
            "prev_id": prev_id,
            "next_id": next_id,
            "position": position,
            "counts": opts["counts"],
            "frigate_base": s.frigate.base_url,
            "plus_enabled": bool(getattr(request.app.state, "plus_enabled", False)),
            "zone_polygons": polygons,
            "zone_legend": zone_legend,
            "filters_qs": filters_qs,
        },
    )


# ----- JSON endpoints used by the UI -----


class LabelPayload(BaseModel):
    event_id: str
    label: str
    note: str | None = None
    session: str | None = None
    submit_plus: bool = False


class ClearPayload(BaseModel):
    event_id: str


def _maybe_submit_plus(
    *, request: Request, event_id: str, label: str
) -> dict[str, Any]:
    """Submit to Frigate+ when the user labels TP/FP and Plus is configured.

    Returns a small status dict for the UI: status in {"sent", "skipped",
    "error"} plus an optional reason/plus_id. Never raises — Plus failures
    must not roll back the triage label write.
    """
    if not getattr(request.app.state, "plus_enabled", False):
        return {"status": "skipped", "reason": "plus_not_enabled"}
    if label not in ("tp", "fp"):
        return {"status": "skipped", "reason": "label_not_submittable"}

    # Pull the event row to gate on the same preconditions Frigate's own
    # endpoint checks — saves a round-trip when we know it'll 400.
    s = _settings(request)
    ev = _get_event_full(
        frigate_db=s.frigate.db_path, sidecar_db=s.sidecar.db_path, event_id=event_id,
    )
    if not ev:
        return {"status": "error", "reason": "event_not_found"}
    if not ev["has_snapshot"]:
        return {"status": "skipped", "reason": "no_snapshot"}
    if ev["box"] is None:
        return {"status": "skipped", "reason": "no_box"}
    if not ev["end_time"]:
        return {"status": "skipped", "reason": "in_progress"}
    if label == "tp" and ev["plus_id"]:
        return {"status": "skipped", "reason": "already_submitted", "plus_id": ev["plus_id"]}
    if label == "fp" and ev["plus_false_positive"]:
        return {"status": "skipped", "reason": "already_submitted", "plus_id": ev["plus_id"]}

    try:
        with FrigateClient(s.frigate.base_url) as fc:
            if label == "tp":
                plus_id = fc.submit_plus(event_id, include_annotation=True)
            else:
                plus_id = fc.submit_false_positive(event_id)
    except FrigatePlusError as exc:
        return {"status": "error", "reason": str(exc), "status_code": exc.status_code}
    return {"status": "sent", "plus_id": plus_id}


@router.post("/label")
def post_label(payload: LabelPayload, request: Request) -> JSONResponse:
    s = _settings(request)
    if payload.label not in _LABELS:
        raise HTTPException(status_code=400, detail=error_detail("invalid_label", "invalid label"))
    try:
        # `force=True` matches the UI's behavior: re-clicking a label updates.
        result = recorder_record(
            frigate_db=s.frigate.db_path,
            sidecar_db=s.sidecar.db_path,
            event_id=payload.event_id,
            label=payload.label,  # type: ignore[arg-type]
            note=payload.note,
            session=payload.session,
            force=True,
        )
    except EventNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "event not found")
        ) from exc
    except AlreadyLabeledError as exc:  # not reachable with force=True, kept for type-safety
        raise HTTPException(
            status_code=409, detail=error_detail("conflict", str(exc))
        ) from exc

    plus_result: dict[str, Any] = {"status": "not_requested"}
    if payload.submit_plus:
        plus_result = _maybe_submit_plus(
            request=request, event_id=payload.event_id, label=payload.label,
        )
    return JSONResponse({"ok": True, "plus": plus_result, "before": result.get("before")})


@router.post("/clear-label")
def post_clear(payload: ClearPayload, request: Request) -> JSONResponse:
    s = _settings(request)
    recorder_clear(sidecar_db=s.sidecar.db_path, event_id=payload.event_id)
    return JSONResponse({"ok": True})
