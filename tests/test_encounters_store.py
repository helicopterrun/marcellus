"""encounters/store.py: schema, upsert idempotency, aggregates, sealing,
watermark (docs/encounters.md "Tests" section)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from marcellus import db
from marcellus.encounters import store
from marcellus.encounters.linker import Atom, LinkDecision, LinkerConfig

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)


def _atom(atom_id: str, camera: str = "alley-wide", start: float = 0.0, **kw: object) -> Atom:
    defaults: dict[str, object] = dict(
        end_time=start + 10.0,
        labels=("person",),
        zones=(),
        event_ids=(f"ev-{atom_id}",),
        sub_labels=(),
        severity="detection",
    )
    defaults.update(kw)
    return Atom(atom_id=atom_id, camera=camera, start_time=start, **defaults)  # type: ignore[arg-type]


def test_schema_applies(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        for table in ("encounters", "encounter_members", "encounter_decisions", "encounter_state"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert cols, f"{table} missing"
    finally:
        conn.close()


def test_upsert_twice_updates_not_duplicates(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        atom = _atom("a1", start=now, end_time=None)
        decision = LinkDecision(None, "new", 1.0)
        enc_id = store.upsert_atom(conn, atom, decision, now)

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1

        updated_atom = _atom("a1", start=now, end_time=now + 15.0)
        again_decision = LinkDecision(enc_id, "same_camera", 0.9)
        enc_id2 = store.upsert_atom(conn, updated_atom, again_decision, now + 20.0)
        assert enc_id2 == enc_id

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
        row = conn.execute("SELECT end_time FROM encounter_members WHERE atom_id = 'a1'").fetchone()
        assert row["end_time"] == now + 15.0
        enc_row = store.get(conn, enc_id)
        assert enc_row is not None
        assert enc_row["end_time"] == now + 15.0
    finally:
        conn.close()


def test_recompute_aggregates(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", camera="alley-wide", start=now, labels=("person",)),
            LinkDecision(None, "new", 1.0),
            now,
        )
        store.upsert_atom(
            conn,
            _atom(
                "a2",
                camera="shed",
                start=now + 10,
                labels=("person", "dog"),
                sub_labels=("rex",),
                severity="alert",
            ),
            LinkDecision(enc_id, "adjacent", 0.6),
            now + 10,
        )
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["atom_count"] == 2
        assert row["peak_severity"] == "alert"
        import json

        assert json.loads(row["cameras_json"]) == ["alley-wide", "shed"]
        assert set(json.loads(row["labels_json"])) == {"person", "dog"}
        assert set(json.loads(row["identities_json"])) == {"rex"}
    finally:
        conn.close()


def test_seal_stale_seals_quiet_encounters(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 10000, end_time=now - 9990),
            LinkDecision(None, "new", 1.0),
            now - 10000,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 1
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is not None
        # sealed encounters are not returned by load_open
        assert enc_id not in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_leaves_fresh_encounters_open(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 5, end_time=now - 1),
            LinkDecision(None, "new", 1.0),
            now - 5,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 0
        assert enc_id in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_never_seals_an_open_member_below_max_duration(
    sidecar_db_path: Path,
) -> None:
    """A member with end_time IS NULL is still active. Before the fix,
    seal_stale's `COALESCE(m.end_time, m.start_time)` treated it as having
    ended at its own start -- so an encounter whose only member started 500s
    ago (well under max_duration_s=1800) but never got an "end" message
    (still genuinely live) looked "quiet" for 500s -- past the 270s
    (1.5x the largest 180s gap) staleness threshold -- and got sealed out
    from under the linker. It must stay open as long as its span hasn't hit
    max_duration_s."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - 500, end_time=None),  # still open, no end message
            LinkDecision(None, "new", 1.0),
            now - 500,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 0
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is None
        assert enc_id in {e.encounter_id for e in store.load_open(conn, now)}
    finally:
        conn.close()


