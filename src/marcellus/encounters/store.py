"""Sidecar-sqlite CRUD for encounters (docs/encounters.md). Tables live in
`db.SIDECAR_SCHEMA` (`encounters`, `encounter_members`, `encounter_decisions`,
`encounter_state`). Every function here takes a plain `sqlite3.Connection`
and is synchronous, same convention as `push/store.py` -- callers from async
routes go through `db.with_sidecar`.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from marcellus.encounters.linker import Atom, LinkDecision, LinkerConfig, OpenEncounter


def _loads_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


def _row_to_open(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> OpenEncounter:
    member_rows = conn.execute(
        "SELECT atom_id, start_time, end_time FROM encounter_members "
        "WHERE encounter_id = ? ORDER BY start_time, joined_at",
        (row["id"],),
    ).fetchall()
    atom_ids = [m["atom_id"] for m in member_rows]
    # A member with end_time IS NULL is STILL ACTIVE -- its review segment
    # hasn't closed -- so it extends to `now`, not back to its own start
    # (linker._effective_end has the same rule and the same rationale).
    ends = [m["end_time"] if m["end_time"] is not None else now for m in member_rows]
    last_end = max(ends) if ends else row["start_time"]
    return OpenEncounter(
        encounter_id=row["id"],
        start_time=row["start_time"],
        last_end=last_end,
        cameras=_loads_list(row["cameras_json"]),
        labels=set(_loads_list(row["labels_json"])),
        identities=set(_loads_list(row["identities_json"])),
        zones=set(_loads_list(row["zones_json"])),
        atom_ids=atom_ids,
        peak_severity=row["peak_severity"],
    )


def load_open(conn: sqlite3.Connection, now: float) -> list[OpenEncounter]:
    """Every unsealed encounter, as the in-memory view `decide`/`apply` use."""
    rows = conn.execute(
        "SELECT id, start_time, cameras_json, labels_json, identities_json, "
        "zones_json, peak_severity FROM encounters WHERE sealed_at IS NULL"
    ).fetchall()
    return [_row_to_open(conn, row, now) for row in rows]


def load_one(conn: sqlite3.Connection, encounter_id: str, now: float) -> OpenEncounter | None:
    """Refresh one encounter's in-memory view after `upsert_atom` -- used by
    the reconciler to keep its working set current without a full reload."""
    row = conn.execute(
        "SELECT id, start_time, cameras_json, labels_json, identities_json, "
        "zones_json, peak_severity FROM encounters WHERE id = ?",
        (encounter_id,),
    ).fetchone()
    return _row_to_open(conn, row, now) if row is not None else None


def _member_row(conn: sqlite3.Connection, atom_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT encounter_id FROM encounter_members WHERE atom_id = ?", (atom_id,)
    ).fetchone()
    return row


def _encounter_sealed(conn: sqlite3.Connection, encounter_id: str) -> bool:
    row = conn.execute("SELECT sealed_at FROM encounters WHERE id = ?", (encounter_id,)).fetchone()
    return row is not None and row["sealed_at"] is not None


def _ensure_encounter(conn: sqlite3.Connection, encounter_id: str, atom: Atom, now: float) -> None:
    exists = conn.execute("SELECT 1 FROM encounters WHERE id = ?", (encounter_id,)).fetchone()
    if exists is not None:
        return
    conn.execute(
        "INSERT INTO encounters (id, start_time, end_time, sealed_at, cameras_json, "
        "labels_json, identities_json, zones_json, primary_event_id, peak_severity, "
        "atom_count, updated_at) VALUES (?, ?, NULL, NULL, '[]', '[]', '[]', '[]', NULL, "
        "'detection', 0, ?)",
        (encounter_id, atom.start_time, now),
    )


def upsert_atom(conn: sqlite3.Connection, atom: Atom, decision: LinkDecision, now: float) -> str:
    """Insert or update one atom's membership row, then refresh its
    encounter's aggregates. Returns the encounter id the atom ends up in.

    Membership never changes on update unless the atom's current encounter is
    sealed and `decision` names a different (live) encounter -- that's the
    one case where a late-arriving update has to be re-homed because its old
    encounter is no longer a linking candidate.
    """
    existing = _member_row(conn, atom.atom_id)
    labels_json = json.dumps(list(atom.labels))
    zones_json = json.dumps(list(atom.zones))
    event_ids_json = json.dumps(list(atom.event_ids))
    sub_labels_json = json.dumps(list(atom.sub_labels))

    if existing is not None:
        current_encounter_id = str(existing["encounter_id"])
        if (
            decision.encounter_id is not None
            and decision.encounter_id != current_encounter_id
            and _encounter_sealed(conn, current_encounter_id)
        ):
            encounter_id = decision.encounter_id
            _ensure_encounter(conn, encounter_id, atom, now)
            conn.execute(
                "UPDATE encounter_members SET encounter_id = ?, camera = ?, start_time = ?, "
                "end_time = ?, severity = ?, labels_json = ?, zones_json = ?, "
                "event_ids_json = ?, sub_labels_json = ?, link_reason = ?, confidence = ?, "
                "joined_at = ? WHERE atom_id = ?",
                (
                    encounter_id,
                    atom.camera,
                    atom.start_time,
                    atom.end_time,
                    atom.severity,
                    labels_json,
                    zones_json,
                    event_ids_json,
                    sub_labels_json,
                    decision.reason,
                    decision.confidence,
                    now,
                    atom.atom_id,
                ),
            )
        else:
            encounter_id = current_encounter_id
            conn.execute(
                "UPDATE encounter_members SET camera = ?, start_time = ?, end_time = ?, "
                "severity = ?, labels_json = ?, zones_json = ?, event_ids_json = ?, "
                "sub_labels_json = ? WHERE atom_id = ?",
                (
                    atom.camera,
                    atom.start_time,
                    atom.end_time,
                    atom.severity,
                    labels_json,
                    zones_json,
                    event_ids_json,
                    sub_labels_json,
                    atom.atom_id,
                ),
            )
    else:
        encounter_id = decision.encounter_id or uuid.uuid4().hex
        _ensure_encounter(conn, encounter_id, atom, now)
        conn.execute(
            "INSERT INTO encounter_members (atom_id, encounter_id, camera, start_time, "
            "end_time, severity, labels_json, zones_json, event_ids_json, sub_labels_json, "
            "link_reason, confidence, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atom.atom_id,
                encounter_id,
                atom.camera,
                atom.start_time,
                atom.end_time,
                atom.severity,
                labels_json,
                zones_json,
                event_ids_json,
                sub_labels_json,
                decision.reason,
                decision.confidence,
                now,
            ),
        )

    recompute(conn, encounter_id)
    conn.execute("UPDATE encounters SET updated_at = ? WHERE id = ?", (now, encounter_id))
    conn.commit()
    return encounter_id


def recompute(conn: sqlite3.Connection, encounter_id: str) -> None:
    """Recompute one encounter's aggregate columns from its current members."""
    members = conn.execute(
        "SELECT camera, start_time, end_time, severity, labels_json, zones_json, "
        "event_ids_json, sub_labels_json FROM encounter_members "
        "WHERE encounter_id = ? ORDER BY start_time, joined_at",
        (encounter_id,),
    ).fetchall()
    if not members:
        return

    cameras: list[str] = []
    labels: set[str] = set()
    identities: set[str] = set()
    zones: set[str] = set()
    start_time = min(m["start_time"] for m in members)
    any_open = any(m["end_time"] is None for m in members)
    end_time = None if any_open else max(m["end_time"] for m in members)
    peak_severity = "alert" if any(m["severity"] == "alert" for m in members) else "detection"

    primary_event_id: str | None = None
    fallback_event_id: str | None = None
    for m in members:
        if m["camera"] not in cameras:
            cameras.append(m["camera"])
        labels |= set(_loads_list(m["labels_json"]))
        identities |= set(_loads_list(m["sub_labels_json"]))
        zones |= set(_loads_list(m["zones_json"]))
        event_ids = _loads_list(m["event_ids_json"])
        if event_ids:
            if fallback_event_id is None:
                fallback_event_id = event_ids[0]
            if primary_event_id is None and m["severity"] == "alert":
                primary_event_id = event_ids[0]
    if primary_event_id is None:
        primary_event_id = fallback_event_id

    conn.execute(
        "UPDATE encounters SET start_time = ?, end_time = ?, cameras_json = ?, "
        "labels_json = ?, identities_json = ?, zones_json = ?, primary_event_id = ?, "
        "peak_severity = ?, atom_count = ? WHERE id = ?",
        (
            start_time,
            end_time,
            json.dumps(cameras),
            json.dumps(sorted(labels)),
            json.dumps(sorted(identities)),
            json.dumps(sorted(zones)),
            primary_event_id,
            peak_severity,
            len(members),
            encounter_id,
        ),
    )


