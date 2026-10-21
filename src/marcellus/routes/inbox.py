"""Review inbox (/inbox), its verdict/undo endpoints, and the Frigate+ coverage page.

The ranking lives in `triage.inbox`, the uploads in `triage.outbox`; this
module is just the HTTP surface. A verdict never blocks on Frigate+: it
records the label, queues the upload and answers at once with when the
upload will go (or why it won't).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from marcellus.db import open_frigate_ro, open_sidecar
from marcellus.errors import error_detail
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates
from marcellus.triage import inbox, outbox, plusmeta

router = APIRouter(tags=["inbox"])


def _plus_enabled(request: Request) -> bool:
    return bool(getattr(request.app.state, "plus_enabled", False))


def _outbox_summary(request: Request) -> dict[str, Any]:
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        return outbox.summary(conn)
    finally:
        conn.close()


@router.get("/inbox", response_class=HTMLResponse)
def inbox_view(request: Request, more: int = 0) -> Any:
    s = _settings(request)
    data = inbox.build(s, more=bool(more))
    return _templates(request).TemplateResponse(
        request,
        "inbox.html",
        {
            **data,
            "more": bool(more),
            "outbox": _outbox_summary(request),
            "plus_enabled": _plus_enabled(request),
            "frigate_base": s.frigate.base_url,
            "plus_delay_s": int(s.triage.plus_delay_s),
            "labels": sorted(plusmeta.get(s).labels),
        },
    )


@router.get("/inbox.json")
def inbox_json(request: Request, more: int = 0) -> JSONResponse:
    data = inbox.build(_settings(request), more=bool(more))
    data["outbox"] = _outbox_summary(request)
    return JSONResponse(data)


class VerdictPayload(BaseModel):
    key: str
    verdict: str
    correct_label: str | None = None


class KeyPayload(BaseModel):
    key: str


@router.post("/inbox/verdict")
def post_verdict(payload: VerdictPayload, request: Request) -> JSONResponse:
    try:
        plus = inbox.apply_verdict(
            _settings(request), key=payload.key, verdict=payload.verdict,
            correct_label=payload.correct_label, plus_enabled=_plus_enabled(request),
        )
    except inbox.VerdictError as exc:
        raise HTTPException(
            status_code=exc.status, detail=error_detail(exc.code, str(exc))
        ) from exc
    return JSONResponse({"ok": True, "plus": plus, "outbox": _outbox_summary(request)})


@router.post("/inbox/undo")
def post_undo(payload: KeyPayload, request: Request) -> JSONResponse:
    try:
        result = inbox.undo(_settings(request), key=payload.key)
    except inbox.VerdictError as exc:
        raise HTTPException(
            status_code=exc.status, detail=error_detail(exc.code, str(exc))
        ) from exc
    return JSONResponse({**result, "outbox": _outbox_summary(request)})


@router.post("/inbox/outbox/{row_id}/retry")
def post_retry(row_id: int, request: Request) -> JSONResponse:
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        ok = outbox.retry(conn, row_id)
        conn.commit()
    finally:
        conn.close()
    if not ok:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "no failed upload with that id")
        )
    return JSONResponse({"ok": True})


# ----- coverage -----


def _iso_ts(value: Any) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


def _coverage(request: Request) -> dict[str, Any]:
    s = _settings(request)
    meta = plusmeta.get(s)
    images = plusmeta.camera_images(s, meta)
    train_ts = meta.train_ts()
    target = s.triage.camera_target_images

    conn = open_sidecar(s.sidecar.db_path)
    try:
        sent_rows = conn.execute(
            "SELECT camera, kind, event_id, item_ts, sent_at FROM plus_outbox "
            "WHERE status = 'sent'"
        ).fetchall()
        # Frames sent from /wildlife's own button never touch the outbox.
        direct_frames = conn.execute(
            "SELECT b.camera, b.frame_ts, b.plus_at FROM wildlife_boxes b "
            "WHERE b.plus_status = 'sent' AND NOT EXISTS "
            "(SELECT 1 FROM plus_outbox o WHERE o.box_id = b.id AND o.status = 'sent')"
        ).fetchall()
        recent = outbox.recent(conn)
        summary = outbox.summary(conn)
    finally:
        conn.close()

    since_build: dict[str, int] = {}
    light: dict[str, dict[str, int]] = {}
    ours: set[str] = set()
    for r in sent_rows:
        cam = str(r["camera"])
        if r["event_id"]:
            ours.add(str(r["event_id"]))
        if train_ts is None or float(r["sent_at"] or 0) >= train_ts:
            since_build[cam] = since_build.get(cam, 0) + 1
        if r["item_ts"] is not None:
            b = inbox.light_bucket(s, float(r["item_ts"]))
            light.setdefault(cam, {"day": 0, "twilight": 0, "night": 0})[b] += 1

    for r in direct_frames:
        cam = str(r["camera"])
        sent_at = _iso_ts(r["plus_at"])
        if train_ts is None or (sent_at is not None and sent_at >= train_ts):
            since_build[cam] = since_build.get(cam, 0) + 1
        b = inbox.light_bucket(s, float(r["frame_ts"]))
        light.setdefault(cam, {"day": 0, "twilight": 0, "night": 0})[b] += 1

    # Uploads made outside the outbox (Frigate's own UI) count too: Frigate's
    # event.plus_id records those. It doesn't store when, so the event's start
    # stands in for the upload time.
    if train_ts is not None:
        try:
            ro = open_frigate_ro(s.frigate.db_path)
            try:
                for r in ro.execute(
                    "SELECT id, camera FROM event WHERE plus_id IS NOT NULL AND start_time >= ?",
                    (train_ts,),
                ):
                    if str(r["id"]) not in ours:
                        cam = str(r["camera"])
                        since_build[cam] = since_build.get(cam, 0) + 1
            finally:
                ro.close()
        except FileNotFoundError:
            pass

    cameras = []
    for cam in sorted(meta.cameras, key=lambda c: (images.get(c, 0), c)):
        have = images.get(cam, 0)
        cameras.append(
            {
                "camera": cam,
                "aliases": s.triage.plus_camera_aliases.get(cam, []),
                "verified": have,
                "pct": min(100, round(100 * have / target)),
                "since_build": since_build.get(cam, 0),
                "light": light.get(cam, {"day": 0, "twilight": 0, "night": 0}),
            }
        )
    return {
        "meta": meta,
        "target": target,
        "cameras": cameras,
        "retired": sorted(plusmeta.retired_cameras(s, meta).items(), key=lambda kv: -kv[1]),
        "outbox_rows": recent,
        "outbox": summary,
        "pending_verify": sum(c["since_build"] for c in cameras),
        "now": time.time(),
    }


@router.get("/inbox/coverage", response_class=HTMLResponse)
def coverage_view(request: Request) -> Any:
    return _templates(request).TemplateResponse(
        request,
        "inbox_coverage.html",
        {**_coverage(request), "plus_enabled": _plus_enabled(request)},
    )