def test_seal_stale_still_seals_an_open_member_past_max_duration(
    sidecar_db_path: Path,
) -> None:
    """An open member is not exempt from the hard `max_duration_s` cap --
    only from the "gone quiet" rule."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now - CFG.max_duration_s - 100, end_time=None),
            LinkDecision(None, "new", 1.0),
            now - CFG.max_duration_s - 100,
        )
        sealed = store.seal_stale(conn, now, CFG)
        assert sealed == 1
        row = store.get(conn, enc_id)
        assert row is not None
        assert row["sealed_at"] is not None
    finally:
        conn.close()


def test_grouped_atom_is_not_rehomed_by_a_later_update(sidecar_db_path: Path) -> None:
    """A founder re-home only ever applies to a *lone* founder. An atom
    already sharing an encounter with another member must never be moved,
    even if a later decision names a different encounter."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        # a2 joins a1's encounter -- now enc_a has 2 members, so a1 is no
        # longer a lone founder even though its own link_reason is "new".
        store.upsert_atom(
            conn,
            _atom("a2", start=now + 1),
            LinkDecision(enc_a, "companion", 0.7),
            now + 1,
        )
        enc_b = store.upsert_atom(
            conn, _atom("b1", camera="shed", start=now), LinkDecision(None, "new", 1.0), now
        )

        # A later message re-decides a1 as belonging to enc_b -- must be
        # ignored: enc_a has 2 members.
        moved = store.upsert_atom(
            conn,
            _atom("a1", start=now, end_time=now + 20),
            LinkDecision(enc_b, "companion", 0.7),
            now + 20,
        )
        assert moved == enc_a
        row = conn.execute(
            "SELECT encounter_id FROM encounter_members WHERE atom_id = 'a1'"
        ).fetchone()
        assert row["encounter_id"] == enc_a
    finally:
        conn.close()


