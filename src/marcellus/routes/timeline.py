"""`GET /v1/timeline` (M4, docs/encounters.md "Global timeline"): one call
that composes several cameras' `/v1/reel` lanes for a shared window, plus
the encounter-linked observations that fall inside it.

Each lane is exactly what `/v1/reel/{camera}` would return for the same
window (same keys, byte-for-byte via `scrub.compose_reel`), with `camera`
and `observations` added. `encounters` is the distinct set of encounter
summaries referenced by those observations, ordered by start.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from marcellus import db
from marcellus.encounters import store
from marcellus.errors import error_detail
from marcellus.models.wire import TimelineResponse
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes.encounters import summaries
from marcellus.routes.scrub import _etagged, compose_reel, known_cameras_async

router = APIRouter(prefix="/v1", tags=["v1"])

_ERR_CAMERA_UNKNOWN = "camera_unknown"
_ERR_BAD_RANGE = "bad_range"
_ERR_NOT_FOUND = "not_found"

#: More than this many lanes in one request is refused outright -- a global
#: timeline is meant for "the whole property at once", not a substitute for
#: fetching every camera the fleet has.
_MAX_CAMERAS = 12

#: Fan-out limit for the per-camera `compose_reel` calls.
_LANE_CONCURRENCY = 4

#: Internal cap on how many observation rows one timeline response will
#: carry -- `store.list_observations` is asked for one more than this so the
#: response can tell the client it was cut off.
_OBSERVATIONS_CAP = 2000


def _bad_range(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=error_detail(_ERR_BAD_RANGE, message))


def _timeline_observation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["atom_id"],
        "start": row["start_time"],
        "end": row["end_time"],
        "encounter_id": row["encounter_id"],
        "labels": json.loads(row["labels_json"]),
        "direction": row.get("direction", ""),
        "severity": row["severity"],
    }


@router.get("/timeline", response_model=TimelineResponse)
async def timeline(
    request: Request,
    start: float | None = None,
    end: float | None = None,
    cameras: str | None = None,
    motion_scale: float = 10.0,
    encounter: str | None = None,
    pad_s: float = 60.0,
) -> Any:
    settings = _settings(request)

    explicit_cameras = [c.strip() for c in cameras.split(",") if c.strip()] if cameras else None

    if encounter is not None:
        encounter_row = await db.with_sidecar(
            settings.sidecar.db_path, lambda conn: store.get(conn, encounter)
        )
        if encounter_row is None:
            raise HTTPException(
                status_code=404, detail=error_detail(_ERR_NOT_FOUND, "no such encounter")
            )
        now = time.time()
        window_start = encounter_row["start_time"] - pad_s
        window_end = (encounter_row["end_time"] or now) + pad_s
        camera_list = explicit_cameras or json.loads(encounter_row["cameras_json"])
    else:
        if start is None or end is None:
            raise HTTPException(
                status_code=422,
                detail=error_detail("missing_params", "start and end are required"),
            )
        window_start, window_end = start, end
        camera_list = explicit_cameras or []

    if not camera_list:
        raise HTTPException(
            status_code=422, detail=error_detail("missing_params", "cameras is required")
        )
    if not (window_end > window_start):
        raise _bad_range("end must be > start")
    if len(camera_list) > _MAX_CAMERAS:
        raise _bad_range(f"at most {_MAX_CAMERAS} cameras per request, got {len(camera_list)}")
    max_window = settings.encounters.timeline_max_window_s
    if window_end - window_start > max_window:
        raise _bad_range(
            f"window of {window_end - window_start:.0f}s exceeds the "
            f"{max_window:.0f}s timeline_max_window_s cap"
        )

    known = await known_cameras_async(request.app.state, settings)
    unknown = [c for c in camera_list if c not in known]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=error_detail(_ERR_CAMERA_UNKNOWN, f"no such camera: {unknown[0]}"),
        )

    # Each lane's `compose_reel` opens its own Frigate connection on a worker
    # thread (no shared sqlite connection, nothing on the event loop), so the
    # lanes genuinely overlap up to `_LANE_CONCURRENCY`.
    semaphore = asyncio.Semaphore(_LANE_CONCURRENCY)

    async def _lane(camera: str) -> dict[str, Any]:
        async with semaphore:
            body = await compose_reel(
                request, settings, camera, window_start, window_end, motion_scale,
            )
        body["camera"] = camera
        body["observations"] = []
        return body

    lanes = await asyncio.gather(*(_lane(camera) for camera in camera_list))

    lanes_by_camera = {lane["camera"]: lane for lane in lanes}

    def _load_observations(sc_conn: Any) -> list[dict[str, Any]]:
        return store.list_observations(
            sc_conn,
            start=window_start,
            end=window_end,
            cameras=camera_list,
            limit=_OBSERVATIONS_CAP + 1,
        )

    obs_rows = await db.with_sidecar(settings.sidecar.db_path, _load_observations)
    truncated = len(obs_rows) > _OBSERVATIONS_CAP
    obs_rows = obs_rows[:_OBSERVATIONS_CAP]
    # `list_observations` returns newest-first; the timeline reads left to
    # right like everything else in this response.
    obs_rows = list(reversed(obs_rows))

    encounter_ids_seen: list[str] = []
    encounter_rows_by_id: dict[str, dict[str, Any]] = {}
    for row in obs_rows:
        lane = lanes_by_camera.get(row["camera"])
        if lane is None:
            continue
        lane["observations"].append(_timeline_observation(row))
        eid = row["encounter_id"]
        if eid not in encounter_rows_by_id:
            encounter_rows_by_id[eid] = row
            encounter_ids_seen.append(eid)

    def _load_encounters(sc_conn: Any) -> list[dict[str, Any]]:
        rows = [r for eid in encounter_ids_seen if (r := store.get(sc_conn, eid)) is not None]
        return summaries(sc_conn, rows)

    encounter_summaries = await db.with_sidecar(settings.sidecar.db_path, _load_encounters)
    encounters_out = sorted(encounter_summaries, key=lambda e: e["start"])

    body: dict[str, Any] = {
        "t": time.time(),
        "window": [window_start, window_end],
        "lanes": [lanes_by_camera[c] for c in camera_list],
        "encounters": encounters_out,
        "truncated": truncated,
    }
    TimelineResponse.model_validate(body)
    return _etagged(request, body)
