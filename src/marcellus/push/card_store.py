"""SQLite-backed storage for `cards.Card` (Elsinore Phase 2: delivery
pipeline).

Same shape as `push/store.py`: plain functions over an already-open
`sqlite3.Connection`, one table (`push_cards`, in `db.SIDECAR_SCHEMA`), no
ORM. `cards.py` stays pure and DB-free; this module is the only place that
turns a `Card` into rows and back.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import cast

from marcellus.push.cards import Card


def _row_to_card(row: sqlite3.Row) -> Card:
    try:
        peak_level = row["peak_level"]
    except (IndexError, KeyError):
        peak_level = row["level"]
    return Card(
        card_key=row["card_key"],
        level=row["level"],
        peak_level=peak_level or row["level"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state_since_at=row["state_since_at"],
        sound_count=row["sound_count"],
        handled=bool(row["handled"]),
        handled_at=row["handled_at"],
        last_sound_at=row["last_sound_at"],
        resound_count=row["resound_count"],
        resolved=bool(row["resolved"]),
        closed=bool(row["closed"]),
        zone_override_hit=bool(row["zone_override_hit"]),
        media_handle=row["media_handle"],
    )


def get_card(conn: sqlite3.Connection, card_key: str) -> Card | None:
    row = conn.execute(
        "SELECT * FROM push_cards WHERE card_key = ?", (card_key,)
    ).fetchone()
    return _row_to_card(row) if row is not None else None


def upsert_card(
    conn: sqlite3.Connection,
    card: Card,
    *,
    subject_kind: str = "",
    place_class: str = "",
    camera: str = "",
    zone_name: str = "",
    zones: tuple[str, ...] = (),
    label: str = "",
    family: str = "",
    encounter_id: str = "",
    event_camera: str = "",
    cameras_path: list[str] | None = None,
) -> None:
    """Insert or fully overwrite the row for `card.card_key`.

    Unlike `store.upsert_device` (an idempotent PUT that must preserve
    fields the caller didn't send), a card's row is always written by the
    single delivery pipeline that owns its full state -- there is nothing to
    merge, so this is a plain replace.
    """
    # Union with anything already recorded: overlapping cameras each
    # contribute their own zone list to the shared story.
    zone_set = {z for z in zones if z}
    if zone_name:
        zone_set.add(zone_name)
    existing = conn.execute(
        "SELECT zones_csv, zone_override_hit, media_handle, encounter_id, "
        "cameras_path_json FROM push_cards WHERE card_key = ?",
        (card.card_key,),
    ).fetchone()
    if existing is not None and existing["zones_csv"]:
        zone_set.update(z for z in existing["zones_csv"].split(",") if z)
    zones_csv = ",".join(sorted(zone_set))
    # Sticky: once a zone override fires for this story, it stays true for
    # the story's lifetime (read at resolve time -- see `send_card_mutation`).
    zone_override_hit = card.zone_override_hit
    if existing is not None:
        zone_override_hit = zone_override_hit or bool(existing["zone_override_hit"])
    # Sticky, same shape as zone_override_hit: a fresh handle (`card.media_handle`
    # non-empty) always wins, but a mutation that mints no media of its own
    # (escalate/resolve) falls back to whatever was last persisted instead of
    # blanking the story's thumbnail.
    media_handle = card.media_handle
    if not media_handle and existing is not None:
        media_handle = existing["media_handle"] or ""
    # Encounter id is sticky once known: a later mutation that couldn't
    # resolve one (link timeout) must not blank the card's grouping key.
    if not encounter_id and existing is not None:
        encounter_id = existing["encounter_id"] or ""
    # Ordered distinct cameras, first-seen order. Append only when this
    # mutation's own camera differs from the last entry -- a story bouncing
    # back to a camera it already crossed records the bounce, a run of
    # updates on one camera records nothing.
    if cameras_path is None:
        stored = parse_cameras_path(existing["cameras_path_json"]) if existing is not None else []
        # Only an encounter-stamped card grows a path: without an encounter
        # there is nothing asserting the cameras are one story, and the
        # ordinary zone/geo dedup merge keeps its " · also on X" copy.
        cameras_path = _append_camera(stored, event_camera or camera) if encounter_id else stored
    conn.execute(
        "INSERT INTO push_cards "
        "(card_key, level, peak_level, subject_kind, place_class, camera, zone_name, "
        " zones_csv, "
        " created_at, updated_at, state_since_at, sound_count, handled, handled_at, "
        " last_sound_at, resound_count, resolved, closed, zone_override_hit, "
        " label, family, media_handle, encounter_id, cameras_path_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(card_key) DO UPDATE SET "
        "level=excluded.level, peak_level=excluded.peak_level, "
        "subject_kind=excluded.subject_kind, "
        "place_class=excluded.place_class, camera=excluded.camera, "
        "zone_name=excluded.zone_name, zones_csv=excluded.zones_csv, "
        "updated_at=excluded.updated_at, "
        "state_since_at=excluded.state_since_at, "
        "sound_count=excluded.sound_count, handled=excluded.handled, "
        "handled_at=excluded.handled_at, last_sound_at=excluded.last_sound_at, "
        "resound_count=excluded.resound_count, resolved=excluded.resolved, "
        "closed=excluded.closed, zone_override_hit=excluded.zone_override_hit, "
        "label=excluded.label, family=excluded.family, "
        "media_handle=excluded.media_handle, "
        "encounter_id=excluded.encounter_id, "
        "cameras_path_json=excluded.cameras_path_json",
        (
            card.card_key, card.level, card.peak_level, subject_kind, place_class,
            camera, zone_name, zones_csv,
            card.created_at, card.updated_at, card.state_since_at, card.sound_count,
            int(card.handled), card.handled_at, card.last_sound_at, card.resound_count,
            int(card.resolved), int(card.closed), int(zone_override_hit),
            label, family, media_handle, encounter_id, json.dumps(cameras_path),
        ),
    )
    conn.commit()


def parse_cameras_path(raw: object) -> list[str]:
    """`cameras_path_json` -> list of camera names; anything unparseable (an
    old row, a hand-edited DB) reads as an empty path rather than raising on
    the delivery hot path."""
    if not raw:
        return []
    try:
        value = json.loads(raw if isinstance(raw, (str, bytes)) else "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v]


def _append_camera(path: list[str], camera: str) -> list[str]:
    """`path` with `camera` appended unless it is already the last entry --
    a run of updates on one camera adds nothing, a genuine crossing (or a
    bounce back to an earlier camera) adds one entry."""
    if camera and (not path or path[-1] != camera):
        return [*path, camera]
    return list(path)


def next_cameras_path(
    conn: sqlite3.Connection, card_key: str, camera: str
) -> list[str]:
    """What `card_key`'s camera path will be once this mutation on `camera`
    is persisted. The delivery pipeline needs it *before* the upsert (the
    push body and payload are built first), then hands the same list back
    to `upsert_card` so the two can never disagree."""
    return _append_camera(cameras_path(conn, card_key), camera)


def cameras_path(conn: sqlite3.Connection, card_key: str) -> list[str]:
    """Ordered distinct cameras this card's story has crossed."""
    row = conn.execute(
        "SELECT cameras_path_json FROM push_cards WHERE card_key = ?", (card_key,)
    ).fetchone()
    return parse_cameras_path(row["cameras_path_json"]) if row is not None else []


def find_open_card_by_encounter(
    conn: sqlite3.Connection, encounter_id: str, *, exclude_key: str = ""
) -> Card | None:
    """The oldest open (not resolved, not closed) card stamped with this
    encounter -- the merge target for a later camera's review of the same
    encounter (`push.encounter_merge`). Oldest, not newest, for the same
    reason `find_dedup_candidate` picks the oldest: with three cameras in
    one encounter every later camera should land on the first one's card,
    not on whichever was touched last.

    A resolved/closed card is deliberately NOT a candidate: an encounter
    whose card already ended never reopens (docs/push-notifications.md
    "Encounter-aware push"), the new review just mints its own card and is
    grouped by `thread-id` instead.
    """
    if not encounter_id:
        return None
    row = conn.execute(
        "SELECT * FROM push_cards WHERE encounter_id = ? AND closed = 0 "
        "AND resolved = 0 AND card_key != ? ORDER BY created_at ASC LIMIT 1",
        (encounter_id, exclude_key),
    ).fetchone()
    return _row_to_card(row) if row is not None else None


def mark_handled(conn: sqlite3.Connection, card_key: str, *, now: float) -> None:
    conn.execute(
        "UPDATE push_cards SET handled = 1, handled_at = ? WHERE card_key = ?",
        (now, card_key),
    )
    conn.commit()


def migrate_drop_zone_from_card_keys(conn: sqlite3.Connection) -> int:
    """One-off, idempotent migration: collapse rows written under the old
    `{camera}:{zone}:{subject_kind}:{subject_id}` card-key scheme onto the
    current `{camera}:{subject_kind}:{subject_id}` identity
    (`delivery.build_card_key`'s docstring has the live-observed failure
    that motivated dropping zone from identity).

    Old-format rows are recognized structurally: exactly 4 `:`-separated
    components (`str.split(":", 3)` so a subject id containing `:` stays
    intact as the 4th piece). System cards (`{camera}:system:{reason}`,
    3 components) were never zone-bearing and are left untouched, as are
    rows already in the 3-component post-fix shape.

    For each old-format key, dropping the zone component maps it onto a new
    key that may collide with other old-format rows for the same subject
    (this is exactly the fragmentation bug) and/or with a row a newer
    delivery already wrote under the fixed scheme. All contributors to a
    given new key are compared by `(updated_at, created_at)`; the newest
    wins and becomes that key's row. Every old-format row is then marked
    `resolved`/`closed` so none linger as phantom open cards -- including
    the winner's own old-format row, since its state now lives under the
    new key instead.

    Returns the number of old-format rows collapsed. Safe to call on every
    startup: a database with nothing in the old shape touches zero rows.
    """
    rows = conn.execute("SELECT * FROM push_cards").fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        # Already migrated (or a naturally-resolved old-format card that
        # never needed collapsing in the first place) -- old-format rows are
        # never deleted, only closed, so without this check every startup
        # would re-"collapse" the same rows forever and log a stale count.
        if row["closed"] and row["resolved"]:
            continue
        parts = row["card_key"].split(":", 3)
        if len(parts) == 4:
            camera, _zone, subject_kind, subject_id = parts
            new_key = f"{camera}:{subject_kind}:{subject_id}"
            groups.setdefault(new_key, []).append(row)

    collapsed = 0
    for new_key, old_rows in groups.items():
        existing_new = conn.execute(
            "SELECT * FROM push_cards WHERE card_key = ?", (new_key,)
        ).fetchone()
        candidates = list(old_rows) + ([existing_new] if existing_new is not None else [])
        winner = max(candidates, key=lambda r: (r["updated_at"], r["created_at"]))

        try:
            peak_level = winner["peak_level"]
        except (IndexError, KeyError):
            peak_level = winner["level"]
        try:
            winner_zones = winner["zones_csv"]
        except (IndexError, KeyError):
            winner_zones = ""
        conn.execute(
            "INSERT INTO push_cards "
            "(card_key, level, peak_level, subject_kind, place_class, camera, zone_name, "
            " zones_csv, "
            " created_at, updated_at, state_since_at, sound_count, handled, handled_at, "
            " last_sound_at, resound_count, resolved, closed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(card_key) DO UPDATE SET "
            "level=excluded.level, peak_level=excluded.peak_level, "
            "subject_kind=excluded.subject_kind, "
            "place_class=excluded.place_class, camera=excluded.camera, "
            "zone_name=excluded.zone_name, zones_csv=excluded.zones_csv, "
            "created_at=excluded.created_at, "
            "updated_at=excluded.updated_at, state_since_at=excluded.state_since_at, "
            "sound_count=excluded.sound_count, handled=excluded.handled, "
            "handled_at=excluded.handled_at, last_sound_at=excluded.last_sound_at, "
            "resound_count=excluded.resound_count, resolved=excluded.resolved, "
            "closed=excluded.closed",
            (
                new_key, winner["level"], peak_level or winner["level"],
                winner["subject_kind"], winner["place_class"],
                winner["camera"], winner["zone_name"], winner_zones,
                winner["created_at"], winner["updated_at"],
                winner["state_since_at"], winner["sound_count"], winner["handled"],
                winner["handled_at"], winner["last_sound_at"], winner["resound_count"],
                winner["resolved"], winner["closed"],
            ),
        )
        for row in old_rows:
            conn.execute(
                "UPDATE push_cards SET resolved = 1, closed = 1, updated_at = ? "
                "WHERE card_key = ?",
                (winner["updated_at"], row["card_key"]),
            )
            collapsed += 1
    conn.commit()
    return collapsed


def get_card_context(conn: sqlite3.Connection, card_key: str) -> dict[str, str] | None:
    """The copy context (`subject_kind`/`place_class`/`camera`/`zone_name`)
    stored alongside `card_key`'s row -- `None` if the card doesn't exist.
    Used by the cross-camera dedup path to recover the *merged* card's
    original camera, since a card that keeps its identity when a second
    camera starts contributing must also keep reporting the camera that
    first created it (`docs/push-notifications.md` "Cross-camera
    deduplication")."""
    row = conn.execute(
        "SELECT subject_kind, place_class, camera, zone_name, label, family, "
        "encounter_id, cameras_path_json FROM push_cards WHERE card_key = ?",
        (card_key,),
    ).fetchone()
    return dict(row) if row is not None else None


def find_dedup_candidate(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    zone_name: str,
    exclude_key: str,
    now: float,
    window_s: float,
    zones: tuple[str, ...] = (),
    neighbor_cameras: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """The oldest open card sharing `subject_kind` and at least one zone with
    this event, created within `window_s` of `now` -- the cross-camera dedup
    candidate for a *fresh* track (caller only calls this when its own card
    doesn't exist yet). Oldest, not newest: with three cameras sharing a
    zone, the first one's card is the one every later camera should merge
    onto, not whichever alias happened to be looked up last.

    `neighbor_cameras` widens the match: a candidate owned by a declared
    neighbor camera merges even with a disjoint zone set. Adjacent cameras
    watching the same approach often have no zone in common at all
    (stairway-tight vs walkway, observed 2026-08-14 as one walk producing
    an LA + summary row per camera).

    Matching is **zone-set intersection**, not first-zone equality: two
    overlapping cameras list the same walk under different first-zones
    (`['driveway']` vs `['back_walkway', 'driveway']`), which the old
    zone_name comparison missed — observed live 2026-08-14 as one walk
    producing a lock-screen row per camera.

    `exclude_key` guards against a card matching itself; in practice the
    caller's own key can't have a row yet (that's the precondition for
    calling this at all), but the check is free and cheap insurance against
    a future caller getting that precondition wrong.
    """
    event_zones = {z for z in zones if z}
    if zone_name:
        event_zones.add(zone_name)
    if not event_zones and not neighbor_cameras:
        return None
    rows = conn.execute(
        "SELECT card_key, zone_name, zones_csv, camera FROM push_cards "
        "WHERE subject_kind = ? AND closed = 0 AND resolved = 0 "
        "AND card_key != ? AND created_at >= ? "
        "ORDER BY created_at ASC",
        (subject_kind, exclude_key, now - window_s),
    ).fetchall()
    for row in rows:
        candidate_zones = {z for z in (row["zones_csv"] or "").split(",") if z}
        if row["zone_name"]:
            candidate_zones.add(row["zone_name"])
        if event_zones & candidate_zones:
            return cast(str, row["card_key"])
        if row["camera"] and row["camera"] in neighbor_cameras:
            return cast(str, row["card_key"])
    return None


def find_open_card_for_track(
    conn: sqlite3.Connection, *, camera: str, track_id: str, exclude_key: str,
) -> str | None:
    """An open card for this exact (camera, track_id) under any OTHER
    subject kind -- the label-flip case: Frigate re-labels a track mid-story
    (animal -> person), and since subject_kind is baked into the card key the
    new label would otherwise mint a sibling card for the same physical
    story. Kind lives in the middle of `{camera}:{kind}:{track_id}`, so
    match on the outer parts and verify by splitting."""
    rows = conn.execute(
        "SELECT card_key FROM push_cards "
        "WHERE closed = 0 AND resolved = 0 AND card_key != ? AND card_key LIKE ? "
        "ORDER BY created_at ASC",
        (exclude_key, f"{camera}:%:{track_id}"),
    ).fetchall()
    for row in rows:
        parts = row["card_key"].split(":", 2)
        if len(parts) == 3 and parts[0] == camera and parts[2] == track_id:
            return cast(str, row["card_key"])
    return None


def find_card_row_by_event_suffix(
    conn: sqlite3.Connection, event_id: str
) -> sqlite3.Row | None:
    """The `push_cards` row whose `card_key` ends with `:{event_id}` --
    `card_key` has no dedicated event-id column, so the id is recovered from
    its position as the trailing `{camera}:{subject_kind}:{track_id}`
    component (frigate's tracked-object id IS the event id).

    Matched with `substr`/`length` rather than `LIKE`, so an event id
    containing `%` or `_` (both LIKE wildcards) can never match the wrong
    row -- the comparison is a literal suffix compare, not a pattern.
    """
    suffix = ":" + event_id
    rows = conn.execute(
        "SELECT * FROM push_cards WHERE substr(card_key, -length(?)) = ?",
        (suffix, suffix),
    ).fetchall()
    if not rows:
        return None
    # Prefer the newest if more than one row's card_key happens to end with
    # this exact suffix (shouldn't normally happen -- card_key is a primary
    # key -- but track_id values are attacker/Frigate-controlled strings, so
    # don't assume uniqueness of the suffix match itself).
    return cast(sqlite3.Row, max(rows, key=lambda r: r["updated_at"]))


def find_track_alias_card_key(conn: sqlite3.Connection, track_id: str) -> str | None:
    """`push_card_track_aliases.card_key` for any row whose `track_id`
    matches, regardless of camera -- the fallback path when no `push_cards`
    row's key itself ends with this event id (the track merged into a card
    under a different track's identity)."""
    row = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE track_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (track_id,),
    ).fetchone()
    return row["card_key"] if row is not None else None


def get_card_row(conn: sqlite3.Connection, card_key: str) -> sqlite3.Row | None:
    """Raw row for `card_key`, for callers (e.g. the card-for-event route)
    that need columns `Card` itself doesn't carry (`camera`, `zones_csv`,
    `label`, `family`, ...) instead of the `_row_to_card` projection."""
    return cast(
        "sqlite3.Row | None",
        conn.execute("SELECT * FROM push_cards WHERE card_key = ?", (card_key,)).fetchone(),
    )


def get_track_alias(conn: sqlite3.Connection, camera: str, track_id: str) -> str | None:
    row = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE camera = ? AND track_id = ?",
        (camera, track_id),
    ).fetchone()
    return row["card_key"] if row is not None else None


