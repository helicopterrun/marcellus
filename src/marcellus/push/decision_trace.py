"""Durable routing decision log (alerts-slice1 §A).

One row per event decision (pre-fanout), in the sidecar SQLite DB
(`push_decisions`, `db.SIDECAR_SCHEMA`) rather than an in-memory ring buffer
-- restart no longer loses the trail. Same shape as `push/card_store.py`:
plain functions over an already-open `sqlite3.Connection`, no ORM.

`append` must NEVER raise into the push path -- every call site is deep in
delivery_wire's per-event routing, and a decision-log failure must not drop
the notification it's describing. Every public function that touches the DB
catches broadly and logs at warning.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SERVE_CAP = 200
_RETENTION_DAYS = 30
_PRUNE_INTERVAL_S = 3600.0

_last_prune_at = 0.0


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"dec-{row['rowid']:08d}",
        "ts": row["ts"],
        "camera": row["camera"],
        "label": row["label"],
        "subject": row["subject"],
        "zones": [z for z in (row["zones_csv"] or "").split(",") if z],
        "place": row["place"],
        "level": row["level"],
        "reasons": [r for r in (row["reasons_csv"] or "").split(",") if r],
        "event_id": row["event_id"],
        "stage": row["stage"] or "",
        "modifiers": [m for m in (row["modifiers_csv"] or "").split(",") if m],
        "card_key": row["card_key"] or "",
        "mutation": row["mutation"] or "",
        "zone": row["zone"] or "",
        "sound": bool(row["sound"]),
        "sent": row["sent"] or 0,
    }
    if row["family"] is not None:
        entry["family"] = row["family"]
    if row["la_started"] is not None:
        entry["la_started"] = bool(row["la_started"])
    if row["la_reason"] is not None:
        entry["la_reason"] = row["la_reason"]
    return entry


def _maybe_prune(conn: sqlite3.Connection) -> None:
    global _last_prune_at
    now = time.time()
    if now - _last_prune_at < _PRUNE_INTERVAL_S:
        return
    _last_prune_at = now
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_DAYS * 86400
    cutoff_ts = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM push_decisions WHERE ts < ?", (cutoff_ts,))


def append(
    conn: sqlite3.Connection,
    *,
    camera: str,
    label: str,
    subject: str,
    zones: list[str],
    place: str,
    level: str,
    reasons: list[str],
    event_id: str,
    stage: str = "",
    modifiers: tuple[str, ...] = (),
    card_key: str = "",
    mutation: str = "",
    zone: str = "",
    sound: bool = False,
    sent: int = 0,
) -> dict[str, Any]:
    """Append a decision entry. Returns the entry for testing convenience, or
    an empty dict if the write failed -- this must never raise into the push
    path."""
    try:
        cur = conn.execute(
            "INSERT INTO push_decisions ("
            "ts, camera, label, subject, zones_csv, place, level, reasons_csv, "
            "event_id, stage, modifiers_csv, card_key, mutation, zone, sound, sent"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                camera,
                label,
                subject,
                ",".join(zones),
                place,
                level,
                ",".join(reasons),
                event_id,
                stage,
                ",".join(modifiers),
                card_key,
                mutation,
                zone,
                1 if sound else 0,
                sent,
            ),
        )
        rowid = cur.lastrowid
        _maybe_prune(conn)
        conn.commit()
        row = conn.execute(
            "SELECT rowid, * FROM push_decisions WHERE rowid = ?", (rowid,)
        ).fetchone()
        return _row_to_entry(row) if row is not None else {}
    except sqlite3.DatabaseError:
        logger.warning("decision_trace.append failed", exc_info=True)
        return {}


def annotate(
    conn: sqlite3.Connection,
    event_id: str,
    *,
    family: str | None = None,
    la_started: bool | None = None,
    la_reason: str | None = None,
    sent: int | None = None,
    sound: bool | None = None,
) -> None:
    """Patch the newest row for `event_id` with the Live Activity side of the
    decision (one alerts stack: the feed covers the whole stack, not just
    banner routing), or fields only known after the push actually went out
    (`sent`, `sound`). No-op when no row matches. Never raises into the push
    path."""
    try:
        row = conn.execute(
            "SELECT rowid FROM push_decisions WHERE event_id = ? ORDER BY rowid DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if row is None:
            return
        rowid = row["rowid"]
        updates: list[str] = []
        params: list[Any] = []
        if family is not None:
            updates.append("family = ?")
            params.append(family)
        if la_started is not None:
            updates.append("la_started = ?")
            params.append(1 if la_started else 0)
        if la_reason is not None:
            updates.append("la_reason = ?")
            params.append(la_reason)
        if sent is not None:
            updates.append("sent = ?")
            params.append(sent)
        if sound is not None:
            updates.append("sound = ?")
            params.append(1 if sound else 0)
        if not updates:
            return
        params.append(rowid)
        conn.execute(
            f"UPDATE push_decisions SET {', '.join(updates)} WHERE rowid = ?",
            params,
        )
        conn.commit()
    except sqlite3.DatabaseError:
        logger.warning("decision_trace.annotate failed", exc_info=True)


def reasons_for(conn: sqlite3.Connection, event_id: str) -> list[str]:
    """Best-effort `reasons` for the newest row matching `event_id`, or `[]`
    when there is none. Callers (e.g. the card-for-event route) must treat
    this as optional context, never as a durable guarantee."""
    try:
        row = conn.execute(
            "SELECT reasons_csv FROM push_decisions WHERE event_id = ? ORDER BY rowid DESC LIMIT 1",
            (event_id,),
        ).fetchone()
        if row is None:
            return []
        return [r for r in (row["reasons_csv"] or "").split(",") if r]
    except sqlite3.DatabaseError:
        logger.warning("decision_trace.reasons_for failed", exc_info=True)
        return []


def _silence_lookup(conn: sqlite3.Connection) -> dict[tuple[str, str, str], dict[str, str]]:
    """Map of `("zone", zone, subject)` and `("cell", subject, place)` keys to
    the most recently applied silence, read at serve time (spec §A:
    `silenced` reflects the *current* state of `push_silences`, not what was
    true when the decision was recorded)."""
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT kind, zone, subject, place, applied FROM push_silences ORDER BY id ASC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return result
    for row in rows:
        if row["kind"] == "zone_override" and row["zone"]:
            result[("zone", row["zone"], row["subject"])] = {
                "zone": row["zone"],
                "subject": row["subject"],
                "level": row["applied"],
            }
        elif row["kind"] == "outcome_cell":
            result[("cell", row["subject"], row["place"])] = {
                "zone": "",
                "subject": row["subject"],
                "level": row["applied"],
            }
    return result


def recent(
    conn: sqlite3.Connection,
    limit: int = 50,
    *,
    before: str | None = None,
    card_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` most recent entries, newest first. `before` is a
    decision `id` cursor for older pages; `card_key` filters to one card."""
    try:
        limit = max(1, min(limit, _SERVE_CAP))
        clauses: list[str] = []
        params: list[Any] = []
        if before:
            try:
                before_rowid = int(before.split("-", 1)[1])
            except (IndexError, ValueError):
                before_rowid = None
            if before_rowid is not None:
                clauses.append("rowid < ?")
                params.append(before_rowid)
        if card_key:
            clauses.append("card_key = ?")
            params.append(card_key)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT rowid, * FROM push_decisions {where} ORDER BY rowid DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        entries = [_row_to_entry(r) for r in rows]

        silences = _silence_lookup(conn)
        for entry in entries:
            zone_key = ("zone", entry["zone"], entry["subject"])
            cell_key = ("cell", entry["subject"], entry["place"])
            entry["silenced"] = silences.get(zone_key) or silences.get(cell_key)
        return entries
    except sqlite3.DatabaseError:
        logger.warning("decision_trace.recent failed", exc_info=True)
        return []


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cheap `push_decisions`-only half of `GET /v1/push/status` (alerts-slice1
    §B): the newest decision, the newest *sent* one, and how many decisions
    have landed since. Never raises -- a status endpoint that 500s because
    its own db read failed is worse than one that reports nulls."""
    try:
        row = conn.execute(
            "SELECT rowid, ts FROM push_decisions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        last_decision_at = row["ts"] if row is not None else None

        sent_row = conn.execute(
            "SELECT rowid, ts, level, card_key FROM push_decisions "
            "WHERE sent > 0 ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if sent_row is not None:
            since_row = conn.execute(
                "SELECT COUNT(*) AS c FROM push_decisions WHERE rowid > ?",
                (sent_row["rowid"],),
            ).fetchone()
            return {
                "last_decision_at": last_decision_at,
                "last_sent_at": sent_row["ts"],
                "last_sent_level": sent_row["level"],
                "last_sent_card_key": sent_row["card_key"],
                "decisions_since_last_sent": since_row["c"],
            }
        # Never sent anything: count every decision on record (documented
        # resolution -- there's no "last sent" rowid to count from).
        all_row = conn.execute("SELECT COUNT(*) AS c FROM push_decisions").fetchone()
        return {
            "last_decision_at": last_decision_at,
            "last_sent_at": None,
            "last_sent_level": None,
            "last_sent_card_key": None,
            "decisions_since_last_sent": all_row["c"] if all_row else 0,
        }
    except sqlite3.DatabaseError:
        logger.warning("decision_trace.status failed", exc_info=True)
        return {
            "last_decision_at": None,
            "last_sent_at": None,
            "last_sent_level": None,
            "last_sent_card_key": None,
            "decisions_since_last_sent": 0,
        }


def reset_for_tests(conn: sqlite3.Connection) -> None:
    """Clear all state — test isolation only."""
    conn.execute("DELETE FROM push_decisions")
    conn.commit()
