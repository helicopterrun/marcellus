"""sqlite persistence for cards (`push/card_store.py`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from marcellus import db
from marcellus.push import card_store
from marcellus.push.cards import Card


def open_conn(sidecar_db_path: Path) -> sqlite3.Connection:
    return db.open_sidecar(sidecar_db_path)


def test_get_missing_card_is_none(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    assert card_store.get_card(conn, "front:stranger:1") is None


def test_upsert_then_get_round_trips(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    card = Card(
        card_key="front:stranger:1", level="notify",
        created_at=100.0, updated_at=100.0, state_since_at=100.0, sound_count=1,
    )
    card_store.upsert_card(
        conn, card, subject_kind="stranger", place_class="doors",
        camera="front", zone_name="doors",
    )
    fetched = card_store.get_card(conn, "front:stranger:1")
    assert fetched is not None
    assert fetched.level == "notify"
    assert fetched.sound_count == 1


def test_upsert_overwrites_in_place(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    card = Card(card_key="k1", level="quiet", created_at=1.0, updated_at=1.0, state_since_at=1.0)
    card_store.upsert_card(conn, card)
    card = Card(
        card_key="k1", level="urgent", created_at=1.0, updated_at=5.0, state_since_at=5.0,
        sound_count=2,
    )
    card_store.upsert_card(conn, card)
    fetched = card_store.get_card(conn, "k1")
    assert fetched.level == "urgent"
    assert fetched.sound_count == 2


def test_mark_handled(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    card = Card(card_key="k2", level="urgent", created_at=1.0, updated_at=1.0, state_since_at=1.0)
    card_store.upsert_card(conn, card)
    card_store.mark_handled(conn, "k2", now=42.0)
    fetched = card_store.get_card(conn, "k2")
    assert fetched.handled is True
    assert fetched.handled_at == 42.0


def test_list_open_urgent_cards_excludes_closed_and_other_levels(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    open_urgent = Card(
        card_key="u1", level="urgent", created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    closed_urgent = Card(
        card_key="u2", level="urgent", created_at=1.0, updated_at=1.0, state_since_at=1.0,
        closed=True,
    )
    notify = Card(card_key="n1", level="notify", created_at=1.0, updated_at=1.0, state_since_at=1.0)
    for c in (open_urgent, closed_urgent, notify):
        card_store.upsert_card(conn, c)
    keys = {c.card_key for c, _context in card_store.list_open_urgent_cards(conn)}
    assert keys == {"u1"}


def test_close_stale_cards_closes_only_idle_open_cards(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    stale = Card(
        card_key="stale1", level="notify", created_at=1.0, updated_at=100.0, state_since_at=1.0,
    )
    fresh = Card(
        card_key="fresh1", level="notify", created_at=1.0, updated_at=900.0, state_since_at=1.0,
    )
    already_closed = Card(
        card_key="closed1", level="notify", created_at=1.0, updated_at=100.0,
        state_since_at=1.0, closed=True, resolved=True,
    )
    for c in (stale, fresh, already_closed):
        card_store.upsert_card(conn, c)

    n = card_store.close_stale_cards(conn, idle_for=600.0, now=1000.0)

    assert n == 1
    closed_fetched = card_store.get_card(conn, "stale1")
    assert closed_fetched.closed is True
    assert closed_fetched.resolved is True
    assert closed_fetched.updated_at == 1000.0

    fresh_fetched = card_store.get_card(conn, "fresh1")
    assert fresh_fetched.closed is False
    assert fresh_fetched.updated_at == 900.0

    # Already-closed card is untouched (updated_at unchanged).
    untouched = card_store.get_card(conn, "closed1")
    assert untouched.updated_at == 100.0


def test_migrate_drop_zone_collapses_split_rows_onto_the_newest(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    older = Card(
        card_key="alley-wide:_:thing:trk1", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    newer = Card(
        card_key="alley-wide:parking_spot:thing:trk1", level="quiet",
        created_at=5.0, updated_at=5.0, state_since_at=5.0, sound_count=0,
    )
    card_store.upsert_card(conn, older, camera="alley-wide", zone_name="")
    card_store.upsert_card(conn, newer, camera="alley-wide", zone_name="parking_spot")

    collapsed = card_store.migrate_drop_zone_from_card_keys(conn)
    assert collapsed == 2  # both old-format rows are marked stale

    rows = conn.execute("SELECT * FROM push_cards").fetchall()
    by_key = {r["card_key"]: r for r in rows}
    assert "alley-wide:thing:trk1" in by_key
    survivor = by_key["alley-wide:thing:trk1"]
    assert survivor["level"] == "quiet"  # the newer row's state won
    assert survivor["resolved"] == 0
    assert survivor["closed"] == 0

    assert by_key["alley-wide:_:thing:trk1"]["closed"] == 1
    assert by_key["alley-wide:_:thing:trk1"]["resolved"] == 1
    assert by_key["alley-wide:parking_spot:thing:trk1"]["closed"] == 1
    assert by_key["alley-wide:parking_spot:thing:trk1"]["resolved"] == 1

    # Running it again (e.g. on every service restart, as the real deployment
    # does) must not re-"collapse" the same now-closed rows forever -- that
    # was a real bug: old-format rows are never deleted, only closed, so a
    # naive structural check kept finding and re-processing them every time.
    assert card_store.migrate_drop_zone_from_card_keys(conn) == 0


def test_migrate_drop_zone_is_idempotent_and_ignores_system_cards(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    system_card = Card(
        card_key="front:system:offline", level="notify",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    already_fixed = Card(
        card_key="front:stranger:trk9", level="notify",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    card_store.upsert_card(conn, system_card)
    card_store.upsert_card(conn, already_fixed)

    assert card_store.migrate_drop_zone_from_card_keys(conn) == 0
    assert card_store.get_card(conn, "front:system:offline").closed is False
    assert card_store.get_card(conn, "front:stranger:trk9").closed is False

    # Running it again (e.g. every startup) touches nothing further.
    assert card_store.migrate_drop_zone_from_card_keys(conn) == 0


def test_find_dedup_candidate_matches_subject_kind_and_zone_within_window(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    card = Card(card_key="cam-a:stranger:trk1", level="quiet", created_at=0.0, updated_at=0.0,
                state_since_at=0.0)
    card_store.upsert_card(conn, card, subject_kind="stranger", camera="cam-a",
                            zone_name="driveway")

    hit = card_store.find_dedup_candidate(
        conn, subject_kind="stranger", zone_name="driveway",
        exclude_key="cam-b:stranger:trk2", now=5.0, window_s=15.0,
    )
    assert hit == "cam-a:stranger:trk1"

    # Outside the window: no match.
    assert card_store.find_dedup_candidate(
        conn, subject_kind="stranger", zone_name="driveway",
        exclude_key="cam-b:stranger:trk2", now=20.0, window_s=15.0,
    ) is None

    # Different zone or subject_kind: no match.
    assert card_store.find_dedup_candidate(
        conn, subject_kind="stranger", zone_name="back_yard",
        exclude_key="cam-b:stranger:trk2", now=5.0, window_s=15.0,
    ) is None
    assert card_store.find_dedup_candidate(
        conn, subject_kind="thing", zone_name="driveway",
        exclude_key="cam-b:thing:trk2", now=5.0, window_s=15.0,
    ) is None


def test_find_dedup_candidate_matches_on_zone_set_intersection(sidecar_db_path: Path):
    """The exact live miss (2026-08-14): stairway-wide's review listed
    ['back_walkway', 'driveway'] (card zone_name = back_walkway), alley-wide's
    listed ['driveway'] — same walk, no first-zone match, two lock-screen
    rows. Intersection on the full zone set must merge them."""
    conn = open_conn(sidecar_db_path)
    card = Card(card_key="stairway-wide:person:trkA", level="quiet",
                created_at=0.0, updated_at=0.0, state_since_at=0.0)
    card_store.upsert_card(
        conn, card, subject_kind="person", camera="stairway-wide",
        zone_name="back_walkway", zones=("back_walkway", "driveway"),
    )

    hit = card_store.find_dedup_candidate(
        conn, subject_kind="person", zone_name="driveway",
        exclude_key="alley-wide:person:trkB", now=5.0, window_s=15.0,
        zones=("driveway",),
    )
    assert hit == "stairway-wide:person:trkA"

    # Reverse direction: candidate stored only "driveway"; the new event's
    # first zone differs but its set includes it.
    card2 = Card(card_key="alley-wide:person:trkC", level="quiet",
                 created_at=6.0, updated_at=6.0, state_since_at=6.0)
    card_store.upsert_card(
        conn, card2, subject_kind="person", camera="alley-wide",
        zone_name="driveway", zones=("driveway",),
    )
    hit = card_store.find_dedup_candidate(
        conn, subject_kind="person", zone_name="parking_spot",
        exclude_key="stairway-tight:person:trkD", now=8.0, window_s=15.0,
        zones=("parking_spot", "driveway"),
    )
    assert hit == "stairway-wide:person:trkA"  # oldest open still wins

    # Disjoint zone sets never merge.
    assert card_store.find_dedup_candidate(
        conn, subject_kind="person", zone_name="front_garden",
        exclude_key="garden:person:trkE", now=8.0, window_s=15.0,
        zones=("front_garden",),
    ) is None


def test_find_dedup_candidate_ignores_closed_cards(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    card = Card(card_key="cam-a:stranger:trk1", level="quiet", created_at=0.0, updated_at=0.0,
                state_since_at=0.0, closed=True, resolved=True)
    card_store.upsert_card(conn, card, subject_kind="stranger", camera="cam-a",
                            zone_name="driveway")
    assert card_store.find_dedup_candidate(
        conn, subject_kind="stranger", zone_name="driveway",
        exclude_key="cam-b:stranger:trk2", now=5.0, window_s=15.0,
    ) is None


def test_track_alias_round_trips_and_deletes(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    assert card_store.get_track_alias(conn, "cam-b", "trk2") is None
    card_store.set_track_alias(conn, "cam-b", "trk2", "cam-a:stranger:trk1", now=5.0)
    assert card_store.get_track_alias(conn, "cam-b", "trk2") == "cam-a:stranger:trk1"
    card_store.delete_track_alias(conn, "cam-b", "trk2")
    assert card_store.get_track_alias(conn, "cam-b", "trk2") is None


def test_migrate_drop_zone_handles_a_single_unsplit_row(sidecar_db_path: Path):
    conn = open_conn(sidecar_db_path)
    lone = Card(
        card_key="street:_:thing:trk2", level="log",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    card_store.upsert_card(conn, lone, camera="street")

    assert card_store.migrate_drop_zone_from_card_keys(conn) == 1
    fetched = card_store.get_card(conn, "street:thing:trk2")
    assert fetched is not None
    assert fetched.level == "log"
    assert card_store.get_card(conn, "street:_:thing:trk2").closed is True
