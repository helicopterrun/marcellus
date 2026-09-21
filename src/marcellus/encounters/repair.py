"""`marcellus encounters repair`: one-off/rerunnable fix-up for membership
rows written before PR #67's `start_time <= 0` guard and end_time no-regress
(docs/encounters.md "Repair").

Selects every `encounter_members` row with `start_time <= 0` OR
`end_time IS NULL`, looks each atom up in Frigate's `reviewsegment` table by
id, and either repairs it (start/end from Frigate) or deletes it (no Frigate
row, or a null-end segment old enough that Frigate would have closed it by
now). Touched encounters get their aggregates recomputed and are dropped if
left with zero members; `seal_stale` re-runs over the touched set.

Idempotent: a clean pass reports all-zero counts. Batches of 200 atoms, one
SAVEPOINT per atom, same pattern as `service.reconcile`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

from marcellus.encounters import store
from marcellus.encounters.linker import LinkerConfig
from marcellus.encounters.observations import load_direction

_BATCH_SIZE = 200


@dataclass
class RepairSummary:
    scanned: int = 0
    fixed_start: int = 0
    fixed_end: int = 0
    deleted_members: int = 0
    deleted_encounters: int = 0
    recomputed: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _suspect_atoms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT atom_id, encounter_id, start_time, end_time FROM encounter_members "
        "WHERE start_time <= 0 OR end_time IS NULL ORDER BY atom_id"
    ).fetchall()


def _frigate_row(frigate_conn: sqlite3.Connection, atom_id: str) -> sqlite3.Row | None:
    try:
        row: sqlite3.Row | None = frigate_conn.execute(
            "SELECT id, start_time, end_time FROM reviewsegment WHERE id = ?", (atom_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row


def repair(
    sidecar_conn: sqlite3.Connection,
    frigate_conn: sqlite3.Connection,
    now: float,
    cfg: LinkerConfig,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> RepairSummary:
    """Run one repair pass. `cfg.max_duration_s` bounds how old a still-open
    (NULL end_time) Frigate segment may be before it's treated as stale
    garbage rather than genuinely live."""
    summary = RepairSummary()
    rows = _suspect_atoms(sidecar_conn)
    if limit is not None:
        rows = rows[:limit]
    summary.scanned = len(rows)
    if dry_run:
        # Dry-run still classifies every row so the printed counts are
        # meaningful, it just never writes.
        touched_dummy: set[str] = set()
        for row in rows:
            _classify(row, frigate_conn, now, cfg, summary, touched_dummy)
        return summary

    touched_encounters: set[str] = set()
    for start in range(0, len(rows), _BATCH_SIZE):
        batch = rows[start : start + _BATCH_SIZE]
        sidecar_conn.execute("BEGIN")
        for row in batch:
            sidecar_conn.execute("SAVEPOINT repair_atom")
            try:
                _repair_one(row, sidecar_conn, frigate_conn, now, cfg, summary, touched_encounters)
            except Exception:
                sidecar_conn.execute("ROLLBACK TO SAVEPOINT repair_atom")
                sidecar_conn.execute("RELEASE SAVEPOINT repair_atom")
                raise
            else:
                sidecar_conn.execute("RELEASE SAVEPOINT repair_atom")
        sidecar_conn.commit()

    for encounter_id in touched_encounters:
        exists = sidecar_conn.execute(
            "SELECT 1 FROM encounters WHERE id = ?", (encounter_id,)
        ).fetchone()
        if exists is None:
            continue
        n = sidecar_conn.execute(
            "SELECT COUNT(*) AS n FROM encounter_members WHERE encounter_id = ?",
            (encounter_id,),
        ).fetchone()["n"]
        if n == 0:
            sidecar_conn.execute("DELETE FROM encounters WHERE id = ?", (encounter_id,))
            summary.deleted_encounters += 1
        else:
            store.recompute(sidecar_conn, encounter_id)
            sidecar_conn.execute(
                "UPDATE encounters SET updated_at = ? WHERE id = ?", (now, encounter_id)
            )
            summary.recomputed += 1
    sidecar_conn.commit()

    if touched_encounters:
        store.seal_stale(sidecar_conn, now, cfg)

    return summary


def _classify(
    row: sqlite3.Row,
    frigate_conn: sqlite3.Connection,
    now: float,
    cfg: LinkerConfig,
    summary: RepairSummary,
    touched: set[str],
) -> None:
    """Dry-run bookkeeping: same decision logic as `_repair_one`, no writes."""
    atom_id = row["atom_id"]
    frow = _frigate_row(frigate_conn, atom_id)
    if frow is None:
        summary.deleted_members += 1
        touched.add(str(row["encounter_id"]))
        return
    f_start = frow["start_time"]
    f_end = frow["end_time"]
    if f_start is None or f_start <= 0:
        summary.deleted_members += 1
        touched.add(str(row["encounter_id"]))
        return
    if f_end is None:
        if now - f_start > cfg.max_duration_s:
            summary.deleted_members += 1
            touched.add(str(row["encounter_id"]))
            return
        # still genuinely open -- untouched, but start may still need fixing
        if row["start_time"] <= 0:
            summary.fixed_start += 1
            touched.add(str(row["encounter_id"]))
        return
    if row["start_time"] <= 0:
        summary.fixed_start += 1
        touched.add(str(row["encounter_id"]))
    if row["end_time"] is None:
        summary.fixed_end += 1
        touched.add(str(row["encounter_id"]))


def _remove_and_track(
    sidecar_conn: sqlite3.Connection,
    atom_id: str,
    encounter_id: str,
    now: float,
    summary: RepairSummary,
    touched: set[str],
) -> None:
    """`store.remove_member` already deletes the donor encounter outright
    when it's left with zero members (`_donor_after_move`), so by the time
    it returns there's nothing left in `encounters` to recompute -- count it
    here rather than adding it to `touched` for the end-of-pass sweep."""
    store.remove_member(sidecar_conn, atom_id, now)
    summary.deleted_members += 1
    still_exists = sidecar_conn.execute(
        "SELECT 1 FROM encounters WHERE id = ?", (encounter_id,)
    ).fetchone()
    if still_exists is None:
        summary.deleted_encounters += 1
    else:
        touched.add(encounter_id)


def _repair_one(
    row: sqlite3.Row,
    sidecar_conn: sqlite3.Connection,
    frigate_conn: sqlite3.Connection,
    now: float,
    cfg: LinkerConfig,
    summary: RepairSummary,
    touched: set[str],
) -> None:
    atom_id = row["atom_id"]
    encounter_id = str(row["encounter_id"])
    frow = _frigate_row(frigate_conn, atom_id)

    if frow is None:
        _remove_and_track(sidecar_conn, atom_id, encounter_id, now, summary, touched)
        return

    f_start = frow["start_time"]
    f_end = frow["end_time"]

    if f_start is None or f_start <= 0:
        # Frigate itself has no usable start -- nothing to repair from.
        _remove_and_track(sidecar_conn, atom_id, encounter_id, now, summary, touched)
        return

    if f_end is None:
        if now - f_start > cfg.max_duration_s:
            # Ancient and still "open" -- Frigate would have closed a real
            # segment long ago. Never invent an end_time; delete instead.
            _remove_and_track(sidecar_conn, atom_id, encounter_id, now, summary, touched)
            return
        # Genuinely still young/open -- only fix start if needed, leave
        # end_time null.
        if row["start_time"] <= 0:
            sidecar_conn.execute(
                "UPDATE encounter_members SET start_time = ? WHERE atom_id = ?",
                (f_start, atom_id),
            )
            summary.fixed_start += 1
            touched.add(encounter_id)
        return

    fixed_something = False
    new_start = row["start_time"]
    if row["start_time"] <= 0:
        new_start = f_start
        fixed_something = True
        summary.fixed_start += 1
    new_end = row["end_time"]
    if row["end_time"] is None:
        new_end = f_end
        fixed_something = True
        summary.fixed_end += 1
    if not fixed_something:
        return

    sidecar_conn.execute(
        "UPDATE encounter_members SET start_time = ?, end_time = ? WHERE atom_id = ?",
        (new_start, new_end, atom_id),
    )
    touched.add(encounter_id)

    # Optional: cheap direction recompute for the repaired row. Best-effort,
    # never blocks the repair.
    try:
        event_ids_row = sidecar_conn.execute(
            "SELECT event_ids_json FROM encounter_members WHERE atom_id = ?", (atom_id,)
        ).fetchone()
        if event_ids_row is not None:
            import json

            event_ids = json.loads(event_ids_row["event_ids_json"] or "[]")
            direction = load_direction(frigate_conn, event_ids)
            store.set_direction(sidecar_conn, atom_id, direction, commit=False)
    except Exception:  # noqa: BLE001 -- direction is best-effort, never fatal
        pass
