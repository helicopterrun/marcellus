"""HTTP endpoints for the read-only analysis modules.

Each endpoint mirrors the corresponding `fsc analysis ...` CLI subcommand
and returns the same structured payload as JSON.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from marcellus import db
from marcellus.analysis import (
    annotation_offset,
    fps_budget,
    motion_active,
    motion_compare,
    motion_rate,
    pull_events,
    score_histogram,
    zone_hits,
)
from marcellus.errors import error_detail
from marcellus.frigate_api import FrigateAPIError
from marcellus.routes._deps import settings_of as _settings

# `database is locked` vocabulary, matching routes/scrub.py's `{"error",
# "message"}` shape (docs/scrub-cache-and-proxy-spec.md §4.0) rather than a
# bare string -- so a client can distinguish "transiently unavailable, retry
# me" from every other 5xx.
_ERR_DB_LOCKED = "db_locked"


def _db_locked(exc: db.DBLockedError) -> HTTPException:
    return HTTPException(
        status_code=503, detail={"error": _ERR_DB_LOCKED, "message": str(exc)}
    )

router = APIRouter(prefix="/analysis", tags=["analysis"])


@router.get("/score-histogram")
def score_histogram_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
    camera: str | None = None,
    label: str | None = None,
    min_samples: int = Query(30, ge=1),
) -> dict[str, Any]:
    s = _settings(request)
    return score_histogram.analyze(
        frigate_db=s.frigate.db_path,
        sidecar_db=s.sidecar.db_path,
        days=days, camera=camera, label=label, min_samples=min_samples,
    )


@router.get("/motion-rate")
def motion_rate_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
) -> list[dict[str, Any]]:
    s = _settings(request)
    return motion_rate.analyze(frigate_db=s.frigate.db_path, days=days)


@router.get("/fps-budget")
def fps_budget_endpoint(request: Request) -> dict[str, Any]:
    s = _settings(request)
    try:
        return fps_budget.analyze(frigate_base_url=s.frigate.base_url)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc


@router.get("/motion-active")
def motion_active_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
) -> dict[str, Any]:
    s = _settings(request)
    try:
        return motion_active.analyze(frigate_base_url=s.frigate.base_url, days=days)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc


@router.get("/motion-compare")
def motion_compare_endpoint(
    request: Request,
    baseline: str,
    target: str,
) -> dict[str, Any]:
    s = _settings(request)
    try:
        return motion_compare.analyze(
            frigate_base_url=s.frigate.base_url, baseline=baseline, target=target,
        )
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc
    except ValueError as exc:
        # Date-parse errors from parse_range bubble up here.
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_range", str(exc))
        ) from exc


@router.get("/zone-hits")
def zone_hits_endpoint(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    camera: str | None = None,
) -> dict[str, Any]:
    s = _settings(request)
    try:
        return zone_hits.analyze(
            frigate_db=s.frigate.db_path,
            sidecar_db=s.sidecar.db_path,
            days=days, camera=camera,
        )
    except db.DBLockedError as exc:
        raise _db_locked(exc) from exc


@router.get("/pull-events")
def pull_events_endpoint(
    request: Request,
    days: int = Query(14, ge=1, le=365),
    camera: str | None = None,
    label: str | None = None,
    limit: int = Query(1000, ge=1, le=10000),
) -> list[dict[str, Any]]:
    """Returns up to `limit` events as a JSON array (capped for HTTP use)."""
    s = _settings(request)
    out: list[dict[str, Any]] = []
    try:
        for ev in pull_events.pull(
            frigate_db=s.frigate.db_path, days=days, camera=camera, label=label,
        ):
            out.append(ev)
            if len(out) >= limit:
                break
    except db.DBLockedError as exc:
        raise _db_locked(exc) from exc
    return out


@router.get("/annotation-offset")
def annotation_offset_endpoint(
    request: Request,
    days: int = Query(7, ge=1, le=90),
    camera: str | None = None,
) -> list[dict[str, Any]]:
    s = _settings(request)
    try:
        return annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days, camera=camera,
        )
    except annotation_offset.AnnotationOffsetUnavailable as exc:
        raise HTTPException(
            status_code=501, detail=error_detail("not_implemented", str(exc))
        ) from exc


# --- Event-clock alignment (Settings page workflow) -------------------------
#
# The measurement sweeps a wider window than the CLI default: the skew this
# exists to find is "a few seconds", and a ±3 s sweep cannot see a 5 s offset.
_MEASURE_WINDOW_MS = 8000


def _alignment_job(request: Request) -> dict[str, Any]:
    state = request.app.state
    if not hasattr(state, "alignment_job"):
        state.alignment_job = {
            "running": False, "results": None, "error": None, "measured_at": None
        }
    return cast(dict[str, Any], state.alignment_job)


@router.post("/annotation-offset/measure")
async def alignment_measure(request: Request, days: int = Query(3, ge=1, le=30)) -> Any:
    """Starts a background measurement pass; poll `state` for the result.

    Template-matching every candidate event thumbnail against recording
    snapshots takes minutes -- far past an HTTP timeout -- so this returns
    immediately and the Settings page polls."""
    import asyncio

    job = _alignment_job(request)
    if job["running"]:
        raise HTTPException(
            status_code=409,
            detail=error_detail("measurement_running", "a measurement is already running"),
        )
    s = _settings(request)
    job.update(running=True, error=None)

    def _run() -> list[dict[str, Any]]:
        return annotation_offset.analyze(
            frigate_db=s.frigate.db_path,
            frigate_base_url=s.frigate.base_url,
            days=days,
            search_window_ms=_MEASURE_WINDOW_MS,
        )

    async def _task() -> None:
        import time as _time

        try:
            job["results"] = await asyncio.to_thread(_run)
            job["measured_at"] = _time.time()
        except Exception as exc:  # noqa: BLE001 -- surfaced to the page, not raised
            job["error"] = str(exc)
        finally:
            job["running"] = False

    request.app.state.alignment_task = asyncio.create_task(_task())
    return {"started": True}


@router.get("/annotation-offset/events")
def alignment_events(
    request: Request,
    camera: str,
    limit: int = Query(12, ge=1, le=25),
) -> list[dict[str, Any]]:
    """Recent finished events for the manual-calibration picker, sorted by
    how far the object moved across the frame.

    A subject crossing the frame gives an unambiguous visual match; a near-
    stationary one makes every candidate frame look the same. `extent` is the
    diagonal of the path's bounding box in normalized frame units, computed
    from `data.path_data`; events are returned most-moving first (newest first
    among ties, e.g. a Frigate storing no paths). Events younger than a minute
    are skipped -- the recording segment covering them may not be committed
    yet, so the filmstrip would be all 404s.
    """
    import json as json_mod
    import math
    import sqlite3
    import time

    from marcellus import db

    s = _settings(request)
    conn = db.open_frigate_ro(s.frigate.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
        data_col = ", data" if "data" in cols else ""
        # The movement sort surfaces the best walkers from the whole window --
        # which on a quiet camera can be events whose recordings retention has
        # already pruned, leaving a filmstrip of nothing but 404s. Retention is
        # motion-based and SPARSE (old segments survive in patches), so a
        # global oldest-recording bound is not enough: each event's own window
        # must still be covered. The ±15 s margin absorbs the detect-vs-record
        # clock skew this whole tool exists to measure.
        recordings_ok = True
        try:
            conn.execute("SELECT 1 FROM recordings LIMIT 1")
        except sqlite3.Error:
            recordings_ok = False
        coverage_clause = (
            """
               AND EXISTS (
                   SELECT 1 FROM recordings r
                    WHERE r.camera = event.camera
                      AND r.start_time < event.end_time + 15
                      AND r.end_time > event.start_time - 15
               )
            """
            if recordings_ok
            else ""
        )
        rows = conn.execute(
            f"""
            SELECT id, label, sub_label, start_time, end_time, top_score{data_col}
              FROM event
             WHERE camera = ? AND has_snapshot = 1
               AND end_time IS NOT NULL AND start_time < ?
               {coverage_clause}
             ORDER BY start_time DESC
             LIMIT 50
            """,
            (camera, time.time() - 60.0),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        extent = 0.0
        anchor = r["start_time"]
        if data_col:
            try:
                parsed = json_mod.loads(r["data"]) if r["data"] else {}
            except (json_mod.JSONDecodeError, TypeError):
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            pts = db.parse_path_data(parsed.get("path_data"))
            if len(pts) >= 2:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            # The snapshot is the BEST-SCORING frame, not the start frame.
            # Anchoring the visual match at start_time would fold the
            # start-to-peak delay into the measured offset -- a delay that
            # varies per event (and correlates with travel direction, which is
            # how it was noticed). The path point nearest the snapshot box's
            # bottom-centre is the snapshot's own detect-clock moment.
            box = parsed.get("box")
            if pts and isinstance(box, list) and len(box) == 4:
                try:
                    bx = float(box[0]) + float(box[2]) / 2
                    by = float(box[1]) + float(box[3])
                    nearest = min(
                        pts, key=lambda p: (p[0] - bx) ** 2 + (p[1] - by) ** 2
                    )
                    anchor = nearest[2]
                except (TypeError, ValueError):
                    pass
        out.append(
            {
                "id": r["id"],
                "label": r["label"],
                "sub_label": r["sub_label"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "top_score": r["top_score"],
                "extent": round(extent, 3),
                # The detect-clock moment the snapshot shows; the calibrator
                # anchors its filmstrip and preview here, not at start_time.
                "anchor_time": anchor,
            }
        )
    out.sort(key=lambda e: (-e["extent"], -e["start_time"]))
    return out[:limit]


@router.get("/annotation-offset/thumbnail/{event_id}")
def alignment_thumbnail(request: Request, event_id: str) -> Any:
    """An event's thumbnail for the calibration picker.

    Served here rather than through the reverse proxy: Frigate's nginx 401s
    proxied `/api/events/...` image requests when Frigate auth is enabled, so
    the browser cannot fetch them directly -- the sidecar's own connection can.
    Thumbnails are immutable once the event ends, so they cache hard."""
    from fastapi.responses import Response

    from marcellus.frigate_api import FrigateAPIError, FrigateClient

    s = _settings(request)
    try:
        with FrigateClient(s.frigate.base_url) as fc:
            jpeg, _status = fc.event_thumbnail(event_id)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc
    if jpeg is None:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "no thumbnail for event")
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _commit_frigate_config(config_path: str, camera: str, offset_ms: int) -> bool:
    """Best-effort git commit of the config write, when the config lives in a
    repo (it does on the reference deployment). Failure is logged, never
    raised -- the offset is already in effect; the commit is audit trail."""
    import logging
    import subprocess
    from pathlib import Path

    p = Path(config_path)
    repo = p.parent
    if not (repo / ".git").exists():
        return False
    try:
        subprocess.run(
            ["git", "-C", str(repo), "add", p.name], check=True,
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m",
             f"{camera}: annotation_offset {offset_ms} (sidecar calibrator)"],
            check=True, capture_output=True, timeout=10,
        )
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logging.getLogger(__name__).warning("config git commit failed: %s", exc)
        return False


def _restart_pending(request: Request) -> list[str]:
    """Cameras whose config offset was written since Frigate's last restart.

    In-memory on purpose: worst case after a sidecar restart the button
    disappears, and Frigate's own UI still shows its restart banner."""
    state = request.app.state
    if not hasattr(state, "frigate_restart_pending"):
        state.frigate_restart_pending = []
    return cast(list[str], state.frigate_restart_pending)


