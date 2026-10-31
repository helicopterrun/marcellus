"""High-res cross-camera face captures: visit-grouped review grid + images.

Images are addressed by ROW ID, not filename. routes/faces.py takes a filename
because those files are Frigate's and are in no table of ours; these files are
in our table, so id-addressing removes the path-traversal class outright instead
of guarding it. The handler still asserts the stored relative path resolves
under `output_dir`, because a hand-edited row must not become an arbitrary file
read.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from marcellus.config import Settings
from marcellus.db import open_sidecar
from marcellus.errors import error_detail
from marcellus.faces import crosscam
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates

router = APIRouter(tags=["face-captures"])

_REVIEWS = ("pending", "kept", "discarded")


def _rows(settings: Settings, *, days: float, review: str, limit: int) -> list[dict[str, Any]]:
    """Captured frames only. Filtering in SQL matters: the face gate settles
    most sampled frames as no_face/skipped, and a LIMIT applied before that
    filter left the page showing 5 of 43 pending visits (2026-10-31)."""
    since = time.time() - days * 86400
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        if review == "all":
            sql = (
                "SELECT * FROM face_captures WHERE trigger_start_ts >= ? AND status = 'captured' "
                "ORDER BY trigger_start_ts DESC, offset_ms LIMIT ?"
            )
            args: tuple[Any, ...] = (since, limit)
        else:
            sql = (
                "SELECT * FROM face_captures WHERE trigger_start_ts >= ? AND review = ? "
                "   AND status = 'captured' "
                "ORDER BY trigger_start_ts DESC, offset_ms LIMIT ?"
            )
            args = (since, review, limit)
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _box(raw: Any) -> list[float] | None:
    """face_box column (JSON [x1, y1, x2, y2], normalized) or None."""
    try:
        box = json.loads(raw) if raw else None
    except ValueError:
        return None
    return [float(v) for v in box] if isinstance(box, list) and len(box) == 4 else None


def _group_visits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse rows into visit blocks, newest first, captures only."""
    visits: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for r in rows:
        if r.get("status") != "captured":
            continue
        key = str(r.get("visit_key") or r.get("trigger_event_id"))
        v = visits.get(key)
        if v is None:
            ts = float(r.get("trigger_start_ts") or 0.0)
            v = {
                "visit_key": key,
                "trigger_camera": r.get("trigger_camera"),
                "trigger_label": r.get("trigger_label"),
                "trigger_score": r.get("trigger_score"),
                "start_ts": ts,
                "when": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
                "captures": [],
            }
            visits[key] = v
        v["captures"].append(
            {
                "id": r.get("id"),
                "offset_ms": r.get("offset_ms"),
                "width": r.get("width"),
                "height": r.get("height"),
                "bytes": r.get("bytes"),
                "cropped": bool(r.get("crop_event_id")),
                "face_score": r.get("face_score"),
                "face_box": _box(r.get("face_box")),
                "has_thumb": bool(r.get("thumb_path")),
                "review": r.get("review"),
            }
        )
    for v in visits.values():
        v["captures"].sort(key=lambda c: c["offset_ms"] or 0)
        # The card's hero: the most confident face (ungated rows have none).
        v["best"] = max(v["captures"], key=lambda c: c["face_score"] or 0.0)
    return list(visits.values())


def _attach_identities(
    settings: Settings, visits: list[dict[str, Any]], *, window_s: float
) -> None:
    """Give each visit the identity clusters face_enrich put the same person in.

    Captures and Identities see the same visitor through different events:
    the capture is keyed by the TRIGGER camera's event (doorbell), while
    face_enrich clusters the CAPTURE camera's own events (gate-face). On
    2026-10-31, 35 of 44 captured visits had a clustered gate-face sighting
    within 30s of the trigger. More than one cluster in the window usually
    means two people, or one person split -- both worth seeing.
    """
    cams = list(settings.face_enrich.cameras)
    if not visits or not cams:
        for v in visits:
            v["identities"] = []
        return
    cam_q = ",".join("?" * len(cams))
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        for v in visits:
            t = v["start_ts"]
            v["identities"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT c.cluster_id, c.name, c.observation_count, COUNT(*) AS hits "
                    "  FROM face_enrichments e JOIN face_clusters c ON c.cluster_id = e.cluster_id "
                    f" WHERE e.camera IN ({cam_q}) AND e.excluded_at IS NULL "
                    "   AND e.event_start_ts BETWEEN ? AND ? "
                    " GROUP BY c.cluster_id "
                    " ORDER BY (c.name IS NULL), hits DESC, c.observation_count DESC",
                    [*cams, t - window_s, t + window_s],
                )
            ]
    finally:
        conn.close()


