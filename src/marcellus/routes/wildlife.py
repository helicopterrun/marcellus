"""Wildlife finder review: visits of animals Frigate missed, plus a scoreboard.

Images are addressed by ROW ID, as on /faces/captures: the files are in our
table, so id-addressing removes path traversal outright, and the handler still
checks that the stored relative path resolves under `output_dir`.

"Send to Frigate+" posts ONE frame (a box's `frame_ts`) through Frigate's
POST /api/{camera}/plus/{frame_time}, which uploads it unlabeled for
annotation on plus.frigate.video. It is a deliberate per-frame click, never
automatic: an upload leaves this machine and becomes training data.
"""

from __future__ import annotations

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
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates
from marcellus.wildlife import finder

router = APIRouter(tags=["wildlife"])

_REVIEWS = ("pending", "kept", "discarded")
_HITS_PER_VISIT = 12


def _local(ts: float, tz_name: str) -> str:
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).strftime("%a %b %d %H:%M:%S %Z")
        except Exception:  # noqa: BLE001 - fall through to UTC
            pass
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a %b %d %H:%M:%S UTC")


def _visits(
    settings: Settings, *, days: float, review: str, camera: str, limit: int
) -> list[dict[str, Any]]:
    since = time.time() - days * 86400
    clauses = ["v.start_ts >= ?"]
    args: list[Any] = [since]
    if review != "all":
        clauses.append("v.review = ?")
        args.append(review)
    if camera:
        clauses.append("v.camera = ?")
        args.append(camera)
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        vrows = conn.execute(
            f"SELECT * FROM wildlife_visits v WHERE {' AND '.join(clauses)} "
            "ORDER BY v.start_ts DESC LIMIT ?",
            [*args, limit],
        ).fetchall()
        visits: OrderedDict[int, dict[str, Any]] = OrderedDict()
        for v in vrows:
            visits[int(v["id"])] = {
                "id": int(v["id"]),
                "camera": v["camera"],
                "start_ts": float(v["start_ts"]),
                "end_ts": float(v["end_ts"]),
                "duration_s": round(float(v["end_ts"]) - float(v["start_ts"])),
                "when": _local(float(v["start_ts"]), settings.wildlife.timezone),
                "review": v["review"],
                "hits": [],
                "n_hits": int(v["hits"]),
            }
        if visits:
            q = ",".join("?" * len(visits))
            for b in conn.execute(
                f"SELECT * FROM wildlife_boxes WHERE visit_id IN ({q}) ORDER BY frame_ts",
                list(visits),
            ):
                visit = visits[int(b["visit_id"])]
                visit["hits"].append(
                    {
                        "id": int(b["id"]),
                        "frame_ts": float(b["frame_ts"]),
                        "offset_s": round(float(b["frame_ts"]) - visit["start_ts"], 1),
                        "det_score": b["det_score"],
                        "reject_score": b["reject_score"],
                        "suggestion": b["suggestion"],
                        "suggestion_score": b["suggestion_score"],
                        "top_class": b["top_class"],
                        "top_score": b["top_score"],
                        "has_crop": bool(b["crop_path"]),
                        "has_frame": bool(b["frame_path"]),
                        "plus_status": b["plus_status"],
                        "plus_detail": b["plus_detail"],
                    }
                )
    finally:
        conn.close()

    out = []
    for v in visits.values():
        hits = v["hits"]
        votes: dict[str, float] = {}
        for h in hits:
            if h["suggestion"]:
                votes[h["suggestion"]] = votes.get(h["suggestion"], 0.0) + float(
                    h["suggestion_score"] or 0.0
                )
        v["suggestion"] = max(votes, key=lambda k: votes[k]) if votes else "animal"
        v["sent"] = sum(1 for h in hits if h["plus_status"] == "sent")
        # Show the strongest hits, in time order: detector confidence times
        # "not rejected" confidence.
        if len(hits) > _HITS_PER_VISIT:
            best = sorted(
                hits,
                key=lambda h: -(float(h["det_score"] or 0) * (1 - float(h["reject_score"] or 0))),
            )[:_HITS_PER_VISIT]
            v["hits"] = sorted(best, key=lambda h: h["frame_ts"])
        out.append(v)
    return out


def _resolve(settings: Settings, box_id: int, column: str) -> Path:
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            f"SELECT {column} AS rel FROM wildlife_boxes WHERE id = ?", (box_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["rel"]:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "image not found"))
    root = Path(settings.wildlife.output_dir).resolve()
    try:
        path = (root / str(row["rel"])).resolve()
    except OSError as exc:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "image not found")
        ) from exc
    # A hand-edited row must not become an arbitrary file read.
    if root != path and root not in path.parents:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "image not found"))
    if not path.is_file():
        raise HTTPException(status_code=404, detail=error_detail("not_found", "image file missing"))
    return path


