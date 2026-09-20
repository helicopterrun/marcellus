"""Stream raw events from Frigate's DB as a list of dicts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from marcellus.db import (
    open_frigate_ro,
    parse_event_data,
    read_with_retry,
    time_window_clause,
)

_OPTIONAL_COLS = (
    "area",
    "ratio",
    "region",
    "box",
    "retain_indefinitely",
    "plus_id",
    "sub_label",
    "false_positive",
)


def _column_select(conn: Any) -> str:
    present = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
    parts = [
        "id", "camera", "label", "start_time", "end_time",
        "score", "top_score", "has_clip", "has_snapshot", "zones", "data",
    ]
    for col in _OPTIONAL_COLS:
        parts.append(col if col in present else f"NULL AS {col}")
    return ", ".join(parts)


def pull(
    *,
    frigate_db: str | Path,
    days: int = 14,
    camera: str | None = None,
    label: str | None = None,
) -> Iterator[dict[str, Any]]:
    where, params = time_window_clause(days)
    if camera:
        where += " AND camera = ?"
        params.append(camera)
    if label:
        where += " AND label = ?"
        params.append(label)

    def _fetch_rows() -> list[Any]:
        # A full read from scratch -- opens its own connection so a retry
        # after `database is locked` doesn't reuse a connection that just
        # failed (see db.read_with_retry).
        conn = open_frigate_ro(frigate_db)
        try:
            cols = _column_select(conn)
            sql = f"SELECT {cols} FROM event WHERE {where} ORDER BY start_time"
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    rows = read_with_retry(_fetch_rows)
    for row in rows:
        ev = parse_event_data(row)
        try:
            zones = json.loads(ev["zones"]) if ev.get("zones") else []
        except (TypeError, json.JSONDecodeError):
            zones = []
        yield {
            "id": ev["id"],
            "camera": ev["camera"],
            "label": ev["label"],
            "sub_label": ev["sub_label"],
            "start_time": ev["start_time"],
            "end_time": ev["end_time"],
            "duration": (ev["end_time"] or ev["start_time"]) - ev["start_time"],
            "score": ev["data_score"] if ev["data_score"] is not None else ev["score"],
            "top_score": ev["data_top_score"]
            if ev["data_top_score"] is not None
            else ev["top_score"],
            "false_positive": ev["false_positive"],
            "retain_indefinitely": ev["retain_indefinitely"],
            "has_clip": bool(ev["has_clip"]),
            "has_snapshot": bool(ev["has_snapshot"]),
            "area": ev["area"],
            "ratio": ev["ratio"],
            "region": ev["data_region"],
            "box": ev["data_box"],
            "zones": zones,
            "type": ev.get("data_type"),
            "plus_id": ev["plus_id"],
        }