@router.post("/annotation-offset/apply-config")
async def alignment_apply_config(request: Request) -> Any:
    """Write a calibrated offset into Frigate's own config.

    Body: `{"camera": str, "offset_ms": int}`. This is the escalation path for
    cameras whose `detect.annotation_offset` is config-pinned (config wins over
    sidecar overrides by design): the value goes where it is authoritative and
    the sidecar override is cleared so the two sources cannot disagree.

    Deliberately does NOT restart Frigate -- annotation_offset only takes
    effect in its pipeline on boot, but calibrating several cameras should
    cost one restart, not one each. The explicit restart is
    `POST /analysis/annotation-offset/restart-frigate`; `restart_pending`
    in the state response drives the button."""
    from marcellus.frigate_api import FrigateAPIError, FrigateClient
    from marcellus.routes import scrub as scrub_routes

    body = await request.json()
    camera = body.get("camera")
    if not isinstance(camera, str) or not camera:
        raise HTTPException(
            status_code=422, detail=error_detail("invalid_camera", "camera required")
        )
    try:
        offset_ms = int(body.get("offset_ms"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=error_detail("invalid_offset", "bad offset_ms")
        ) from exc
    if abs(offset_ms) > 60_000:
        raise HTTPException(
            status_code=422,
            detail=error_detail("offset_out_of_range", "offset_ms out of range"),
        )

    s = _settings(request)

    def _apply_config() -> bool:
        with FrigateClient(s.frigate.base_url) as fc:
            fc.set_annotation_offset(camera, offset_ms)
            return _commit_frigate_config(str(s.frigate.config_path), camera, offset_ms)

    # `FrigateClient` and `git commit` (inside `_commit_frigate_config`) are
    # both sync -- run off-thread so a slow Frigate config write doesn't stall
    # every other request on this single-worker event loop.
    try:
        committed = await asyncio.to_thread(_apply_config)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc

    # The config is authoritative now; a lingering sidecar override for the
    # same camera would just be shadowed data waiting to confuse someone.
    def _clear_sidecar_override() -> None:
        conn = db.open_sidecar(s.sidecar.db_path)
        try:
            db.set_event_clock_offset(conn, camera, 0)
        finally:
            conn.close()

    await asyncio.to_thread(_clear_sidecar_override)
    scrub_routes.invalidate_event_clock_offsets()

    pending = _restart_pending(request)
    if camera not in pending:
        pending.append(camera)
    return {"camera": camera, "config_ms": offset_ms, "committed": committed,
            "restart_pending": list(pending)}


@router.post("/annotation-offset/restart-frigate")
async def alignment_restart_frigate(request: Request) -> Any:
    """The explicit restart: apply every config offset saved since the last
    one. Restarting is ~30 s of blind cameras, which is why it is a button
    the user presses once, not a side effect of every save."""
    from marcellus.frigate_api import FrigateAPIError, FrigateClient

    s = _settings(request)

    def _restart() -> None:
        with FrigateClient(s.frigate.base_url) as fc:
            fc.restart()

    # Sync `FrigateClient.restart()` blocks for Frigate's own ~30s restart
    # window -- run off-thread so it doesn't stall every other request on
    # this single-worker event loop for the duration.
    try:
        await asyncio.to_thread(_restart)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc
    applied = list(_restart_pending(request))
    _restart_pending(request).clear()
    return {"restarted": True, "applied": applied}


@router.get("/annotation-offset/snapshot/{event_id}")
def alignment_snapshot(request: Request, event_id: str) -> Any:
    """The event's full-frame snapshot (bbox drawn) for the calibrator's
    reference pane. Same sidecar-authorized posture and caching as the
    thumbnail endpoint; the full frame is what makes the side-by-side (and the
    blink compare) meaningful -- same scene geometry as the recording frames."""
    from fastapi.responses import Response

    from marcellus.frigate_api import FrigateAPIError, FrigateClient

    s = _settings(request)
    try:
        with FrigateClient(s.frigate.base_url) as fc:
            jpeg, _status = fc.event_snapshot_jpeg(event_id)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc
    if jpeg is None:
        raise HTTPException(
        status_code=404, detail=error_detail("not_found", "no snapshot for event")
    )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/annotation-offset/frame/{camera}")
def alignment_frame(request: Request, camera: str, ts: float = Query(...)) -> Any:
    """A recording frame at wall-clock `ts`, for the calibration filmstrip.

    Recordings are immutable, so successful frames cache hard -- nudging back
    and forth over the same offsets stays in the browser cache."""
    import math
    import time

    from fastapi.responses import Response

    from marcellus.frigate_api import FrigateAPIError, FrigateClient

    now = time.time()
    if not math.isfinite(ts) or ts <= 0 or ts > now or now - ts > 30 * 86400:
        raise HTTPException(
        status_code=422, detail=error_detail("invalid_range", "ts out of range")
    )
    s = _settings(request)
    try:
        with FrigateClient(s.frigate.base_url) as fc:
            jpeg, status = fc.recording_snapshot(camera, ts)
    except FrigateAPIError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", str(exc))
        ) from exc
    if jpeg is None:
        raise HTTPException(
        status_code=404, detail=error_detail("not_found", "no recording covers ts")
    )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/annotation-offset/state")
