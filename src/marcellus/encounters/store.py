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
        "SELECT encounter_id, start_time, end_time, link_reason FROM encounter_members "
        "WHERE atom_id = ?",
        (atom_id,),
    ).fetchone()
    return row


def member_row(conn: sqlite3.Connection, atom_id: str) -> sqlite3.Row | None:
    """Public wrapper on `_member_row` for callers outside this module (e.g.
    `service._link_review`'s start_time <= 0 fallback)."""
    return _member_row(conn, atom_id)


def _encounter_sealed(conn: sqlite3.Connection, encounter_id: str) -> bool:
    row = conn.execute("SELECT sealed_at FROM encounters WHERE id = ?", (encounter_id,)).fetchone()
    return row is not None and row["sealed_at"] is not None


def _member_count(conn: sqlite3.Connection, encounter_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM encounter_members WHERE encounter_id = ?", (encounter_id,)
    ).fetchone()
    return int(row["n"])


def founder_singleton(conn: sqlite3.Connection, atom_id: str) -> str | None:
    """If `atom_id` is the sole member of an unsealed encounter it founded
    (its own membership row has `link_reason == "new"`), return that
    encounter's id -- else None.

    Callers exclude this id from the candidate set passed to `decide()`
    before re-deciding an already-stored atom: left in, the atom's own
    singleton encounter would win on `same_camera` (its only member IS the
    atom, so camera/labels always match) even when a real link (e.g.
    `companion`) exists elsewhere. Never recorded as a split decision --
    it's a per-call exclusion, not a permanent one.
    """
    row = conn.execute(
        "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = ?", (atom_id,)
    ).fetchone()
    if row is None or row["link_reason"] != "new":
        return None
    encounter_id = str(row["encounter_id"])
    if _encounter_sealed(conn, encounter_id):
        return None
    if _member_count(conn, encounter_id) != 1:
        return None
    return encounter_id


def _donor_after_move(conn: sqlite3.Connection, encounter_id: str, now: float) -> None:
    """After an atom leaves `encounter_id` for another encounter: recompute
    its aggregates from the members that remain, or delete it outright if
    none remain."""
    if _member_count(conn, encounter_id) == 0:
        conn.execute("DELETE FROM encounters WHERE id = ?", (encounter_id,))
        return
    recompute(conn, encounter_id)
    conn.execute("UPDATE encounters SET updated_at = ? WHERE id = ?", (now, encounter_id))


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


def member_unchanged(conn: sqlite3.Connection, atom: Atom) -> bool:
    """True if `atom`'s stored membership row already matches its current
    serialised values (camera, span, severity, labels/zones/event_ids/
    sub_labels) -- used by `service.reconcile` to skip a no-op decide/
    upsert for an atom Frigate hasn't actually changed."""
    row = conn.execute(
        "SELECT camera, start_time, end_time, severity, labels_json, zones_json, "
        "event_ids_json, sub_labels_json FROM encounter_members WHERE atom_id = ?",
        (atom.atom_id,),
    ).fetchone()
    if row is None:
        return False
    return bool(
        row["camera"] == atom.camera
        and row["start_time"] == atom.start_time
        and row["end_time"] == atom.end_time
        and row["severity"] == atom.severity
        and row["labels_json"] == json.dumps(list(atom.labels))
        and row["zones_json"] == json.dumps(list(atom.zones))
        and row["event_ids_json"] == json.dumps(list(atom.event_ids))
        and row["sub_labels_json"] == json.dumps(list(atom.sub_labels))
    )


def remove_member(conn: sqlite3.Connection, atom_id: str, now: float) -> str | None:
    """Drop `atom_id`'s membership row (its Frigate reviewsegment vanished)
    and clean up the encounter it leaves behind -- same donor cleanup as a
    re-home in `upsert_atom`. Returns the encounter id the atom left, or
    None if it had no membership row."""
    row = _member_row(conn, atom_id)
    if row is None:
        return None
    encounter_id = str(row["encounter_id"])
    conn.execute("DELETE FROM encounter_members WHERE atom_id = ?", (atom_id,))
    _donor_after_move(conn, encounter_id, now)
    return encounter_id