def test_sealed_rehome_recomputes_surviving_donor(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        donor = store.upsert_atom(
            conn,
            _atom("a1", camera="alley-wide", start=now, end_time=now + 5),
            LinkDecision(None, "new", 1.0),
            now,
        )
        store.upsert_atom(
            conn,
            _atom("a2", camera="shed", start=now + 1, end_time=now + 6),
            LinkDecision(donor, "companion", 0.7),
            now + 1,
        )
        conn.execute("UPDATE encounters SET sealed_at = ? WHERE id = ?", (now + 100, donor))
        conn.commit()

        dest = store.upsert_atom(
            conn, _atom("b1", camera="deck", start=now), LinkDecision(None, "new", 1.0), now
        )
        moved = store.upsert_atom(
            conn,
            _atom("a1", camera="alley-wide", start=now, end_time=now + 5),
            LinkDecision(dest, "companion", 0.7),
            now + 200,
        )
        assert moved == dest

        donor_row = store.get(conn, donor)
        assert donor_row is not None
        assert donor_row["atom_count"] == 1
        assert json.loads(donor_row["cameras_json"]) == ["shed"]
        assert donor_row["end_time"] == now + 6
    finally:
        conn.close()


def test_sealed_rehome_deletes_donor_left_with_no_members(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        donor = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        conn.execute("UPDATE encounters SET sealed_at = ? WHERE id = ?", (now + 100, donor))
        conn.commit()

        dest = store.upsert_atom(
            conn, _atom("b1", camera="deck", start=now), LinkDecision(None, "new", 1.0), now
        )
        store.upsert_atom(
            conn, _atom("a1", start=now), LinkDecision(dest, "companion", 0.7), now + 200
        )

        assert store.get(conn, donor) is None
    finally:
        conn.close()


def test_update_after_end_keeps_end_time(sidecar_db_path: Path) -> None:
    """An "update" message arriving after "end" carries `end_time=None`
    (Frigate only ever sets it on the "end" message) -- it must not blank
    out the previously recorded end_time, or the encounter would look open
    again."""
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn,
            _atom("a1", start=now, end_time=now + 5),  # "end" message
            LinkDecision(None, "new", 1.0),
            now,
        )
        store.upsert_atom(
            conn,
            _atom("a1", start=now, end_time=None),  # stray "update" after "end"
            LinkDecision(enc_id, "new", 1.0),
            now + 10,
        )
        row = conn.execute("SELECT end_time FROM encounter_members WHERE atom_id = 'a1'").fetchone()
        assert row["end_time"] == now + 5
        enc_row = store.get(conn, enc_id)
        assert enc_row is not None
        assert enc_row["end_time"] == now + 5
    finally:
        conn.close()


def test_prune_drops_old_sealed_encounters(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        old_start = now - 40 * 86400
        old_sealed = store.upsert_atom(
            conn, _atom("old", start=old_start), LinkDecision(None, "new", 1.0), old_start
        )
        conn.execute(
            "UPDATE encounters SET sealed_at = ? WHERE id = ?",
            (now - 40 * 86400, old_sealed),
        )
        conn.execute(
            "INSERT INTO encounter_decisions (atom_id, action, encounter_id, created_at) "
            "VALUES ('old', 'pin', ?, 'x')",
            (old_sealed,),
        )
        recent_sealed = store.upsert_atom(
            conn, _atom("recent", start=now - 10), LinkDecision(None, "new", 1.0), now - 10
        )
        conn.execute("UPDATE encounters SET sealed_at = ? WHERE id = ?", (now - 10, recent_sealed))
        unsealed = store.upsert_atom(
            conn, _atom("open", start=now - 100000), LinkDecision(None, "new", 1.0), now - 100000
        )
        conn.commit()

        result = store.prune(conn, now, retention_days=30)
        assert result == {"encounters": 1, "members": 1, "decisions": 1}

        assert store.get(conn, old_sealed) is None
        assert store.get(conn, recent_sealed) is not None
        assert store.get(conn, unsealed) is not None
    finally:
        conn.close()


def test_watermark_roundtrip(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        assert store.get_watermark(conn) is None
        store.set_watermark(conn, 12345.5)
        assert store.get_watermark(conn) == 12345.5
        store.set_watermark(conn, 12400.0)
        assert store.get_watermark(conn) == 12400.0
    finally:
        conn.close()


def test_split_atom_moves_to_fresh_encounter_and_donor_recomputes(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now
        )
        store.upsert_atom(
            conn, _atom("a2", start=now + 5), LinkDecision(enc_id, "same_camera", 0.9), now
        )

        new_id = store.split_atom(conn, "a1", now + 10)
        assert new_id != enc_id

        row = store.member_row(conn, "a1")
        assert row is not None
        assert row["encounter_id"] == new_id
        assert row["link_reason"] == "split"

        # donor still exists (a2 remains) and its aggregates recomputed
        donor = store.get(conn, enc_id)
        assert donor is not None
        assert donor["atom_count"] == 1

        decisions = store.decisions_for(conn, "a1")
        assert any(d["action"] == "split" and d["encounter_id"] == enc_id for d in decisions)
    finally:
        conn.close()


def test_split_atom_deletes_donor_when_empty(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = store.upsert_atom(
            conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now
        )
        store.split_atom(conn, "a1", now + 10)
        assert store.get(conn, enc_id) is None
    finally:
        conn.close()


def test_pin_atom_moves_and_records_decision(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        enc_b = store.upsert_atom(
            conn, _atom("a2", start=now + 100), LinkDecision(None, "new", 1.0), now
        )

        result = store.pin_atom(conn, "a2", enc_a, now + 10)
        assert result == enc_a

        row = store.member_row(conn, "a2")
        assert row is not None
        assert row["encounter_id"] == enc_a
        assert row["link_reason"] == "pinned"

        assert store.get(conn, enc_b) is None  # donor emptied out

        decisions = store.decisions_for(conn, "a2")
        assert any(d["action"] == "pin" and d["encounter_id"] == enc_a for d in decisions)
    finally:
        conn.close()


def test_pin_into_sealed_target_keeps_it_sealed(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        conn.execute("UPDATE encounters SET sealed_at = ? WHERE id = ?", (now, enc_a))
        enc_b = store.upsert_atom(
            conn, _atom("a2", start=now + 100), LinkDecision(None, "new", 1.0), now
        )

        store.pin_atom(conn, "a2", enc_a, now + 10)
        row = store.get(conn, enc_a)
        assert row is not None
        assert row["sealed_at"] is not None
        assert store.get(conn, enc_b) is None
    finally:
        conn.close()


def test_pin_atom_unknown_target_raises(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        try:
            store.pin_atom(conn, "a1", "no-such-encounter", now)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        conn.close()


def test_merge_encounters_folds_all_members_and_deletes_source(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        enc_b = store.upsert_atom(
            conn, _atom("b1", start=now + 100), LinkDecision(None, "new", 1.0), now
        )
        store.upsert_atom(
            conn, _atom("b2", start=now + 110), LinkDecision(enc_b, "same_camera", 0.9), now
        )

        count = store.merge_encounters(conn, enc_b, enc_a, now + 10)
        assert count == 2
        assert store.get(conn, enc_b) is None

        for atom_id in ("b1", "b2"):
            row = store.member_row(conn, atom_id)
            assert row is not None
            assert row["encounter_id"] == enc_a
            assert row["link_reason"] == "pinned"

        target = store.get(conn, enc_a)
        assert target is not None
        assert target["atom_count"] == 3
    finally:
        conn.close()


def test_founder_singleton_returns_none_for_split_or_pinned(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        new_id = store.split_atom(conn, "a1", now + 10)
        assert store.founder_singleton(conn, "a1") is None

        enc_b = store.upsert_atom(
            conn, _atom("b1", start=now + 200), LinkDecision(None, "new", 1.0), now
        )
        store.pin_atom(conn, "b1", new_id, now + 20)
        assert store.founder_singleton(conn, "b1") is None
        assert enc_a or enc_b  # silence unused warnings if branches change
    finally:
        conn.close()


def test_upsert_atom_never_moves_pinned_or_split_atom(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_a = store.upsert_atom(conn, _atom("a1", start=now), LinkDecision(None, "new", 1.0), now)
        enc_b = store.upsert_atom(
            conn, _atom("b1", start=now + 200), LinkDecision(None, "new", 1.0), now
        )
        store.pin_atom(conn, "a1", enc_b, now + 5)  # a1 now pinned into enc_b

        # seal enc_b so a sealed-donor rehome would normally trigger
        conn.execute("UPDATE encounters SET sealed_at = ? WHERE id = ?", (now, enc_b))

        # decide() names a different encounter (enc_a) for a1 -- must be ignored
        decision = LinkDecision(enc_a, "same_camera", 0.95)
        result_id = store.upsert_atom(
            conn, _atom("a1", start=now, end_time=now + 30), decision, now + 40
        )
        assert result_id == enc_b  # unchanged despite decision naming enc_a

        row = store.member_row(conn, "a1")
        assert row is not None
        assert row["encounter_id"] == enc_b
        assert row["link_reason"] == "pinned"
    finally:
        conn.close()


def test_upsert_persists_direction(sidecar_db_path: Path) -> None:
    from marcellus.encounters.observations import Direction

    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        atom = _atom("d1", start=now, end_time=None)
        direction = Direction("front_garden", "shed", "out:shed", None, "zones")
        store.upsert_atom(conn, atom, LinkDecision(None, "new", 1.0), now, direction=direction)

        row = store.observation(conn, "d1")
        assert row is not None
        assert row["first_zone"] == "front_garden"
        assert row["last_zone"] == "shed"
        assert row["direction"] == "out:shed"
        assert row["heading_deg"] is None
        assert row["dir_source"] == "zones"
    finally:
        conn.close()


def test_upsert_with_no_direction_defaults_empty(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        atom = _atom("d2", start=now, end_time=None)
        store.upsert_atom(conn, atom, LinkDecision(None, "new", 1.0), now)

        row = store.observation(conn, "d2")
        assert row is not None
        assert row["dir_source"] == ""
        assert row["direction"] == ""
        assert row["heading_deg"] is None
    finally:
        conn.close()


def test_upsert_missing_event_rows_still_works(sidecar_db_path: Path) -> None:
    """`upsert_atom` must never fail just because direction couldn't be
    derived (e.g. the atom's Frigate event rows are gone)."""
    from marcellus.encounters.observations import derive_direction

    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        atom = _atom("d3", start=now, end_time=None, event_ids=("missing-ev",))
        direction = derive_direction([])  # simulates "no matching event rows"
        enc_id = store.upsert_atom(
            conn, atom, LinkDecision(None, "new", 1.0), now, direction=direction
        )
        assert enc_id
        row = store.observation(conn, "d3")
        assert row is not None
        assert row["dir_source"] == ""
    finally:
        conn.close()