def set_track_alias(
    conn: sqlite3.Connection, camera: str, track_id: str, card_key: str, now: float
) -> None:
    conn.execute(
        "INSERT INTO push_card_track_aliases (camera, track_id, card_key, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(camera, track_id) DO UPDATE SET "
        "card_key=excluded.card_key, created_at=excluded.created_at",
        (camera, track_id, card_key, now),
    )
    conn.commit()


def delete_track_alias(conn: sqlite3.Connection, camera: str, track_id: str) -> None:
    conn.execute(
        "DELETE FROM push_card_track_aliases WHERE camera = ? AND track_id = ?",
        (camera, track_id),
    )
    conn.commit()


def reap_cards(
    conn: sqlite3.Connection, *, older_than: float, now: float | None = None
) -> int:
    """Drop closed cards (and any aliases pointing at them) once they're old
    enough that nothing will look them up again.

    Without this the table grows by one row per event ever routed — nothing
    else deletes from `push_cards`. Only `closed` rows are candidates: an open
    card, however old, is still the identity a live track resolves against.
    """
    now = time.time() if now is None else now
    cutoff = now - older_than
    rows = conn.execute(
        "SELECT card_key FROM push_cards WHERE closed = 1 AND updated_at <= ?",
        (cutoff,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "DELETE FROM push_card_track_aliases WHERE card_key = ?", (row["card_key"],)
        )
        conn.execute("DELETE FROM push_cards WHERE card_key = ?", (row["card_key"],))
    # Aliases are normally deleted with their track or card, but an alias
    # whose card vanished by another path would otherwise sit forever.
    conn.execute("DELETE FROM push_card_track_aliases WHERE created_at <= ?", (cutoff,))
    return len(rows)


def close_stale_cards(
    conn: sqlite3.Connection, *, idle_for: float, now: float | None = None
) -> int:
    """Silently close open cards that have gone idle too long -- the resolve
    that never arrived, e.g. a dropped Frigate end or a failed write. Without
    this an open card leaks forever, inflating `extra_stories` on every
    device's Live Activity until the process restarts or the table is
    manually cleaned. Only `open` rows are candidates; a real loiter's
    updates keep `updated_at` moving and the card alive.
    """
    now = time.time() if now is None else now
    cutoff = now - idle_for
    cur = conn.execute(
        "UPDATE push_cards SET closed = 1, resolved = 1, updated_at = ? "
        "WHERE closed = 0 AND resolved = 0 AND updated_at <= ?",
        (now, cutoff),
    )
    conn.commit()
    return cur.rowcount


def list_open_urgent_cards(conn: sqlite3.Connection) -> list[tuple[Card, dict[str, str]]]:
    """Open (`closed = 0`, `resolved = 0`) `urgent` cards -- the candidate
    set the re-sound sweep checks against its timer. Paired with the copy
    context (`subject_kind`/`place_class`/`camera`/`zone_name`) since
    `Card` itself doesn't carry it and the sweep has no live event to
    re-derive it from -- only what was stored at the card's last mutation.
    """
    rows = conn.execute(
        "SELECT * FROM push_cards WHERE level = 'urgent' AND closed = 0 AND resolved = 0"
    ).fetchall()
    return [(_row_to_card(row), _row_to_ctx(row)) for row in rows]


def _row_to_ctx(row: sqlite3.Row) -> dict[str, str]:
    ctx = {
        "subject_kind": row["subject_kind"],
        "place_class": row["place_class"],
        "camera": row["camera"],
        "zone_name": row["zone_name"],
    }
    try:
        ctx["label"] = row["label"]
        ctx["family"] = row["family"]
    except (IndexError, KeyError):
        ctx["label"] = ""
        ctx["family"] = ""
    try:
        ctx["media_handle"] = row["media_handle"]
    except (IndexError, KeyError):
        ctx["media_handle"] = ""
    try:
        ctx["encounter_id"] = row["encounter_id"]
        ctx["cameras_path_json"] = row["cameras_path_json"] or "[]"
    except (IndexError, KeyError):
        ctx["encounter_id"] = ""
        ctx["cameras_path_json"] = "[]"
    return ctx


def list_open_cards(conn: sqlite3.Connection) -> list[tuple[Card, dict[str, str]]]:
    """Every open (`closed = 0`, `resolved = 0`) card, any level -- the
    candidate set for device-scoped Live Activity aggregation: one activity
    per device covers every open story, not just the one currently
    mutating. Modeled on `list_open_urgent_cards` without the level filter.
    """
    rows = conn.execute(
        "SELECT * FROM push_cards WHERE closed = 0 AND resolved = 0 "
        "ORDER BY updated_at DESC"
    ).fetchall()
    return [(_row_to_card(row), _row_to_ctx(row)) for row in rows]