def seal_stale(conn: sqlite3.Connection, now: float, cfg: LinkerConfig) -> int:
    """Seal every open encounter that's gone quiet or hit the duration cap.

    "Quiet" = no member end/start seen for more than 1.5x the largest
    configured gap; a live encounter well past `max_duration_s` is sealed
    outright even if it's still active, matching the linker's own hard cap.

    A member with `end_time IS NULL` is still active, so it contributes
    `now` (not its own `start_time`) to `last_end` -- an encounter with a
    genuinely open member is therefore never "quiet" (its `now - last_end`
    is ~0) and is never sealed by that rule. It CAN still be sealed by the
    `max_duration_s` cap even while open, matching the linker's own hard
    reject on an atom whose start is more than `max_duration_s` past the
    encounter's start.
    """
    largest_gap = max(cfg.gap_s.values()) if cfg.gap_s else 60.0
    threshold = 1.5 * largest_gap
    rows = conn.execute(
        "SELECT e.id AS id, e.start_time AS start_time, "
        "MAX(COALESCE(m.end_time, ?)) AS last_end "
        "FROM encounters e JOIN encounter_members m ON m.encounter_id = e.id "
        "WHERE e.sealed_at IS NULL GROUP BY e.id",
        (now,),
    ).fetchall()
    sealed = 0
    for row in rows:
        last_end = row["last_end"]
        span = last_end - row["start_time"]
        if (now - last_end) > threshold or span >= cfg.max_duration_s:
            conn.execute(
                "UPDATE encounters SET sealed_at = ?, updated_at = ? WHERE id = ?",
                (now, now, row["id"]),
            )
            sealed += 1
    conn.commit()
    return sealed


def list_recent(
    conn: sqlite3.Connection, *, since: float, limit: int = 200, camera: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM encounters WHERE start_time >= ?"
    params: list[Any] = [since]
    if camera:
        sql += " AND cameras_json LIKE ?"
        params.append(f'%"{camera}"%')
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get(conn: sqlite3.Connection, encounter_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM encounters WHERE id = ?", (encounter_id,)).fetchone()
    return dict(row) if row is not None else None


def members(conn: sqlite3.Connection, encounter_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM encounter_members WHERE encounter_id = ? ORDER BY start_time, joined_at",
        (encounter_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def decisions_for(conn: sqlite3.Connection, atom_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM encounter_decisions WHERE atom_id = ? ORDER BY created_at", (atom_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_watermark(conn: sqlite3.Connection) -> float | None:
    row = conn.execute("SELECT value FROM encounter_state WHERE key = 'watermark'").fetchone()
    return float(row["value"]) if row is not None else None


def set_watermark(conn: sqlite3.Connection, value: float) -> None:
    conn.execute(
        "INSERT INTO encounter_state (key, value) VALUES ('watermark', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(value),),
    )
    conn.commit()