@router.get("/wildlife", response_class=HTMLResponse)
def wildlife_view(
    request: Request,
    days: float = Query(7.0, ge=1, le=90),
    review: str = "pending",
    camera: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    s = _settings(request)
    if review not in (*_REVIEWS, "all"):
        review = "pending"
    return _templates(request).TemplateResponse(
        request,
        "wildlife.html",
        {
            "visits": _visits(s, days=days, review=review, camera=camera, limit=limit),
            "board": finder.scoreboard(s, days=days),
            "days": days,
            "review": review,
            "camera": camera,
            "cameras": list(s.wildlife.cameras),
            "enabled": s.wildlife.enabled,
            "last_run": finder.read_last_run(s),
            "last_run_when": _local(
                float((finder.read_last_run(s) or {}).get("ran_at") or 0), s.wildlife.timezone
            ),
        },
    )


@router.get("/wildlife.json")
def wildlife_json(
    request: Request,
    days: float = Query(7.0, ge=1, le=90),
    review: str = "pending",
    camera: str = "",
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    s = _settings(request)
    if review not in (*_REVIEWS, "all"):
        review = "pending"
    return JSONResponse(
        {
            "visits": _visits(s, days=days, review=review, camera=camera, limit=limit),
            "scoreboard": finder.scoreboard(s, days=days),
            "last_run": finder.read_last_run(s),
        }
    )


@router.get("/wildlife/boxes/{box_id}/crop.jpg")
def box_crop(box_id: int, request: Request) -> FileResponse:
    return FileResponse(_resolve(_settings(request), box_id, "crop_path"), media_type="image/jpeg")


@router.get("/wildlife/boxes/{box_id}/frame.jpg")
def box_frame(box_id: int, request: Request) -> FileResponse:
    return FileResponse(_resolve(_settings(request), box_id, "frame_path"), media_type="image/jpeg")


class VisitReviewPayload(BaseModel):
    review: str  # 'kept' | 'discarded' | 'pending'


@router.post("/wildlife/visits/{visit_id}/review")
def visit_review(visit_id: int, payload: VisitReviewPayload, request: Request) -> JSONResponse:
    if payload.review not in _REVIEWS:
        raise HTTPException(
            status_code=400,
            detail=error_detail("invalid_review", f"review must be one of {_REVIEWS}"),
        )
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        cur = conn.execute(
            "UPDATE wildlife_visits SET review = ?, reviewed_at = ? WHERE id = ?",
            (payload.review, datetime.now(timezone.utc).isoformat(timespec="seconds"), visit_id),
        )
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(
                status_code=404, detail=error_detail("not_found", "visit not found")
            )
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@router.post("/wildlife/boxes/{box_id}/plus")
def box_send_plus(box_id: int, request: Request) -> JSONResponse:
    """Upload this hit's frame to Frigate+ and mark its visit kept."""
    from marcellus.frigate_api import FrigateClient, FrigatePlusError

    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT camera, frame_ts, visit_id, plus_status FROM wildlife_boxes WHERE id = ?",
            (box_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=error_detail("not_found", "hit not found"))
        if row["plus_status"] == "sent":
            raise HTTPException(
                status_code=409,
                detail=error_detail("already_sent", "this frame was already sent to Frigate+"),
            )
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with FrigateClient(s.frigate.base_url) as client:
                msg = client.submit_plus_frame(
                    str(row["camera"]), float(row["frame_ts"]), timeout=s.wildlife.http_timeout_s
                )
        except FrigatePlusError as exc:
            conn.execute(
                "UPDATE wildlife_boxes SET plus_status = 'error', plus_detail = ?, plus_at = ? "
                "WHERE id = ?",
                (str(exc)[:300], stamp, box_id),
            )
            conn.commit()
            raise HTTPException(
                status_code=502, detail=error_detail("plus_failed", str(exc))
            ) from exc
        conn.execute(
            "UPDATE wildlife_boxes SET plus_status = 'sent', plus_detail = ?, plus_at = ? "
            "WHERE id = ?",
            (msg[:300], stamp, box_id),
        )
        if row["visit_id"] is not None:
            conn.execute(
                "UPDATE wildlife_visits SET review = 'kept', reviewed_at = ? WHERE id = ?",
                (stamp, row["visit_id"]),
            )
        conn.commit()
        return JSONResponse({"ok": True, "message": msg})
    finally:
        conn.close()
