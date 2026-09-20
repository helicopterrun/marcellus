"""Delivery receipts (alerts-slice2 §A) -- `push_receipts` in the sidecar
SQLite DB, posted by the NSE right after it hands content to the OS, and by
the app when flushing receipts the NSE couldn't deliver.

Pairing decision: the spec's contract describes pairing receipts to
`push_sends` by `(token, card_key, mutation, state_since_ts)` when that table
carries `state_since_ts`, falling back to `(token, card_key, mutation)` +
nearest-prior-send-within-24h otherwise. This repo's `push_sends` table is
the *situation*-pipeline's rolling rate-limit window (keyed on
`situation_id`, no `card_key`/`mutation`/`state_since_ts` at all) and never
recorded card mutations before this slice -- see `db.py`'s
`push_card_sends`, added here for exactly this purpose. `push_card_sends`
has no `state_since_ts` column either, so every receipt pairs by the
fallback rule: `(apns_token, card_key, mutation)`, most recent send at or
before `received_ts` within the last 24h.

Same conn-first, never-raise-into-the-caller-path shape as
`decision_trace.py` -- a receipts POST is best-effort telemetry, not a
critical path, but unlike the push pipeline it *is* the whole point of this
endpoint, so failures here are surfaced to the caller (the route), just not
allowed to corrupt already-accepted rows in the same batch.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 30
_PAIR_WINDOW_S = 86400.0  # 24h

_last_prune_at = 0.0
_PRUNE_INTERVAL_S = 3600.0


def _maybe_prune(conn: sqlite3.Connection, now: float) -> None:
    global _last_prune_at
    if now - _last_prune_at < _PRUNE_INTERVAL_S:
        return
    _last_prune_at = now
    cutoff = now - _RETENTION_DAYS * 86400
    conn.execute("DELETE FROM push_receipts WHERE created_at < ?", (cutoff,))


def _find_send(
    conn: sqlite3.Connection, *, apns_token: str, card_key: str, mutation: str, received_ts: float
) -> float | None:
    row = conn.execute(
        "SELECT sent_at FROM push_card_sends "
        "WHERE apns_token = ? AND card_key = ? AND mutation = ? "
        "AND sent_at <= ? AND sent_at >= ? "
        "ORDER BY sent_at DESC LIMIT 1",
        (apns_token, card_key, mutation, received_ts, received_ts - _PAIR_WINDOW_S),
    ).fetchone()
    return float(row["sent_at"]) if row is not None else None


def record(conn: sqlite3.Connection, receipts: list[dict[str, Any]]) -> dict[str, int]:
    """Insert a batch of receipts. Returns `{"accepted": n, "matched": m}`.

    `accepted` counts every receipt in the batch that was processed without
    error (duplicates included -- a duplicate is not an error, per spec).
    `matched` counts receipts that paired with a `push_card_sends` row,
    among the ones newly inserted this call (a duplicate that matched on an
    earlier call is not re-counted).
    """
    now = time.time()
    accepted = 0
    matched = 0
    for r in receipts:
        try:
            apns_token = str(r["apns_token"])
            card_key = str(r["card_key"])
            mutation = str(r["mutation"])
            state_since_ts = float(r["state_since_ts"])
            received_ts = float(r["received_ts"])
        except (KeyError, TypeError, ValueError):
            logger.warning("push receipts: skipping malformed receipt %r", r)
            continue
        media_attached = bool(r.get("media_attached", False))
        source = str(r.get("source", ""))

        sent_at = _find_send(
            conn,
            apns_token=apns_token,
            card_key=card_key,
            mutation=mutation,
            received_ts=received_ts,
        )
        latency_s = (received_ts - sent_at) if sent_at is not None else None

        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO push_receipts "
                "(apns_token, card_key, mutation, state_since_ts, sent_at, received_at, "
                "latency_s, media_attached, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    apns_token,
                    card_key,
                    mutation,
                    state_since_ts,
                    sent_at,
                    received_ts,
                    latency_s,
                    1 if media_attached else 0,
                    source,
                    now,
                ),
            )
        except sqlite3.DatabaseError:
            logger.warning("push receipts: insert failed", exc_info=True)
            continue

        accepted += 1
        if cur.rowcount and sent_at is not None:
            matched += 1

    _maybe_prune(conn, now)
    conn.commit()
    return {"accepted": accepted, "matched": matched}


def reset_for_tests(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM push_receipts")
    conn.commit()