def _review_counts(settings: Settings, *, days: float) -> dict[str, int]:
    """Visits per review state, for the filter tabs. A visit with frames in
    more than one state counts under each."""
    since = time.time() - days * 86400
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        counts = {
            str(r["review"]): int(r["n"])
            for r in conn.execute(
                "SELECT review, COUNT(DISTINCT visit_key) AS n FROM face_captures "
                " WHERE status = 'captured' AND trigger_start_ts >= ? GROUP BY review",
                (since,),
            )
        }
        counts["all"] = int(
            conn.execute(
                "SELECT COUNT(DISTINCT visit_key) FROM face_captures "
                " WHERE status = 'captured' AND trigger_start_ts >= ?",
                (since,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return counts


def _resolve(settings: Settings, capture_id: int, column: str) -> Path:
    """Row id -> on-disk path, with containment re-validated."""
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            f"SELECT {column} AS rel FROM face_captures WHERE id = ?", (capture_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["rel"]:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "capture not found"))

    root = Path(settings.face_capture.output_dir).resolve()
    try:
        path = (root / str(row["rel"])).resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "capture not found")
        ) from exc
    # A hand-edited row must not become an arbitrary file read.
    if root != path and root not in path.parents:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "capture not found")
        )
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "capture file missing")
        )
    return path


@router.get("/faces/captures", response_class=HTMLResponse)
def captures_view(
    request: Request,
    days: float = Query(7.0, ge=1, le=365),
    review: str = "pending",
    limit: int = Query(200, ge=1, le=500),
) -> Any:
    s = _settings(request)
    if review not in (*_REVIEWS, "all"):
        review = "pending"
    visits = _group_visits(_rows(s, days=days, review=review, limit=limit))
    _attach_identities(s, visits, window_s=s.face_capture.event_window_s)
    return _templates(request).TemplateResponse(
        request,
        "face_captures.html",
        {
            "visits": visits,
            "counts": _review_counts(s, days=days),
            "days": days,
            "review": review,
            "enabled": s.face_capture.enabled,
            "capture_camera": s.face_capture.capture_camera,
            "trigger_cameras": s.face_capture.trigger_cameras,
            "last_run": crosscam.read_last_run(s),
            "window_s": s.face_capture.event_window_s,
        },
    )


@router.get("/faces/captures.json")
def captures_json(
    request: Request,
    days: float = Query(7.0, ge=1, le=365),
    review: str = "pending",
    limit: int = Query(200, ge=1, le=500),
) -> JSONResponse:
    s = _settings(request)
    if review not in (*_REVIEWS, "all"):
        review = "pending"
    visits = _group_visits(_rows(s, days=days, review=review, limit=limit))
    _attach_identities(s, visits, window_s=s.face_capture.event_window_s)
    return JSONResponse({"visits": visits, "last_run": crosscam.read_last_run(s)})


@router.get("/faces/captures/{capture_id}/full.jpg")
def capture_full(capture_id: int, request: Request) -> FileResponse:
    return FileResponse(
        _resolve(_settings(request), capture_id, "full_path"), media_type="image/jpeg"
    )


@router.get("/faces/captures/{capture_id}/thumb.jpg")
def capture_thumb(capture_id: int, request: Request) -> FileResponse:
    return FileResponse(
        _resolve(_settings(request), capture_id, "thumb_path"), media_type="image/jpeg"
    )


class CaptureReviewPayload(BaseModel):
    review: str  # 'kept' | 'discarded' | 'pending'
    visit: bool = False  # apply to the whole visit rather than one capture


@router.post("/faces/captures/{capture_id}/review")
def capture_review(
    capture_id: int, payload: CaptureReviewPayload, request: Request
) -> JSONResponse:
    if payload.review not in _REVIEWS:
        raise HTTPException(
            status_code=400,
            detail=error_detail("invalid_review", f"review must be one of {_REVIEWS}"),
        )
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT visit_key FROM face_captures WHERE id = ?", (capture_id,)
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404, detail=error_detail("not_found", "capture not found")
            )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if payload.visit:
            cur = conn.execute(
                "UPDATE face_captures SET review = ?, reviewed_at = ? WHERE visit_key = ?",
                (payload.review, stamp, row["visit_key"]),
            )
        else:
            cur = conn.execute(
                "UPDATE face_captures SET review = ?, reviewed_at = ? WHERE id = ?",
                (payload.review, stamp, capture_id),
            )
        conn.commit()
        return JSONResponse({"ok": True, "updated": cur.rowcount or 0})
    finally:
        conn.close()


@router.post("/faces/captures/scan")
def captures_scan(request: Request) -> JSONResponse:
    s = _settings(request)
    if not s.face_capture.enabled:
        raise HTTPException(
            status_code=503,
            detail=error_detail("feature_disabled", "face_capture.enabled is false"),
        )
    return JSONResponse({"ok": True, "summary": crosscam.scan(s)})
