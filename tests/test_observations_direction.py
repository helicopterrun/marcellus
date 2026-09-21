"""encounters/observations.py: pure direction derivation (docs/encounters.md
"Observations" / M1 spec)."""

from __future__ import annotations

import json
import sqlite3

from marcellus.encounters.observations import DIR_SOURCE_NONE, derive_direction, load_direction


def _event(
    zones: object = None, box: object = None, path_data: object = None, **extra: object
) -> dict:
    data: dict[str, object] = {}
    if box is not None:
        data["box"] = box
    if path_data is not None:
        data["path_data"] = path_data
    data.update(extra)
    return {
        "id": "ev1",
        "zones": json.dumps(zones) if zones is not None else None,
        "data": json.dumps(data),
    }


def test_two_zones_yields_out_last_zone() -> None:
    d = derive_direction([_event(zones=["front_garden", "shed"])])
    assert d.first_zone == "front_garden"
    assert d.last_zone == "shed"
    assert d.direction == "out:shed"
    assert d.source == "zones"


def test_single_zone_falls_through_to_path_heading() -> None:
    path = [[0.1, 0.5, 0.0], [0.3, 0.5, 1.0], [0.5, 0.5, 2.0], [0.7, 0.5, 3.0]]
    d = derive_direction([_event(zones=["front_garden"], path_data=path)])
    assert d.first_zone == "front_garden"
    assert d.last_zone == "front_garden"
    assert d.source == "path"
    assert d.direction == "l2r"
    assert d.heading_deg is not None


def test_too_few_path_points_falls_to_box() -> None:
    ev1 = _event(box=[0.1, 0.1, 0.2, 0.2], path_data=[[0.1, 0.1, 0.0]])
    ev2 = _event(box=[0.6, 0.1, 0.7, 0.2], path_data=[[0.6, 0.1, 1.0]])
    d = derive_direction([ev1, ev2])
    assert d.source == "box"
    assert d.direction == "l2r"


def test_box_area_growth_prefers_toward() -> None:
    ev1 = _event(box=[0.4, 0.4, 0.5, 0.5])  # small box, area .01
    ev2 = _event(box=[0.35, 0.35, 0.65, 0.65])  # much bigger, area .09, centroid barely moves
    d = derive_direction([ev1, ev2])
    assert d.source == "box"
    assert d.direction == "toward"


def test_no_event_rows_yields_empty_source_not_none() -> None:
    """No rows at all (nothing to look at) stays the empty '' source -- not
    yet attempted, or the lookup found nothing -- so it stays eligible for
    a later backfill retry."""
    d = derive_direction([])
    assert d.first_zone == ""
    assert d.last_zone == ""
    assert d.direction == ""
    assert d.heading_deg is None
    assert d.source == ""


def test_event_row_with_nothing_derivable_yields_dir_source_none() -> None:
    """We had a real event row (Frigate answered) but none of the three
    tiers could derive anything from it -- attempted and empty, marked
    `none` so the backfill CLI doesn't rewalk it forever."""
    d2 = derive_direction([_event()])
    assert d2.direction == ""
    assert d2.source == DIR_SOURCE_NONE


def test_malformed_json_never_raises() -> None:
    bad_rows = [
        {"id": "e1", "zones": "{not json", "data": "also not json"},
        {"id": "e2", "zones": None, "data": None},
        {},
    ]
    d = derive_direction(bad_rows)
    assert d.direction == ""
    assert d.source == DIR_SOURCE_NONE


def test_load_direction_no_matching_events_keeps_empty_source() -> None:
    """`load_direction` against a Frigate connection that simply has no
    matching `event` rows for the given ids must stay '' (retryable), never
    `none` -- `none` is reserved for rows Frigate *did* return."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE event (id TEXT, zones TEXT, data TEXT, start_time REAL)")
    conn.commit()
    d = load_direction(conn, ["missing-event-id"])
    assert d.source == ""


def test_load_direction_found_rows_nothing_derivable_yields_none() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE event (id TEXT, zones TEXT, data TEXT, start_time REAL)")
    conn.execute(
        "INSERT INTO event (id, zones, data, start_time) VALUES (?, ?, ?, ?)",
        ("ev1", None, "{}", 0.0),
    )
    conn.commit()
    d = load_direction(conn, ["ev1"])
    assert d.source == DIR_SOURCE_NONE


def test_backfill_rewalk_converges_after_one_pass(tmp_path) -> None:
    """`fsc encounters backfill-direction`'s rewalk set is
    `store.members_missing_direction` (`dir_source == ''`). A row whose
    Frigate events exist but yield nothing derivable must be written back
    with `dir_source="none"`, not `''`, so it drops out of that set and the
    backfill converges instead of rewalking it forever."""
    import time

    from marcellus import db
    from marcellus.encounters import store
    from marcellus.encounters.linker import Atom, LinkDecision

    sidecar_path = tmp_path / "sidecar.db"
    frigate_path = tmp_path / "frigate.db"

    frigate_conn = sqlite3.connect(frigate_path)
    frigate_conn.execute("CREATE TABLE event (id TEXT, zones TEXT, data TEXT, start_time REAL)")
    frigate_conn.execute(
        "INSERT INTO event (id, zones, data, start_time) VALUES (?, ?, ?, ?)",
        ("ev-undeterminable", None, "{}", 0.0),
    )
    frigate_conn.commit()
    frigate_conn.close()

    sidecar_conn = db.open_sidecar(sidecar_path)
    now = time.time()
    atom = Atom(
        atom_id="a1",
        camera="alley-wide",
        start_time=now - 100,
        end_time=now - 90,
        labels=("person",),
        zones=(),
        event_ids=("ev-undeterminable",),
        sub_labels=(),
        severity="alert",
    )
    store.upsert_atom(sidecar_conn, atom, LinkDecision(None, "new", 1.0), now)
    sidecar_conn.commit()

    def _rewalk_once() -> int:
        frigate_conn = db.open_frigate_ro(frigate_path)
        try:
            rows = store.members_missing_direction(sidecar_conn, 500)
            for row in rows:
                event_ids = json.loads(row["event_ids_json"] or "[]")
                direction = load_direction(frigate_conn, event_ids)
                store.set_direction(sidecar_conn, row["atom_id"], direction, commit=False)
            sidecar_conn.commit()
            return len(rows)
        finally:
            frigate_conn.close()

    first_pass = _rewalk_once()
    assert first_pass == 1
    row = store.observation(sidecar_conn, "a1")
    assert row is not None
    assert row["dir_source"] == DIR_SOURCE_NONE

    second_pass = _rewalk_once()
    assert second_pass == 0
    sidecar_conn.close()
