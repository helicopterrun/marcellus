"""Structured event search (`GET /v1/events/search`).

Reads Frigate's `event` table directly (read-only, no sidecar join needed)
and supports the filter set a search UI needs: free-text `q` across
label/sub_label/zones, comma-separated camera/label/zone allowlists, a
sub_label substring match, a start_time window, a minimum score, and a
has_snapshot flag. `zones` is stored as a JSON array on the row (Frigate's own
storage shape), so zone matching goes through `json_each()` rather than a
plain LIKE/IN.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Query, Request

from marcellus.config import Settings
from marcellus.db import open_frigate_ro, open_sidecar
from marcellus.models.wire import RelatedResponse, SearchResultItem
from marcellus.push import card_store

router = APIRouter(prefix="/v1", tags=["search"])

_MAX_LIMIT = 200
_ERR_EVENT_NOT_FOUND = "event_not_found"
# Pad the requested event's span by this many seconds on each side before
# testing another camera's same-label event for time overlap (§ related).
_RELATED_OVERLAP_PAD_S = 20.0


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _query_search(
    *,
    db_path: Any,
    q: str,
    cameras: list[str],
    labels: list[str],
    zones: list[str],
    sub_label: str,
    after: float | None,
    before: float | None,
    min_score: float | None,
    has_snapshot: bool | None,
    limit: int,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if cameras:
        placeholders = ",".join("?" for _ in cameras)
        where.append(f"e.camera IN ({placeholders})")
        params.extend(cameras)

    if labels:
        placeholders = ",".join("?" for _ in labels)
        where.append(f"e.label IN ({placeholders})")
        params.extend(labels)

    if zones:
        placeholders = ",".join("?" for _ in zones)
        where.append(
            "EXISTS (SELECT 1 FROM json_each(e.zones) WHERE json_each.value IN "
            f"({placeholders}))"
        )
        params.extend(zones)

    if sub_label:
        where.append("e.sub_label LIKE ?")
        params.append(f"%{sub_label}%")

    if after is not None:
        where.append("e.start_time >= ?")
        params.append(after)

    if before is not None:
        where.append("e.start_time <= ?")
        params.append(before)

    if min_score is not None:
        where.append("JSON_EXTRACT(e.data, '$.score') >= ?")
        params.append(min_score)

    if has_snapshot is not None:
        where.append("e.has_snapshot = ?")
        params.append(1 if has_snapshot else 0)

    tokens = q.split()
    for token in tokens:
        like = f"%{token}%"
        where.append(
            "(e.label LIKE ? OR e.sub_label LIKE ? OR EXISTS "
            "(SELECT 1 FROM json_each(e.zones) WHERE json_each.value LIKE ?))"
        )
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), _MAX_LIMIT))

    sql = f"""
        SELECT e.id, e.camera, e.label, e.sub_label, e.zones,
               e.start_time, e.end_time, e.has_clip, e.has_snapshot, e.data
        FROM event e
        {where_sql}
        ORDER BY e.start_time DESC
        LIMIT ?
    """
    params.append(limit)

    conn = open_frigate_ro(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "camera": row["camera"],
                "label": row["label"],
                "sub_label": row["sub_label"],
                "zones": json.loads(row["zones"]) if row["zones"] else [],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "has_clip": bool(row["has_clip"]),
                "has_snapshot": bool(row["has_snapshot"]),
                "data": json.loads(row["data"]) if row["data"] else {},
                "search_distance": None,
                "search_source": "structured",
            }
        )
    return out


@router.get("/events/search", response_model=list[SearchResultItem])
async def events_search(
    request: Request,
    q: str = Query(default=""),
    cameras: str = Query(default=""),
    labels: str = Query(default=""),
    zones: str = Query(default=""),
    sub_label: str = Query(default=""),
    after: float | None = Query(default=None),
    before: float | None = Query(default=None),
    min_score: float | None = Query(default=None),
    has_snapshot: bool | None = Query(default=None),
    limit: int = Query(default=50),
) -> list[dict[str, Any]]:
    settings: Settings = request.app.state.settings
    return await asyncio.to_thread(
        _query_search,
        db_path=settings.frigate.db_path,
        q=q,
        cameras=_split_csv(cameras),
        labels=_split_csv(labels),
        zones=_split_csv(zones),
        sub_label=sub_label,
        after=after,
        before=before,
        min_score=min_score,
        has_snapshot=has_snapshot,
        limit=limit,
    )


def _related_item(row: sqlite3.Row, source: str) -> dict[str, Any]:
    data = json.loads(row["data"]) if row["data"] else {}
    return {
        "camera": row["camera"],
        "event_id": row["id"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "label": row["label"],
        "score": data.get("score"),
        "source": source,
    }


def _linked_track_ids(sidecar_db_path: Any, event_id: str) -> set[str]:
    """Every Frigate event id the push pipeline's cross-camera dedup folded
    into the same card as `event_id` -- the card's own track plus any track
    aliased onto it (`push/card_store.py`; card_key is
    `{camera}:{label}:{track_id}` and Frigate's track id IS the event id, per
    `routes/push.py`'s `card_for_event`, whose resolution order this mirrors).
    """
    try:
        conn = open_sidecar(sidecar_db_path)
    except OSError:
        return set()
    try:
        row = card_store.find_card_row_by_event_suffix(conn, event_id)
        card_key = row["card_key"] if row is not None else None
        if card_key is None:
            card_key = card_store.find_track_alias_card_key(conn, event_id)
        if card_key is None:
            return set()

        ids = set()
        parts = card_key.split(":", 2)
        if len(parts) == 3:
            ids.add(parts[2])
        for alias_row in conn.execute(
            "SELECT track_id FROM push_card_track_aliases WHERE card_key = ?",
            (card_key,),
        ).fetchall():
            ids.add(alias_row["track_id"])
        ids.discard(event_id)
        return ids
    finally:
        conn.close()


def _query_related(
    *, db_path: Any, sidecar_db_path: Any, event_id: str
) -> dict[str, Any] | None:
    conn = open_frigate_ro(db_path)
    try:
        row = conn.execute(
            "SELECT id, camera, label, start_time, end_time, data FROM event WHERE id = ?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        camera, label, start_time, end_time = (
            row["camera"], row["label"], row["start_time"], row["end_time"],
        )

        items: dict[str, dict[str, Any]] = {}

        # Overlap: other cameras, same label, span overlapping ours padded
        # ±20s (candidates with no end_time are treated as instantaneous).
        req_end = end_time if end_time is not None else start_time
        win_start = start_time - _RELATED_OVERLAP_PAD_S
        win_end = req_end + _RELATED_OVERLAP_PAD_S
        overlap_rows = conn.execute(
            "SELECT id, camera, label, start_time, end_time, data FROM event "
            "WHERE camera != ? AND label = ? AND id != ? "
            "AND start_time <= ? AND COALESCE(end_time, start_time) >= ?",
            (camera, label, event_id, win_end, win_start),
        ).fetchall()
        for r in overlap_rows:
            items[r["id"]] = _related_item(r, "overlap")

        # Linked: cross-camera dedup aliases -- takes priority over overlap.
        linked_ids = _linked_track_ids(sidecar_db_path, event_id)
        if linked_ids:
            placeholders = ",".join("?" for _ in linked_ids)
            linked_rows = conn.execute(
                "SELECT id, camera, label, start_time, end_time, data FROM event "
                f"WHERE id IN ({placeholders})",
                tuple(linked_ids),
            ).fetchall()
            for r in linked_rows:
                if r["camera"] == camera:
                    continue
                items[r["id"]] = _related_item(r, "linked")

        related = sorted(items.values(), key=lambda it: cast("float", it["start_time"]))
        return {"event_id": event_id, "related": related}
    finally:
        conn.close()


@router.get("/events/{event_id}/related", response_model=RelatedResponse)
async def events_related(event_id: str, request: Request) -> dict[str, Any]:
    """Other cameras that saw the same object as `event_id`: cards the push
    pipeline's cross-camera dedup already linked, unioned with same-label
    events on other cameras whose time span overlaps this one (padded ±20s).
    Events on `event_id`'s own camera are never included.
    """
    settings: Settings = request.app.state.settings
    result = await asyncio.to_thread(
        _query_related,
        db_path=settings.frigate.db_path,
        sidecar_db_path=settings.sidecar.db_path,
        event_id=event_id,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_EVENT_NOT_FOUND,
                "message": f"no such event: {event_id}",
            },
        )
    return result
