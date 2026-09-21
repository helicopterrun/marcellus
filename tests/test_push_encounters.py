"""Encounter-aware push (docs/push-notifications.md "Encounter-aware push").

Three layers, tested separately:

* `EncounterService.link_now` -- the synchronous link the push path awaits,
  and its idempotency with the queued worker.
* `PushEngine.handle_event`'s timeout/failure fallback.
* `handle_delivery_event` routing/copy/payload under
  `push.encounter_threading` / `push.encounter_merge`.

Camera names use underscores on purpose: the copy prettifier
(`delivery_wire._pretty_camera`) only turns `_` into a space before
title-casing, so `alley_wide` reads as "Alley Wide" while a hyphenated
`cam-b` stays "Cam-B". These assertions check the real output, not an
idealized one.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection, Settings
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.service import EncounterService
from marcellus.push import card_store, live_activities
from marcellus.push.delivery_wire import handle_delivery_event, handle_delivery_resolve
from marcellus.push.engine import PushEngine
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport
from tests.test_scrub import REVIEWSEGMENT_SCHEMA


def make_device(token: str = "tok1") -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity="detection",
    )


def make_event(
    camera: str,
    track_id: str,
    *,
    zones: tuple[str, ...] = ("front_door",),
    labels: tuple[str, ...] = ("person",),
) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=labels, track_ids=(track_id,), zones=zones,
    )


def _settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    conn.commit()
    conn.close()
    return Settings(
        frigate={
            "base_url": "http://frigate.test:5000",
            "config_path": cfg,
            "db_path": frigate_db,
        },
        sidecar={"db_path": tmp_path / "sidecar.db"},
    )


# --------------------------------------------------------------------------
# link_now
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_now_returns_encounter_id_and_is_idempotent(tmp_path: Path) -> None:
    """`link_now` names the encounter inline, and the queued worker running
    the same review afterwards lands on the same one (no second membership,
    no re-home)."""
    settings = _settings(tmp_path)
    clock = [100.0]
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: clock[0]
    )
    ev = ReviewEvent(
        review_id="r1", camera="alley_wide", severity="alert", labels=("person",),
        msg_type="new", track_ids=("ev1",), start_time=100.0,
    )

    encounter_id = await asyncio.to_thread(service.link_now, ev)
    assert encounter_id

    # Same review again through the *queued* path: idempotent.
    service.observe_review(ev)
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute("SELECT encounter_id FROM encounter_members").fetchall()
        assert len(rows) == 1
        assert rows[0]["encounter_id"] == encounter_id
    finally:
        conn.close()

    # And calling link_now a third time still reports the same encounter.
    assert await asyncio.to_thread(service.link_now, ev) == encounter_id


@pytest.mark.asyncio
async def test_link_now_after_worker_returns_the_workers_encounter(tmp_path: Path) -> None:
    """Reverse order: the worker links first, `link_now` must report that
    encounter rather than minting a second one."""
    settings = _settings(tmp_path)
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: 100.0
    )
    ev = ReviewEvent(
        review_id="r1", camera="alley_wide", severity="alert", labels=("person",),
        msg_type="new", track_ids=("ev1",), start_time=100.0,
    )
    service.observe_review(ev)
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        stored = conn.execute(
            "SELECT encounter_id FROM encounter_members WHERE atom_id = 'r1'"
        ).fetchone()["encounter_id"]
    finally:
        conn.close()

    assert await asyncio.to_thread(service.link_now, ev) == stored


@pytest.mark.asyncio
async def test_link_now_concurrent_worker_and_push_paths_agree(tmp_path: Path) -> None:
    """The actual PR #80 race: `_link_review` (live MQTT worker) and
    `link_now` (push delivery path) both run the same brand-new review on
    separate threads/connections at once. Before the `store.upsert_atom`
    `BEGIN IMMEDIATE` fix, the loser's INSERT could raise a UNIQUE
    constraint error, which `link_now` swallowed and turned into `None` --
    dropping the encounter id from the push payload. Both paths must agree
    on one encounter id and there must be exactly one membership row."""
    settings = _settings(tmp_path)
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: 100.0
    )
    ev = ReviewEvent(
        review_id="r1", camera="alley_wide", severity="alert", labels=("person",),
        msg_type="new", track_ids=("ev1",), start_time=100.0,
    )

    barrier = threading.Barrier(2)
    link_now_result: list[str | None] = [None]

    def run_worker() -> None:
        barrier.wait(timeout=5)
        service._link_review(ev)

    def run_push() -> None:
        barrier.wait(timeout=5)
        link_now_result[0] = service.link_now(ev)

    t1 = threading.Thread(target=run_worker)
    t2 = threading.Thread(target=run_push)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert link_now_result[0] is not None

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute(
            "SELECT encounter_id FROM encounter_members WHERE atom_id = 'r1'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["encounter_id"] == link_now_result[0]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_engine_link_timeout_yields_none_and_delivery_still_runs(
    sidecar_db_path: Path,
) -> None:
    """A link that blows the budget must cost the push its encounter id, not
    its existence."""
    transport = LogTransport()
    engine = PushEngine(
        db_path=str(sidecar_db_path), transport=transport, server_id="s_test",
        push_config=PushSection(
            delivery_enabled=True, encounter_link_timeout_s=0.01,
        ),
        encounter_link=lambda ev: (time.sleep(0.2), "enc-never")[1],
    )
    assert await engine._encounter_id_for(make_event("alley_wide", "t1"), now=0.0) is None

    sent = await engine.handle_event(make_event("alley_wide", "t1"))
    assert sent >= 0  # the pipeline ran; no exception escaped
    conn = db.open_sidecar(sidecar_db_path)
    try:
        row = conn.execute("SELECT encounter_id FROM push_cards").fetchone()
        assert row is not None
        assert row["encounter_id"] == ""
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_engine_link_exception_is_swallowed_and_logged_once(
    sidecar_db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(ev: ReviewEvent) -> str | None:
        raise RuntimeError("encounters down")

    engine = PushEngine(
        db_path=str(sidecar_db_path), transport=LogTransport(), server_id="s_test",
        push_config=PushSection(delivery_enabled=True),
        encounter_link=boom,
    )
    ev = make_event("alley_wide", "t1")
    with caplog.at_level(logging.WARNING, logger="marcellus.push.engine"):
        assert await engine._encounter_id_for(ev, now=0.0) is None
        # Second call inside the same minute: rate-limited to one log line.
        assert await engine._encounter_id_for(ev, now=1.0) is None
    warnings = [r for r in caplog.records if "encounter link unavailable" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_engine_skips_the_link_when_both_flags_are_off(
    sidecar_db_path: Path,
) -> None:
    calls: list[str] = []
    engine = PushEngine(
        db_path=str(sidecar_db_path), transport=LogTransport(), server_id="s_test",
        push_config=PushSection(
            delivery_enabled=True, encounter_threading=False, encounter_merge=False,
        ),
        encounter_link=lambda ev: calls.append(ev.review_id) or "enc-1",
    )
    assert await engine._encounter_id_for(make_event("alley_wide", "t1"), now=0.0) is None
    assert calls == []


# --------------------------------------------------------------------------
# Routing / copy
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_on_routes_second_camera_onto_the_first_card(
    sidecar_db_path: Path,
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True, encounter_merge=True)
    device = make_device()

    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[device],
        transport=transport, config=config, now=0.0, encounter_id="enc-1",
    )
    # Second camera, a disjoint zone so ordinary zone dedup cannot explain
    # the merge -- only the shared encounter can.
    await handle_delivery_event(
        make_event("stairway_wide", "trkB", zones=("side_path",)),
        conn=conn, devices=[device], transport=transport, config=config,
        now=3.0, encounter_id="enc-1",
    )

    rows = conn.execute("SELECT card_key, camera, encounter_id FROM push_cards").fetchall()
    assert len(rows) == 1, "one encounter is one card when encounter_merge is on"
    assert rows[0]["card_key"] == "alley_wide:person:trkA"
    assert rows[0]["camera"] == "alley_wide", "merged card keeps its originating camera"
    assert rows[0]["encounter_id"] == "enc-1"

    assert card_store.cameras_path(conn, "alley_wide:person:trkA") == [
        "alley_wide", "stairway_wide",
    ]

    alias = conn.execute(
        "SELECT card_key FROM push_card_track_aliases WHERE track_id = 'trkB'"
    ).fetchone()
    assert alias is not None and alias["card_key"] == "alley_wide:person:trkA"

    assert len(transport.sent) == 2
    first, second = transport.sent[0]["payload"], transport.sent[1]["payload"]
    assert first["mutation"] == "create"
    assert second["mutation"] == "enrich", "a camera crossing is an ENRICH, not a new kind"
    assert second["secondary"] == "Alley Wide → Stairway Wide"
    assert "also on" not in second["secondary"]
    assert second["cameras_path"] == ["alley_wide", "stairway_wide"]
    # One story, one collapse id.
    assert transport.sent[0]["collapse_id"] == transport.sent[1]["collapse_id"]


@pytest.mark.asyncio
async def test_merge_off_keeps_two_cards_but_threads_them(
    sidecar_db_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True, encounter_merge=False)
    device = make_device()

    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[device],
        transport=transport, config=config, now=0.0, encounter_id="enc-1",
    )
    with caplog.at_level(logging.DEBUG, logger="marcellus.push.delivery_wire"):
        await handle_delivery_event(
            make_event("stairway_wide", "trkB", zones=("side_path",)),
            conn=conn, devices=[device], transport=transport, config=config,
            now=3.0, encounter_id="enc-1",
        )

    keys = {r["card_key"] for r in conn.execute("SELECT card_key FROM push_cards")}
    assert keys == {"alley_wide:person:trkA", "stairway_wide:person:trkB"}
    assert any(
        r.getMessage()
        == "encounter_merge would have routed trkB onto alley_wide:person:trkA"
        for r in caplog.records
    )

    payloads = [s["payload"] for s in transport.sent]
    assert len(payloads) == 2
    assert {p["encounter_id"] for p in payloads} == {"enc-1"}
    assert {p["aps"]["thread-id"] for p in payloads} == {"enc-1"}
    # Separate cards keep their own copy -- no path, no "also on".
    assert all("→" not in p["secondary"] for p in payloads)


@pytest.mark.asyncio
async def test_threading_off_threads_by_camera(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(
        delivery_enabled=True, encounter_threading=False, encounter_merge=False,
    )
    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[make_device()],
        transport=transport, config=config, now=0.0, encounter_id="enc-1",
    )
    payload = transport.sent[0]["payload"]
    assert payload["aps"]["thread-id"] == "alley_wide"
    # The id is still reported -- the app can group on it itself.
    assert payload["encounter_id"] == "enc-1"


@pytest.mark.asyncio
async def test_no_encounter_id_payload_omits_the_keys(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[make_device()],
        transport=transport, config=PushSection(delivery_enabled=True), now=0.0,
    )
    payload = transport.sent[0]["payload"]
    assert "encounter_id" not in payload
    assert "cameras_path" not in payload
    assert payload["aps"]["thread-id"] == "alley_wide"


@pytest.mark.asyncio
async def test_family_gate_blocks_a_vehicle_joining_a_person_card(
    sidecar_db_path: Path,
) -> None:
    """A person and the car they arrived in share an encounter but must not
    share a notification."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True, encounter_merge=True)
    device = make_device()

    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[device],
        transport=transport, config=config, now=0.0, encounter_id="enc-1",
    )
    await handle_delivery_event(
        make_event("gate_walkway", "trkC", zones=("driveway",), labels=("car",)),
        conn=conn, devices=[device], transport=transport, config=config,
        now=3.0, encounter_id="enc-1",
    )

    keys = {r["card_key"] for r in conn.execute("SELECT card_key FROM push_cards")}
    assert keys == {"alley_wide:person:trkA", "gate_walkway:vehicle:trkC"}
    # Both still carry the encounter, so Notification Center groups them.
    stamped = {
        r["encounter_id"] for r in conn.execute("SELECT encounter_id FROM push_cards")
    }
    assert stamped == {"enc-1"}