def alignment_state(request: Request) -> Any:
    """The measurement job plus what's currently in effect per camera."""
    from marcellus import db
    from marcellus.faces.crosscam import annotation_offset_ms
    from marcellus.frigate_api import FrigateClient

    s = _settings(request)
    job = _alignment_job(request)
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        applied = db.event_clock_offsets(conn)
    finally:
        conn.close()
    cameras = sorted(
        {r["camera"] for r in (job["results"] or [])} | set(applied)
    )
    # Every camera in Frigate's *live* config, so the manual calibrator can
    # reach ones with no measurement and no applied offset. Event history is
    # only a fallback — it keeps retired camera names alive forever after a
    # rename, offering rows that can never be calibrated (no recordings).
    all_cameras: list[str] = []
    running_cfg: dict[str, Any] | None = None
    try:
        with FrigateClient(s.frigate.base_url) as fc:
            running_cfg = fc.config()
        all_cameras = sorted(running_cfg.get("cameras", {}))
    except FrigateAPIError:
        all_cameras = []
    if not all_cameras:
        try:
            fconn = db.open_frigate_ro(s.frigate.db_path)
            try:
                all_cameras = sorted(
                    r["camera"] for r in fconn.execute("SELECT DISTINCT camera FROM event")
                )
            finally:
                fconn.close()
        except db.FrigateDBMissingError:
            all_cameras = cameras
    config_ms = {
        cam: annotation_offset_ms(str(s.frigate.config_path), cam)
        for cam in sorted(set(cameras) | set(all_cameras))
    }
    # Restart-pending is *derived*, not remembered: a camera whose saved
    # config offset differs from what the running Frigate process is using
    # needs a restart. The in-memory list (kept for the unreachable-Frigate
    # fallback) used to be the only source, and a sidecar restart silently
    # wiped it — the button vanished with saves still unapplied.
    restart_pending: list[str]
    if running_cfg is not None:
        restart_pending = []
        for cam in all_cameras:
            running_ms = (
                running_cfg.get("cameras", {})
                .get(cam, {})
                .get("detect", {})
                .get("annotation_offset", 0)
            )
            if int(running_ms or 0) != int(config_ms.get(cam) or 0):
                restart_pending.append(cam)
    else:
        restart_pending = list(_restart_pending(request))
    return {
        "running": job["running"],
        "error": job["error"],
        "measured_at": job["measured_at"],
        "results": job["results"],
        "applied_ms": applied,
        "config_ms": config_ms,
        "cameras": all_cameras,
        "restart_pending": restart_pending,
    }


