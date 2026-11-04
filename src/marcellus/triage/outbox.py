"""Frigate+ outbox: uploads queued by a review verdict, sent after an undo window.

A verdict never talks to Frigate+ directly. It writes a `plus_outbox` row
with `not_before = now + triage.plus_delay_s`; `drain` (driven by the web
process every `triage.outbox_interval_s`) sends rows whose time has come.
That gives every verdict an undo -- `cancel_*` flips a still-queued row to
`cancelled` -- and turns a Frigate+ outage into retries instead of a lost
label or a stalled review page.

Three kinds, one per Frigate endpoint:

- `tp`    POST /api/events/{id}/plus {"include_annotation": 1} -- the
          snapshot plus the event's box under its label.
- `fp`    PUT  /api/events/{id}/false_positive -- uploads the snapshot
          without a box (Frigate passes no body to its own /plus call) and
          marks the box a false positive. Also the "wrong label" verdict:
          Frigate+'s annotating guide says a misidentified object is a false
          positive plus a correct label added on plus.frigate.video.
- `frame` POST /api/{camera}/plus/{frame_ts} -- an unlabeled recording
          frame, for a wildlife-finder hit Frigate never made an event of.

Uploads only ever leave the machine from here, and only for rows a person
queued.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marcellus.config import Settings
from marcellus.db import open_frigate_ro, open_sidecar
from marcellus.frigate_api import FrigateClient, FrigatePlusError

logger = logging.getLogger(__name__)

KINDS = ("tp", "fp", "frame")

#: Frigate answers these when the upload already happened (an earlier
#: attempt that timed out client-side but finished, or a submit from
#: Frigate's own UI). The outcome we wanted is in place: count it as sent.
_ALREADY = ("already submitted",)

#: Messages Frigate returns for a failure on the far side of the upload
#: (plus.frigate.video unreachable, a slow annotation call) or a state that
#: resolves itself (the event was still in progress). Worth retrying.
_TRANSIENT = ("error uploading", "in-progress", "in progress")

#: Base retry delay; doubles per attempt (60 s, 2 min, 4 min, ...).
_BACKOFF_S = 60.0

#: Rows handed to one drain pass. Each send is one blocking HTTP call of up
#: to 30 s, so a small batch keeps a pass from hogging its worker thread.
_DRAIN_BATCH = 10


class NotSubmittable(ValueError):
    """The event can't be uploaded as asked (no snapshot, still running,
    already uploaded the other way). `reason` is a short machine code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Queued:
    id: int
    kind: str
    send_at: float


def _event_row(frigate_db: str | Path, event_id: str) -> sqlite3.Row | None:
    conn = open_frigate_ro(frigate_db)
    try:
        row: sqlite3.Row | None = conn.execute(
            "SELECT id, camera, label, start_time, end_time, has_snapshot, plus_id, "
            "false_positive, json_extract(data, '$.box') AS box FROM event WHERE id = ?",
            (event_id,),
        ).fetchone()
        return row
    finally:
        conn.close()


def check_event(frigate_db: str | Path, event_id: str, kind: str) -> sqlite3.Row:
    """Raise NotSubmittable unless Frigate would accept `kind` for this event.

    The same preconditions Frigate's endpoints check, so a doomed upload is
    refused at verdict time with a reason the page can show, instead of
    failing a minute later in the outbox.
    """
    ev = _event_row(frigate_db, event_id)
    if ev is None:
        raise NotSubmittable("event_not_found", "event not found")
    if not ev["has_snapshot"]:
        raise NotSubmittable("no_snapshot", "no snapshot to upload")
    if ev["box"] is None:
        raise NotSubmittable("no_box", "event has no box")
    if kind == "tp" and ev["plus_id"]:
        raise NotSubmittable("already_sent", "already in Frigate+")
    if kind == "fp" and ev["false_positive"]:
        raise NotSubmittable("already_sent", "already in Frigate+ as a false positive")
    if kind == "fp" and ev["plus_id"]:
        # Frigate would add a false-positive box on top of the true-positive
        # annotation it uploaded before -- two contradicting labels on one
        # image. That fix belongs on plus.frigate.video.
        raise NotSubmittable(
            "sent_as_real", "already in Frigate+ as real: fix it on plus.frigate.video"
        )
    return ev