@pytest.mark.asyncio
async def test_resolved_encounter_card_is_not_reopened(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    config = PushSection(delivery_enabled=True, encounter_merge=True)
    device = make_device()

    await handle_delivery_event(
        make_event("alley_wide", "trkA"), conn=conn, devices=[device],
        transport=transport, config=config, now=0.0, encounter_id="enc-1",
    )
    await handle_delivery_resolve(
        "alley_wide", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, now=5.0,
    )
    assert conn.execute(
        "SELECT resolved FROM push_cards WHERE card_key = 'alley_wide:person:trkA'"
    ).fetchone()["resolved"]

    await handle_delivery_event(
        make_event("stairway_wide", "trkB", zones=("side_path",)),
        conn=conn, devices=[device], transport=transport, config=config,
        now=8.0, encounter_id="enc-1",
    )
    rows = conn.execute("SELECT card_key, resolved, encounter_id FROM push_cards").fetchall()
    assert {r["card_key"] for r in rows} == {
        "alley_wide:person:trkA", "stairway_wide:person:trkB",
    }
    new_card = next(r for r in rows if r["card_key"] == "stairway_wide:person:trkB")
    assert not new_card["resolved"]
    assert new_card["encounter_id"] == "enc-1", "still grouped, just not reopened"
    old_card = next(r for r in rows if r["card_key"] == "alley_wide:person:trkA")
    assert old_card["resolved"], "the resolved card stays resolved"


# --------------------------------------------------------------------------
# Copy + content state
# --------------------------------------------------------------------------


def test_cameras_path_text_caps_at_four_with_an_ellipsis() -> None:
    from marcellus.push.delivery_wire import _cameras_path_text

    assert _cameras_path_text(["alley_wide"]) == "Alley Wide"
    assert _cameras_path_text(["alley_wide", "stairway_wide"]) == (
        "Alley Wide → Stairway Wide"
    )
    assert _cameras_path_text(["a", "b", "c", "d"]) == "A → B → C → D"
    assert _cameras_path_text(["a", "b", "c", "d", "e"]) == (
        "… B → C → D → E"
    )


def test_content_state_additive_fields_stay_off_the_wire_when_absent() -> None:
    base = dict(
        level="notify", mutation="update", glyph="person.person", primary="P",
        secondary="S", elapsed_seconds=3, card_key="c:person:t",
        thumbnail_handle=None, thumbnail_revision=1,
    )
    before = live_activities.build_content_state(**base)
    assert "cameras_path" not in before
    assert "encounter_id" not in before

    after = live_activities.build_content_state(
        **base, cameras_path=["alley_wide", "stairway_wide"], encounter_id="enc-1",
    )
    assert after["cameras_path"] == ["alley_wide", "stairway_wide"]
    assert after["encounter_id"] == "enc-1"
    # Every pre-existing key is byte-identical.
    assert {k: v for k, v in after.items()
            if k not in ("cameras_path", "encounter_id")} == before


def test_cameras_path_only_grows_for_encounter_cards(sidecar_db_path: Path) -> None:
    """An ordinary (encounter-less) card records no path, so the dedup
    " · also on X" copy is untouched by this feature."""
    from marcellus.push.cards import Card

    conn = db.open_sidecar(sidecar_db_path)
    try:
        card = Card(card_key="k1", level="notify", created_at=0.0, updated_at=0.0)
        card_store.upsert_card(conn, card, camera="alley_wide")
        assert card_store.cameras_path(conn, "k1") == []

        card_store.upsert_card(conn, card, camera="alley_wide", encounter_id="enc-1")
        card_store.upsert_card(
            conn, card, camera="alley_wide", event_camera="stairway_wide",
            encounter_id="enc-1",
        )
        # A repeat of the same camera adds nothing.
        card_store.upsert_card(
            conn, card, camera="alley_wide", event_camera="stairway_wide",
            encounter_id="enc-1",
        )
        assert card_store.cameras_path(conn, "k1") == ["alley_wide", "stairway_wide"]

        # Sticky: a later mutation whose link timed out must not blank it.
        card_store.upsert_card(conn, card, camera="alley_wide")
        row = conn.execute("SELECT encounter_id FROM push_cards WHERE card_key='k1'").fetchone()
        assert row["encounter_id"] == "enc-1"
    finally:
        conn.close()


def test_find_open_card_by_encounter_ignores_closed_cards(sidecar_db_path: Path) -> None:
    from marcellus.push.cards import Card

    conn = db.open_sidecar(sidecar_db_path)
    try:
        open_card = Card(card_key="open", level="notify", created_at=1.0, updated_at=1.0)
        done = Card(
            card_key="done", level="notify", created_at=0.0, updated_at=0.0,
            resolved=True, closed=True,
        )
        card_store.upsert_card(conn, done, camera="a", encounter_id="enc-1")
        card_store.upsert_card(conn, open_card, camera="b", encounter_id="enc-1")

        found = card_store.find_open_card_by_encounter(conn, "enc-1")
        assert found is not None and found.card_key == "open"
        assert card_store.find_open_card_by_encounter(conn, "enc-2") is None
        assert card_store.find_open_card_by_encounter(conn, "") is None
        assert card_store.find_open_card_by_encounter(
            conn, "enc-1", exclude_key="open"
        ) is None
    finally:
        conn.close()
