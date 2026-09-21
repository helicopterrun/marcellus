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
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.continuations import (
    ContinuationConfig,
    Source,
    bucket,
    continuation_config_from_settings,
    predict_window,
    score_candidate,
)
from marcellus.encounters.linker import family_of
from marcellus.encounters.transitions import load_transitions
from marcellus.errors import error_detail
from marcellus.models.wire import (
    ContinuationsResponse,
    ObservationDetailResponse,
    ObservationsResponse,
)
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes.encounters import _adjacency_for, _summary

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


_CONT_BUCKET_RANK = {"confirmed": 0, "likely": 1, "possible": 2}


def _cand_labels(row: dict[str, Any]) -> list[str]:
    import json

    raw = row.get("labels_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _raw_candidates(
    conn: Any,
    *,
    source_camera: str,
    source_end: float,
    source_encounter_id: str,
    families: set[str],
    adjacency: Adjacency,
    cfg: ContinuationConfig,
) -> list[dict[str, Any]]:
    """One dict per (neighbour camera, shared family): either a real
    candidate member found in the predicted window, or a pure prediction
    (`atom_id=None`) when none exists. Runs inside `db.with_sidecar`."""
    transitions = load_transitions(conn)
    neighbour_cameras = adjacency.neighbours(source_camera) | store.cameras_with_learned_transition(
        conn, source_camera
    )
    neighbour_cameras.discard(source_camera)

    out: list[dict[str, Any]] = []
    for cam in sorted(neighbour_cameras):
        for family in sorted(families):
            stats = transitions.get((source_camera, cam, family))
            window = predict_window(source_end, stats, cfg)
            members = store.members_in_window(conn, cam, window[0], window[1], limit=20)
            matched = [
                m for m in members if family in {family_of(lb) for lb in _cand_labels(m)}
            ]
            if not matched:
                out.append(
                    {
                        "camera": cam,
                        "family": family,
                        "atom_id": None,
                        "encounter_id": None,
                        "start_time": None,
                        "cand_label": family,
                        "stats": stats,
                        "window": window,
                        "pinned": False,
                    }
                )
                continue
            for m in matched:
                cand_label = next(
                    (lb for lb in _cand_labels(m) if family_of(lb) == family), family
                )
                pinned = store.has_pin_decision(conn, m["atom_id"], source_encounter_id)
                out.append(
                    {
                        "camera": cam,
                        "family": family,
                        "atom_id": m["atom_id"],
                        "encounter_id": m["encounter_id"],
                        "start_time": m["start_time"],
                        "cand_label": cand_label,
                        "stats": stats,
                        "window": window,
                        "pinned": pinned,
                    }
                )
    return out


@v1_router.get("/observations/{atom_id}/continuations")
async def observation_continuations(
    atom_id: str, request: Request, limit: int = Query(5, ge=1, le=20)
) -> dict[str, Any]:
    """M5, docs/encounters.md "Suggested continuations": camera(s) this
    observation might continue onto next, scored against adjacency + learned
    transition times. Never returns a `confirmed` suggestion from score
    alone -- see `encounters/continuations.py`."""
    settings = _settings(request)
    adjacency = _adjacency_for(request)
    cfg = continuation_config_from_settings(settings)

    def _load(conn: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        row = store.observation(conn, atom_id)
        if row is None:
            return None, []
        families = {family_of(lb) for lb in _cand_labels(row)}
        raw = _raw_candidates(
            conn,
            source_camera=row["camera"],
            source_end=row["end_time"] if row["end_time"] is not None else row["start_time"],
            source_encounter_id=row["encounter_id"],
            families=families,
            adjacency=adjacency,
            cfg=cfg,
        )
        return row, raw

    row, raw_candidates = await db.with_sidecar(settings.sidecar.db_path, _load)
    if row is None:
        raise HTTPException(
            status_code=404, detail=error_detail("not_found", "no such observation")
        )

    source = Source(
        camera=row["camera"],
        end_time=row["end_time"] if row["end_time"] is not None else row["start_time"],
        labels=tuple(_cand_labels(row)),
        last_zone=row.get("last_zone", "") or "",
        encounter_id=row["encounter_id"],
        atom_id=atom_id,
    )

    suggestions: list[dict[str, Any]] = []
    for cand in raw_candidates:
        elapsed = None if cand["start_time"] is None else cand["start_time"] - source.end_time
        score, why = score_candidate(
            source, cand["camera"], cand["cand_label"], elapsed, cand["stats"], adjacency, cfg
        )
        existing_same = (
            cand["atom_id"] is not None and cand["encounter_id"] == source.encounter_id
        )
        bkt = bucket(score, cfg, existing_same_encounter=existing_same, pinned=cand["pinned"])
        if bkt is None:
            continue
        suggestions.append(
            {
                "camera": cand["camera"],
                "window": list(cand["window"]),
                "score": score,
                "bucket": bkt,
                "why": why,
                "observation_id": cand["atom_id"],
                "encounter_id": cand["encounter_id"],
                "start": cand["start_time"],
            }
        )

    suggestions.sort(key=lambda s: (_CONT_BUCKET_RANK.get(s["bucket"], 9), -s["score"]))
    suggestions = suggestions[:limit]

    body = {
        "from": {
            "id": atom_id,
            "camera": source.camera,
            "end": source.end_time,
            "direction": row.get("direction", "") or "",
            "last_zone": source.last_zone,
            "labels": list(source.labels),
            "encounter_id": source.encounter_id,
        },
        "suggestions": suggestions,
    }
    ContinuationsResponse.model_validate(body)
    return body