def enqueue_event(
    settings: Settings, conn: sqlite3.Connection, *, event_id: str, kind: str,
    now: float | None = None,
) -> Queued:
    """Queue an event upload, replacing any upload still queued for it.

    Raises NotSubmittable (nothing queued) when Frigate would refuse it.
    """
    if kind not in ("tp", "fp"):
        raise ValueError(f"event kind must be tp or fp, got {kind!r}")
    ev = check_event(settings.frigate.db_path, event_id, kind)
    now = time.time() if now is None else now
    cancel_event(conn, event_id)
    send_at = now + settings.triage.plus_delay_s
    cur = conn.execute(
        "INSERT INTO plus_outbox(kind, event_id, camera, label, item_ts, not_before, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, event_id, ev["camera"], ev["label"], ev["start_time"], send_at, now),
    )
    return Queued(int(cur.lastrowid or 0), kind, send_at)


def enqueue_frames(
    settings: Settings, conn: sqlite3.Connection, *, box_ids: list[int],
    now: float | None = None,
) -> list[Queued]:
    """Queue wildlife-hit frames (skipping any already sent or queued)."""
    now = time.time() if now is None else now
    send_at = now + settings.triage.plus_delay_s
    out: list[Queued] = []
    for box_id in box_ids:
        b = conn.execute(
            "SELECT camera, frame_ts, suggestion, plus_status FROM wildlife_boxes WHERE id = ?",
            (box_id,),
        ).fetchone()
        if b is None or b["plus_status"] in ("sent", "queued"):
            continue
        cur = conn.execute(
            "INSERT INTO plus_outbox(kind, box_id, camera, label, item_ts, not_before, "
            "created_at) VALUES ('frame', ?, ?, ?, ?, ?, ?)",
            (box_id, b["camera"], b["suggestion"] or "", b["frame_ts"], send_at, now),
        )
        conn.execute(
            "UPDATE wildlife_boxes SET plus_status = 'queued', plus_detail = NULL WHERE id = ?",
            (box_id,),
        )
        out.append(Queued(int(cur.lastrowid or 0), "frame", send_at))
    return out


def cancel_event(conn: sqlite3.Connection, event_id: str) -> int:
    cur = conn.execute(
        "UPDATE plus_outbox SET status = 'cancelled' WHERE event_id = ? AND status = 'queued'",
        (event_id,),
    )
    return cur.rowcount


def cancel_visit(conn: sqlite3.Connection, visit_id: int) -> int:
    rows = conn.execute(
        "SELECT o.id, o.box_id FROM plus_outbox o JOIN wildlife_boxes b ON b.id = o.box_id "
        "WHERE b.visit_id = ? AND o.status = 'queued'",
        (visit_id,),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE plus_outbox SET status = 'cancelled' WHERE id = ?", (r["id"],))
        conn.execute(
            "UPDATE wildlife_boxes SET plus_status = NULL WHERE id = ? AND plus_status = 'queued'",
            (r["box_id"],),
        )
    return len(rows)


def sent_for_event(conn: sqlite3.Connection, event_id: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM plus_outbox WHERE event_id = ? AND status = 'sent' LIMIT 1",
            (event_id,),
        ).fetchone()
        is not None
    )


def retry(conn: sqlite3.Connection, row_id: int, now: float | None = None) -> bool:
    """Put an `error` row back in the queue for an immediate attempt."""
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE plus_outbox SET status = 'queued', attempts = 0, not_before = ? "
        "WHERE id = ? AND status = 'error'",
        (now, row_id),
    )
    if cur.rowcount:
        conn.execute(
            "UPDATE wildlife_boxes SET plus_status = 'queued' WHERE id = "
            "(SELECT box_id FROM plus_outbox WHERE id = ? AND box_id IS NOT NULL)",
            (row_id,),
        )
    return bool(cur.rowcount)


# ----- sending -----


def _classify(exc: FrigatePlusError) -> str:
    """'sent' | 'retry' | 'error' for a failed call."""
    msg = str(exc).lower()
    if any(s in msg for s in _ALREADY):
        return "sent"
    if exc.status_code == 0 or exc.status_code >= 500:
        return "retry"
    if any(s in msg for s in _TRANSIENT):
        return "retry"
    return "error"


def _send_one(client: FrigateClient, row: sqlite3.Row, timeout: float) -> str:
    """Do the HTTP call for one row; returns the plus_id or Frigate's message."""
    if row["kind"] == "tp":
        return client.submit_plus(str(row["event_id"]), include_annotation=True)
    if row["kind"] == "fp":
        return client.submit_false_positive(str(row["event_id"]))
    return client.submit_plus_frame(str(row["camera"]), float(row["item_ts"]), timeout=timeout)


