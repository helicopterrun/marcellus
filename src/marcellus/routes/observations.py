"""`/v1/observations` (docs/encounters.md "Observations"): a read-only view
over `encounter_members` -- one row per atom instead of grouped by
encounter, for callers that want the atom-level unit directly (a single
camera-span with a direction hint) rather than an encounter's rollup.
Auth is the same app-wide `FrigateAuthMiddleware` every other `/v1` route
under this router uses -- no per-route decorator.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from marcellus import db
from marcellus.encounters import store
from marcellus.errors import error_detail
from marcellus.models.wire import ObservationDetailResponse, ObservationsResponse
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes.encounters import _summary

v1_router = APIRouter(prefix="/v1", tags=["v1"])

#: Default lookback window and hard cap, matching routes/encounters.py.
_DEFAULT_WINDOW_S = 24 * 3600.0
_MAX_LIMIT = 500


def _observation(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "id": row["atom_id"],
        "encounter_id": row["encounter_id"],
        "camera": row["camera"],
        "start": row["start_time"],
        "end": row["end_time"],
        "labels": json.loads(row["labels_json"]),
        "zones": json.loads(row["zones_json"]),
        "first_zone": row.get("first_zone", ""),
        "last_zone": row.get("last_zone", ""),
        "direction": row.get("direction", ""),
        "heading_deg": row.get("heading_deg"),
        "dir_source": row.get("dir_source", ""),
        "severity": row["severity"],
        "event_ids": json.loads(row["event_ids_json"]),
        "sub_labels": json.loads(row["sub_labels_json"]),
        "link_reason": row["link_reason"],
        "confidence": row["confidence"],
    }


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


@v1_router.get("/observations", response_model=ObservationsResponse)
async def observations_list(
    request: Request,
    start: float | None = None,
    end: float | None = None,
    cameras: str | None = None,
    labels: str | None = None,
    limit: int = Query(500, ge=1, le=_MAX_LIMIT),
) -> dict[str, Any]:
    settings = _settings(request)
    now = time.time()
    window_end = end if end is not None else now
    window_start = start if start is not None else window_end - _DEFAULT_WINDOW_S
    camera_list = _split_csv(cameras)
    label_list = _split_csv(labels)

    def _load(conn: Any) -> list[dict[str, Any]]:
        return store.list_observations(
            conn,
            start=window_start,
            end=window_end,
            cameras=camera_list,
            labels=label_list,
            limit=limit,
        )

    rows = await db.with_sidecar(settings.sidecar.db_path, _load)
    return {"t": now, "observations": [_observation(r) for r in rows]}


@v1_router.get("/observations/{atom_id}", response_model=ObservationDetailResponse)
async def observation_detail(atom_id: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)

    def _load(
        conn: Any,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        tuple[dict[str, Any] | None, dict[str, Any] | None],
    ]:
        row = store.observation(conn, atom_id)
        if row is None:
            return None, None, (None, None)
        encounter_row = store.get(conn, row["encounter_id"])
        neighbours = store.observation_neighbours(conn, atom_id)
        return row, encounter_row, neighbours

    row, encounter_row, (prev_row, next_row) = await db.with_sidecar(
        settings.sidecar.db_path, _load
    )
    if row is None or encounter_row is None:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "no such observation")
        )
    return {
        "observation": _observation(row),
        "encounter": _summary(encounter_row),
        "neighbours": {
            "prev": _observation(prev_row) if prev_row is not None else None,
            "next": _observation(next_row) if next_row is not None else None,
        },
    }