def prune(conn: sqlite3.Connection, now: float, retention_days: int) -> dict[str, int]:
    """Delete sealed encounters (and their members/decisions) whose
    `end_time` (or `sealed_at` when `end_time` is NULL) is older than
    `retention_days`. One transaction; unsealed and recent encounters are
    left untouched."""
    cutoff = now - (retention_days * 86400)
    rows = conn.execute(
        "SELECT id FROM encounters WHERE sealed_at IS NOT NULL "
        "AND COALESCE(end_time, sealed_at) < ?",
        (cutoff,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    out = {"encounters": 0, "members": 0, "decisions": 0}
    if not ids:
        return out
    placeholders = ",".join("?" for _ in ids)
    atom_rows = conn.execute(
        f"SELECT atom_id FROM encounter_members WHERE encounter_id IN ({placeholders})", ids
    ).fetchall()
    atom_ids = [r["atom_id"] for r in atom_rows]

    members_cur = conn.execute(
        f"DELETE FROM encounter_members WHERE encounter_id IN ({placeholders})", ids
    )
    out["members"] = members_cur.rowcount if members_cur.rowcount is not None else 0

    if atom_ids:
        atom_placeholders = ",".join("?" for _ in atom_ids)
        decisions_cur = conn.execute(
            f"DELETE FROM encounter_decisions WHERE encounter_id IN ({placeholders}) "
            f"OR atom_id IN ({atom_placeholders})",
            [*ids, *atom_ids],
        )
    else:
        decisions_cur = conn.execute(
            f"DELETE FROM encounter_decisions WHERE encounter_id IN ({placeholders})", ids
        )
    out["decisions"] = decisions_cur.rowcount if decisions_cur.rowcount is not None else 0
    encounters_cur = conn.execute(f"DELETE FROM encounters WHERE id IN ({placeholders})", ids)
    out["encounters"] = encounters_cur.rowcount if encounters_cur.rowcount is not None else 0
    conn.commit()
    return out


def upsert_atom(
    conn: sqlite3.Connection,
    atom: Atom,
    decision: LinkDecision,
    now: float,
    *,
    commit: bool = True,
) -> str:
    """Insert or update one atom's membership row, then refresh its
    encounter's aggregates. Returns the encounter id the atom ends up in.

    Membership changes on update in two cases: (1) the atom's current
    encounter is sealed and `decision` names a different (live) encounter --
    a late-arriving update has to be re-homed because its old encounter is no
    longer a linking candidate; (2) the atom is the lone founder of its own
    (unsealed) encounter -- `link_reason == "new"`, one member -- and
    `decision` (re-decided with that singleton excluded from candidates,
    see `founder_singleton`) found a real link elsewhere. Only lone founders
    move this way; an atom already grouped with others never churns.
    """
    existing = _member_row(conn, atom.atom_id)
    labels_json = json.dumps(list(atom.labels))
    zones_json = json.dumps(list(atom.zones))
    event_ids_json = json.dumps(list(atom.event_ids))
    sub_labels_json = json.dumps(list(atom.sub_labels))

    if existing is not None:
        current_encounter_id = str(existing["encounter_id"])
        # A human decision (split/pin) locks this atom's membership -- it is
        # never re-homed by a later decide()/reconcile pass, sealed donor or
        # not. `pin_atom`/`split_atom` are the only ways to move it after
        # that; see docs/encounters.md "Correcting encounters".
        human_locked = existing["link_reason"] in ("pinned", "split")
        sealed_rehome = (
            not human_locked
            and decision.encounter_id is not None
            and decision.encounter_id != current_encounter_id
            and _encounter_sealed(conn, current_encounter_id)
        )
        founder_rehome = (
            not human_locked
            and not sealed_rehome
            and decision.encounter_id is not None
            and decision.encounter_id != current_encounter_id
            and decision.reason != "new"
            and not _encounter_sealed(conn, current_encounter_id)
            and existing["link_reason"] == "new"
            and _member_count(conn, current_encounter_id) == 1
        )
        if sealed_rehome or founder_rehome:
            encounter_id = decision.encounter_id
            assert encounter_id is not None
            _ensure_encounter(conn, encounter_id, atom, now)
            existing_end_time = existing["end_time"]
            end_time = atom.end_time if atom.end_time is not None else existing_end_time
            conn.execute(
                "UPDATE encounter_members SET encounter_id = ?, camera = ?, start_time = ?, "
                "end_time = ?, severity = ?, labels_json = ?, zones_json = ?, "
                "event_ids_json = ?, sub_labels_json = ?, link_reason = ?, confidence = ?, "
                "joined_at = ? WHERE atom_id = ?",
                (
                    encounter_id,
                    atom.camera,
                    atom.start_time,
                    end_time,
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
            _donor_after_move(conn, current_encounter_id, now)
        else:
            encounter_id = current_encounter_id
            existing_end_time = existing["end_time"]
            end_time = atom.end_time if atom.end_time is not None else existing_end_time
            conn.execute(
                "UPDATE encounter_members SET camera = ?, start_time = ?, end_time = ?, "
                "severity = ?, labels_json = ?, zones_json = ?, event_ids_json = ?, "
                "sub_labels_json = ? WHERE atom_id = ?",
                (
                    atom.camera,
                    atom.start_time,
                    end_time,
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
    if commit:
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
    positive_starts = [m["start_time"] for m in members if m["start_time"] > 0]
    start_time = min(positive_starts) if positive_starts else min(m["start_time"] for m in members)
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


def add_decision(
    conn: sqlite3.Connection,
    atom_id: str,
    action: str,
    encounter_id: str,
    note: str | None,
    now: float,
) -> None:
    """Record one human decision, replacing any existing decision for the
    same atom+action+encounter (the table's PK)."""
    conn.execute(
        "INSERT INTO encounter_decisions (atom_id, action, encounter_id, created_at, note) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(atom_id, action, encounter_id) DO UPDATE SET "
        "created_at = excluded.created_at, note = excluded.note",
        (atom_id, action, encounter_id, f"{now:.6f}", note),
    )


def clear_decisions(conn: sqlite3.Connection, atom_id: str) -> None:
    conn.execute("DELETE FROM encounter_decisions WHERE atom_id = ?", (atom_id,))


def split_atom(conn: sqlite3.Connection, atom_id: str, now: float, *, commit: bool = True) -> str:
    """Split `atom_id` out of its current encounter into a fresh one of its
    own, recording a `split` decision against the donor. The new encounter's
    single member gets `link_reason='split'` -- never treated as a lone
    founder (`founder_singleton` requires `link_reason == 'new'`), and never
    re-homed by `upsert_atom` (see the `human_locked` guard there)."""
    row = _member_row(conn, atom_id)
    if row is None:
        raise ValueError(f"no such atom: {atom_id}")
    donor_id = str(row["encounter_id"])
    add_decision(conn, atom_id, "split", donor_id, None, now)

    new_encounter_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO encounters (id, start_time, end_time, sealed_at, cameras_json, "
        "labels_json, identities_json, zones_json, primary_event_id, peak_severity, "
        "atom_count, updated_at) VALUES (?, ?, NULL, NULL, '[]', '[]', '[]', '[]', NULL, "
        "'detection', 0, ?)",
        (new_encounter_id, row["start_time"], now),
    )
    conn.execute(
        "UPDATE encounter_members SET encounter_id = ?, link_reason = 'split', "
        "confidence = 1.0, joined_at = ? WHERE atom_id = ?",
        (new_encounter_id, now, atom_id),
    )
    _donor_after_move(conn, donor_id, now)
    recompute(conn, new_encounter_id)
    conn.execute("UPDATE encounters SET updated_at = ? WHERE id = ?", (now, new_encounter_id))
    if commit:
        conn.commit()
    return new_encounter_id


def pin_atom(
    conn: sqlite3.Connection,
    atom_id: str,
    target_encounter_id: str,
    now: float,
    *,
    commit: bool = True,
) -> str:
    """Pin `atom_id` into `target_encounter_id`, recording a `pin` decision
    and clearing any `split` decision that named this same target (a pin
    overrides an earlier split-away-from-here). The target may be sealed or
    unsealed; a sealed target stays sealed."""
    row = _member_row(conn, atom_id)
    if row is None:
        raise ValueError(f"no such atom: {atom_id}")
    if (
        conn.execute("SELECT 1 FROM encounters WHERE id = ?", (target_encounter_id,)).fetchone()
        is None
    ):
        raise ValueError(f"no such encounter: {target_encounter_id}")

    donor_id = str(row["encounter_id"])
    add_decision(conn, atom_id, "pin", target_encounter_id, None, now)
    conn.execute(
        "DELETE FROM encounter_decisions WHERE atom_id = ? AND action = 'split' "
        "AND encounter_id = ?",
        (atom_id, target_encounter_id),
    )

    conn.execute(
        "UPDATE encounter_members SET encounter_id = ?, link_reason = 'pinned', "
        "confidence = 1.0, joined_at = ? WHERE atom_id = ?",
        (target_encounter_id, now, atom_id),
    )
    if donor_id != target_encounter_id:
        _donor_after_move(conn, donor_id, now)
    recompute(conn, target_encounter_id)
    conn.execute("UPDATE encounters SET updated_at = ? WHERE id = ?", (now, target_encounter_id))
    if commit:
        conn.commit()
    return target_encounter_id


def merge_encounters(
    conn: sqlite3.Connection, source_id: str, target_id: str, now: float, *, commit: bool = True
) -> int:
    """Pin every member of `source_id` into `target_id` (recording a pin
    decision per atom). `source_id` is deleted once empty. Returns the
    number of atoms moved."""
    if conn.execute("SELECT 1 FROM encounters WHERE id = ?", (target_id,)).fetchone() is None:
        raise ValueError(f"no such encounter: {target_id}")
    if source_id == target_id:
        raise ValueError("source and target must differ")
    member_rows = conn.execute(
        "SELECT atom_id FROM encounter_members WHERE encounter_id = ?", (source_id,)
    ).fetchall()
    atom_ids = [str(r["atom_id"]) for r in member_rows]
    for atom_id in atom_ids:
        pin_atom(conn, atom_id, target_id, now, commit=False)
    if commit:
        conn.commit()
    return len(atom_ids)


def undo_decisions(
    conn: sqlite3.Connection, atom_id: str, now: float, *, commit: bool = True
) -> None:
    """Clear every decision recorded for `atom_id`. Membership is left as-is
    -- this only lifts the constraint on *future* re-links; a later
    decide()/reconcile pass is free to move the atom again (unless its
    membership row still says 'pinned'/'split', which `undo_decisions` does
    not change -- see docs/encounters.md)."""
    clear_decisions(conn, atom_id)
    if commit:
        conn.commit()


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


def purge_phantoms(
    sidecar_conn: sqlite3.Connection,
    frigate_conn: sqlite3.Connection,
    now: float,
    dry_run: bool = False,
) -> dict[str, int]:
    """Remove member rows synthesized by the pre-fix push backfill bug
    (`push/mqtt.py`'s `/api/events` backfill used to hand phantom
    `ReviewEvent`s straight to `EncounterService.observe_review`): a member
    with `start_time <= 0` whose `atom_id` has no matching row in Frigate's
    `reviewsegment` table is not a real review segment and is dropped via
    `remove_member` (donor recompute/delete, same as a vanished-segment
    cleanup). A zero-start member that DOES have a `reviewsegment` row is
    left alone -- it may be a legitimate open atom that just hasn't been
    given a real start_time yet.

    One transaction for the whole sweep: committed at the end unless
    `dry_run`, in which case everything is rolled back and the counts
    reflect what *would* have been removed.
    """
    candidate_rows = sidecar_conn.execute(
        "SELECT atom_id, encounter_id FROM encounter_members WHERE start_time <= 0"
    ).fetchall()
    out = {"candidates": len(candidate_rows), "removed": 0, "encounters_deleted": 0}
    for row in candidate_rows:
        atom_id = str(row["atom_id"])
        exists = frigate_conn.execute(
            "SELECT 1 FROM reviewsegment WHERE id = ?", (atom_id,)
        ).fetchone()
        if exists is not None:
            continue
        encounter_id = remove_member(sidecar_conn, atom_id, now)
        out["removed"] += 1
        if encounter_id is not None:
            still_there = sidecar_conn.execute(
                "SELECT 1 FROM encounters WHERE id = ?", (encounter_id,)
            ).fetchone()
            if still_there is None:
                out["encounters_deleted"] += 1
    if dry_run:
        sidecar_conn.rollback()
    else:
        sidecar_conn.commit()
    return out