def _finish(
    conn: sqlite3.Connection, row: sqlite3.Row, *, status: str, detail: str,
    plus_id: str | None, now: float, next_try: float | None = None,
) -> None:
    if status == "queued":  # a retry
        conn.execute(
            "UPDATE plus_outbox SET attempts = attempts + 1, not_before = ?, detail = ? "
            "WHERE id = ?",
            (next_try, detail[:300], row["id"]),
        )
        return
    conn.execute(
        "UPDATE plus_outbox SET status = ?, attempts = attempts + 1, detail = ?, plus_id = ?, "
        "sent_at = ? WHERE id = ?",
        (status, detail[:300], plus_id, now if status == "sent" else None, row["id"]),
    )
    if row["box_id"] is not None:
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE wildlife_boxes SET plus_status = ?, plus_detail = ?, plus_at = ? WHERE id = ?",
            (status, detail[:300], stamp, row["box_id"]),
        )


def drain(settings: Settings, *, now: float | None = None, client: Any = None) -> dict[str, int]:
    """Send every due row (at most a batch). Returns counts by outcome.

    Holds no DB connection across an HTTP call: each row is re-read as
    still-queued right before sending, so an undo that lands mid-pass wins.
    """
    now = time.time() if now is None else now
    counts = {"sent": 0, "retry": 0, "error": 0}
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        due = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM plus_outbox WHERE status = 'queued' AND not_before <= ? "
                "ORDER BY not_before LIMIT ?",
                (now, _DRAIN_BATCH),
            )
        ]
    finally:
        conn.close()
    if not due:
        return counts

    own_client = client is None
    fc = FrigateClient(settings.frigate.base_url) if own_client else client
    try:
        for row_id in due:
            conn = open_sidecar(settings.sidecar.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM plus_outbox WHERE id = ? AND status = 'queued'", (row_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                continue  # undone since the SELECT above
            plus_id: str | None = None
            try:
                result = _send_one(fc, row, settings.wildlife.http_timeout_s)
                outcome, detail = "sent", result
                plus_id = result if row["kind"] != "frame" else None
            except FrigatePlusError as exc:
                outcome, detail = _classify(exc), str(exc)
            conn = open_sidecar(settings.sidecar.db_path)
            try:
                if outcome == "retry" and int(row["attempts"]) + 1 < (
                    settings.triage.outbox_max_attempts
                ):
                    delay = _BACKOFF_S * (2 ** int(row["attempts"]))
                    _finish(conn, row, status="queued", detail=detail, plus_id=None,
                            now=now, next_try=now + delay)
                    counts["retry"] += 1
                else:
                    final = "error" if outcome == "retry" else outcome
                    _finish(conn, row, status=final, detail=detail, plus_id=plus_id, now=now)
                    counts[final] += 1
                    if final == "error":
                        logger.warning("plus outbox: row %s (%s) failed: %s",
                                       row_id, row["kind"], detail)
                conn.commit()
            finally:
                conn.close()
    finally:
        if own_client:
            fc.close()
    return counts


# ----- reading -----


def summary(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, Any]:
    """Header chip numbers: what's waiting, what failed, the last 30 days' mix."""
    now = time.time() if now is None else now
    queued = conn.execute(
        "SELECT COUNT(*) AS n, MIN(not_before) AS next FROM plus_outbox WHERE status = 'queued'"
    ).fetchone()
    errors = conn.execute(
        "SELECT COUNT(*) AS n FROM plus_outbox WHERE status = 'error'"
    ).fetchone()
    mix = {
        r["kind"]: int(r["n"])
        for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM plus_outbox WHERE status = 'sent' AND sent_at >= ? "
            "GROUP BY kind",
            (now - 30 * 86400,),
        )
    }
    real = mix.get("tp", 0) + mix.get("frame", 0)
    total = real + mix.get("fp", 0)
    return {
        "queued": int(queued["n"]),
        "next_send_in_s": max(0, round(float(queued["next"]) - now)) if queued["next"] else None,
        "errors": int(errors["n"]),
        "sent_30d": total,
        "sent_30d_real": real,
        "sent_30d_fp": mix.get("fp", 0),
        "real_share": (real / total) if total else None,
    }


def recent(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict[str, Any]]:
    """Newest rows first, for the coverage page's outbox table."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM plus_outbox WHERE status != 'cancelled' "
            "ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, "
            "created_at DESC LIMIT ?",
            (limit,),
        )
    ]