@router.post("/annotation-offset/apply")
async def alignment_apply(request: Request) -> Any:
    """Writes sidecar-side offsets: body `{"offsets": {camera: ms, ...}}`.

    An offset of 0 clears the override. Frigate's own
    `detect.annotation_offset` still wins over these when set."""
    from marcellus.routes import scrub as scrub_routes

    body = await request.json()
    offsets = body.get("offsets")
    if not isinstance(offsets, dict) or not offsets:
        raise HTTPException(
            status_code=422,
            detail=error_detail(
                "invalid_offsets", "offsets must be a non-empty object"
            ),
        )
    clean: dict[str, int] = {}
    for cam, ms in offsets.items():
        try:
            value = int(ms)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail=error_detail("invalid_offset", f"bad offset for {cam}"),
            ) from exc
        if abs(value) > 60_000:
            raise HTTPException(
                status_code=422,
                detail=error_detail("offset_out_of_range", f"offset for {cam} out of range"),
            )
        clean[str(cam)] = value

    s = _settings(request)

    def _write_offsets() -> None:
        conn = db.open_sidecar(s.sidecar.db_path)
        try:
            for cam, ms in clean.items():
                db.set_event_clock_offset(conn, cam, ms)
        finally:
            conn.close()

    await asyncio.to_thread(_write_offsets)
    scrub_routes.invalidate_event_clock_offsets()
    return {"applied": clean}
