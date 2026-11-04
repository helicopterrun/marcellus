"""Sidecar-sqlite CRUD for the external-sources journal.

Tables (`external_sightings`, `external_source_state`) live in
`db.SIDECAR_SCHEMA`. Every function takes a plain `sqlite3.Connection`, is
synchronous, and commits its own writes -- callers from async code go through
`db.with_sidecar`.

Upsert rules: a poll beats a hint, `final` never goes back, and non-empty
media never becomes empty.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any, Literal

from marcellus.external.types import (
    Origin,
    Sighting,
    sighting_from_json,
    sighting_to_json,
)

UpsertResult = Literal["new", "updated", "unchanged"]


def _hash(s: Sighting) -> str:
    blob = json.dumps(sighting_to_json(s), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _row_to_sighting(row: sqlite3.Row) -> Sighting:
    return sighting_from_json(
        json.loads(row["sighting_json"]), raw=json.loads(row["raw_json"] or "{}")
    )


def upsert_sighting(
    conn: sqlite3.Connection, s: Sighting, origin: Origin, now: float
) -> UpsertResult:
    row = conn.execute(
        "SELECT * FROM external_sightings WHERE source_id = ? AND native_key = ?",
        (s.source_id, s.native_key),
    ).fetchone()

    merged = s
    stored_origin: Origin = origin
    if row is not None:
        if row["origin"] == "poll" and origin == "mqtt":
            return "unchanged"
        existing = _row_to_sighting(row)
        if existing.final and not s.final:
            return "unchanged"
        if not s.poster and not s.streams and (existing.poster or existing.streams):
            merged = dataclasses.replace(
                s,
                poster=existing.poster,
                streams=existing.streams,
                boxes=existing.boxes,
            )
        if row["origin"] == "poll" or origin == "poll":
            stored_origin = "poll"

    content_hash = _hash(merged)
    if row is not None and row["content_hash"] == content_hash and row["origin"] == stored_origin:
        return "unchanged"

    conn.execute(
        """
        INSERT INTO external_sightings (
            source_id, native_key, native_id, start_time, end_time, trigger,
            subject, subject_conf, labels_json, status, final, sighting_json,
            raw_json, origin, content_hash, first_seen, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, native_key) DO UPDATE SET
            native_id = excluded.native_id,
            start_time = excluded.start_time,
            end_time = excluded.end_time,
            trigger = excluded.trigger,
            subject = excluded.subject,
            subject_conf = excluded.subject_conf,
            labels_json = excluded.labels_json,
            status = excluded.status,
            final = excluded.final,
            sighting_json = excluded.sighting_json,
            raw_json = excluded.raw_json,
            origin = excluded.origin,
            content_hash = excluded.content_hash,
            updated_at = excluded.updated_at
        """,
        (
            merged.source_id,
            merged.native_key,
            merged.native_id,
            merged.start,
            merged.end,
            merged.trigger,
            merged.subject,
            merged.subject_conf,
            json.dumps(list(merged.labels)),
            merged.status,
            1 if merged.final else 0,
            json.dumps(sighting_to_json(merged)),
            json.dumps(dict(merged.raw)),
            stored_origin,
            content_hash,
            now,
            now,
        ),
    )
    conn.commit()
    return "new" if row is None else "updated"


def get_sighting(conn: sqlite3.Connection, source_id: str, native_key: str) -> Sighting | None:
    row = conn.execute(
        "SELECT sighting_json, raw_json FROM external_sightings "
        "WHERE source_id = ? AND native_key = ?",
        (source_id, native_key),
    ).fetchone()
    return None if row is None else _row_to_sighting(row)


def get_origin(conn: sqlite3.Connection, source_id: str, native_key: str) -> str | None:
    row = conn.execute(
        "SELECT origin FROM external_sightings WHERE source_id = ? AND native_key = ?",
        (source_id, native_key),
    ).fetchone()
    return None if row is None else str(row["origin"])


def list_sightings(
    conn: sqlite3.Connection,
    *,
    source_id: str | None = None,
    subject: str | None = None,
    subjects: Sequence[str] | None = None,
    since: float | None = None,
    before: float | None = None,
    limit: int = 50,
) -> list[Sighting]:
    """Newest first. `since` is inclusive, `before` exclusive (cursor)."""
    where: list[str] = []
    params: list[Any] = []
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    wanted = list(subjects) if subjects is not None else ([subject] if subject else None)
    if wanted is not None:
        where.append("subject IN (" + ",".join("?" * len(wanted)) + ")")
        params.extend(wanted)
    if since is not None:
        where.append("start_time >= ?")
        params.append(since)
    if before is not None:
        where.append("start_time < ?")
        params.append(before)
    sql = "SELECT sighting_json, raw_json FROM external_sightings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY start_time DESC, native_key DESC LIMIT ?"
    params.append(max(1, min(500, limit)))
    return [_row_to_sighting(r) for r in conn.execute(sql, params).fetchall()]


def open_sightings(
    conn: sqlite3.Connection, source_id: str, *, since: float
) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT native_key, native_id FROM external_sightings "
        "WHERE source_id = ? AND final = 0 AND start_time >= ? "
        "ORDER BY start_time ASC, native_key ASC",
        (source_id, since),
    ).fetchall()
    return [(str(r["native_key"]), r["native_id"]) for r in rows]


def newest_start(conn: sqlite3.Connection, source_id: str) -> float | None:
    row = conn.execute(
        "SELECT MAX(start_time) AS m FROM external_sightings WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return None if row is None or row["m"] is None else float(row["m"])


def count_since(conn: sqlite3.Connection, source_id: str, since: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM external_sightings WHERE source_id = ? AND start_time >= ?",
        (source_id, since),
    ).fetchone()
    return int(row["n"])


def prune(conn: sqlite3.Connection, cutoff: float) -> int:
    cur = conn.execute("DELETE FROM external_sightings WHERE start_time < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# --------------------------------------------------------------------------
# Per-source device state
# --------------------------------------------------------------------------


def set_state(conn: sqlite3.Connection, source_id: str, key: str, value: Any, now: float) -> None:
    conn.execute(
        "INSERT INTO external_source_state (source_id, key, value_json, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(source_id, key) DO UPDATE SET "
        "value_json = excluded.value_json, updated_at = excluded.updated_at",
        (source_id, key, json.dumps(value), now),
    )
    conn.commit()


def delete_state(conn: sqlite3.Connection, source_id: str, key: str) -> None:
    conn.execute(
        "DELETE FROM external_source_state WHERE source_id = ? AND key = ?",
        (source_id, key),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, source_id: str) -> dict[str, tuple[Any, float]]:
    rows = conn.execute(
        "SELECT key, value_json, updated_at FROM external_source_state WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    return {str(r["key"]): (json.loads(r["value_json"]), float(r["updated_at"])) for r in rows}
