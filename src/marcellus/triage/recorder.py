"""Record / clear / count triage labels in the sidecar DB.

Triage labels are tp / fp / skip and live in the sidecar's `triage_labels`
table — we never touch Frigate's `event.false_positive` column because that
field has Frigate+ upload semantics in upstream code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from marcellus.db import open_frigate_ro, open_sidecar

Label = Literal["fp", "tp", "skip"]
VALID_LABELS: tuple[Label, ...] = ("fp", "tp", "skip")


class EventNotFoundError(LookupError):
    """The event_id was not found in Frigate's DB."""


class AlreadyLabeledError(RuntimeError):
    """An existing label is present and `force=False`."""

    def __init__(self, event_id: str, existing: str) -> None:
        super().__init__(f"{event_id} already labeled '{existing}'")
        self.event_id = event_id
        self.existing = existing


def record(
    *,
    frigate_db: str | Path,
    sidecar_db: str | Path,
    event_id: str,
    label: Label,
    note: str | None = None,
    session: str | None = None,
    force: bool = False,
) -> dict[str, str | None]:
    """Insert or update a triage label. Verifies the event exists first."""
    if label not in VALID_LABELS:
        raise ValueError(f"label must be one of {VALID_LABELS}, got {label!r}")

    ro = open_frigate_ro(frigate_db)
    try:
        row = ro.execute("SELECT id FROM event WHERE id = ?", (event_id,)).fetchone()
    finally:
        ro.close()
    if not row:
        raise EventNotFoundError(event_id)

    now = datetime.now(timezone.utc).isoformat()
    conn = open_sidecar(sidecar_db)
    try:
        existing_row = conn.execute(
            "SELECT label FROM triage_labels WHERE event_id = ?", (event_id,)
        ).fetchone()
        before = str(existing_row["label"]) if existing_row else None
        if existing_row and before is not None and not force:
            raise AlreadyLabeledError(event_id, before)

        conn.execute(
            """
            INSERT INTO triage_labels(event_id, label, note, labeled_at, session)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                label=excluded.label, note=excluded.note,
                labeled_at=excluded.labeled_at, session=excluded.session
            """,
            (event_id, label, note, now, session),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": event_id, "before": before, "after": label}


def clear(*, sidecar_db: str | Path, event_id: str) -> dict[str, int | str]:
    conn = open_sidecar(sidecar_db)
    try:
        cur = conn.execute("DELETE FROM triage_labels WHERE event_id = ?", (event_id,))
        conn.commit()
        return {"id": event_id, "cleared": cur.rowcount}
    finally:
        conn.close()


def stats(*, sidecar_db: str | Path) -> dict[str, object]:
    conn = open_sidecar(sidecar_db)
    try:
        rows = conn.execute(
            "SELECT label, COUNT(*) AS n FROM triage_labels GROUP BY label"
        ).fetchall()
        by_label = {row["label"]: row["n"] for row in rows}
        return {"total": sum(by_label.values()), "by_label": by_label}
    finally:
        conn.close()
