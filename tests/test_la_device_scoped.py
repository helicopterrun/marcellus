"""Device-scoped Live Activity aggregation tests (Elsinore Phase 4): one
activity per device, aggregating all open eligible "cards" (stories).

Reuses fixtures/helpers from `test_push_live_activities_wire.py` (same
directory) rather than redefining them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import store
from marcellus.push.delivery_wire import (
    end_activity_if_card_closed,
    handle_delivery_event,
    handle_delivery_resolve,
)
from marcellus.push.engine import PushEngine
from marcellus.push.transport import LogTransport
from tests.test_push_live_activities_wire import (
    _reset_ladder_table,  # noqa: F401  (autouse fixture)
    attach_token,
    find_activity_row,
    la_sends,
    make_device,
    make_event,
)


def _open_demo_activity(conn, *, device) -> None:
    """Simulate the app's Settings -> "Try" debug demo activity: a row the
    app uploads via `attach_activity_token` with its own real situation/track
    id, distinct from the sidecar's `store.DEVICE_SITUATION_ID` sentinel."""
    store.open_activity(
        conn, activity_id="demo-activity-1", apns_token=device.apns_token,
        situation_id="demo:person:test-001", track_id="demo-test-001",
        camera="", collapse_id="demo:person:test-001", handle="",
    )
    store.attach_activity_token(
        conn, activity_id="demo-activity-1", apns_token=device.apns_token,
        situation_id="demo:person:test-001", track_id="demo-test-001",
        token="demoPerActivityToken",
    )
    conn.commit()

#: Mirrors `test_push_delivery_wire.EXTERNAL_BASE_URL` -- needed alongside a
#: real `PushEngine` to actually mint a `media_handle` (`_media_for` mints
#: nothing without both).
EXTERNAL_BASE_URL = "http://192.168.50.207:5001"


@pytest.mark.asyncio
async def test_two_stories_one_activity(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # Story A: person at front_door -> notify, LA-eligible.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # Story B: a different track, well outside the cross-camera dedup
    # window, joins as a second eligible story on the same device activity.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # still only ONE start, ever

    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    join_update = updates[0]
    assert join_update["payload"]["aps"].get("alert") is not None
    content_state = join_update["payload"]["aps"]["content-state"]
    assert content_state["extra_stories"] == 1
    assert "camera" in content_state

    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row is not None
    assert row["ended_at"] is None
    # Only one open row for this device -- device-scoped, not per-card.
    rows = conn.execute(
        "SELECT COUNT(*) FROM push_activities WHERE apns_token = ? AND ended_at IS NULL",
        (device.apns_token,),
    ).fetchone()[0]
    assert rows == 1


@pytest.mark.asyncio
async def test_primary_flips_on_escalation(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # A: notify.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # B: joins at notify too.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    # Escalate B: move to off_limits -> urgent. Primary must flip to B.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )

    last_update = [r for r in la_sends(transport) if r["event"] == "update"][-1]
    content_state = last_update["payload"]["aps"]["content-state"]
    assert content_state["deep_link_card_key"] == "doorbell:person:trkB"
    assert content_state["level"] == "urgent"


@pytest.mark.asyncio
async def test_end_only_after_both_close(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    # Resolve A -- B is still open and eligible, so no `end` yet.
    resolved = await handle_delivery_resolve(
        "doorbell", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=40.0,
    )
    assert resolved == 1
    assert all(r["event"] != "end" for r in la_sends(transport))
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["ended_at"] is None

    # Resolve B -- nothing eligible remains -> end, row closes.
    resolved = await handle_delivery_resolve(
        "doorbell", "trkB", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=60.0,
    )
    assert resolved == 1
    ends = [r for r in la_sends(transport) if r["event"] == "end"]
    assert len(ends) == 1
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["ended_at"] is not None


@pytest.mark.asyncio
async def test_dismissal_quiet_period(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    store.dismiss_activity(conn, row["activity_id"], now=5.0)
    conn.commit()

    # New story joins while dismissed -- no restart.
    sent_before = len(la_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    assert len(la_sends(transport)) == sent_before

    # Escalate B (e.g. move to off_limits -> urgent) breaks through the
    # tombstone: it's cleared and a fresh start is sent.
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    assert store.find_dismissed_activity(conn, apns_token=device.apns_token) is None
    # A's original start, plus this fresh restart -- two starts total.
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 2


@pytest.mark.asyncio
async def test_tombstone_cleared_when_all_close(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    store.dismiss_activity(conn, row["activity_id"], now=5.0)
    conn.commit()

    resolved = await handle_delivery_resolve(
        "doorbell", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=20.0,
    )
    assert resolved == 1
    # Clean slate: the last open story closing clears the tombstone.
    assert store.find_dismissed_activity(conn, apns_token=device.apns_token) is None

    # A brand-new story after that gets a fresh start (the original story
    # A's own start plus this one -- two starts total, never a restart of
    # the tombstoned activity).
    await handle_delivery_event(
        make_event("doorbell", "trkC", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 2


@pytest.mark.asyncio
async def test_escalate_keeps_create_thumbnail_handle(sidecar_db_path: Path):
    """Regression test: an ESCALATE mints no fresh media (`_MEDIA_MUTATIONS`
    is create/enrich only), so before the sticky `media_handle` fix the
    content-state omitted `thumbnail_handle` entirely on escalation --
    blanking the widget's thumbnail mid-story. It must now carry the SAME
    handle the CREATE minted.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)

    # create: front_door -> notify, LA-eligible, mints media.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    create_handle = starts[0]["payload"]["aps"]["content-state"]["thumbnail_handle"]
    assert create_handle
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # escalate: pool -> off_limits/urgent for a person. Mints no media.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=10.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    escalate_state = updates[0]["payload"]["aps"]["content-state"]
    assert escalate_state["level"] == "urgent"
    assert escalate_state["thumbnail_handle"] == create_handle


@pytest.mark.asyncio
async def test_aggregate_uses_non_triggering_primarys_own_handle(sidecar_db_path: Path):
    """Two open stories where the non-triggering story is the aggregate
    PRIMARY: the emitted content-state's `thumbnail_handle` must be the
    PRIMARY's own persisted handle, not the triggering card's handle."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="s_test")
    config = PushSection(delivery_enabled=True, external_base_url=EXTERNAL_BASE_URL)

    # Story A: created at urgent (off_limits/person via "pool" zone) -- it
    # will stay the PRIMARY (highest level) through the rest of this test.
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    a_handle = starts[0]["payload"]["aps"]["content-state"]["thumbnail_handle"]
    assert a_handle
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    # Story B: a different, lower-level (notify) story joins and triggers
    # this mutation -- it is NOT the primary (A outranks it at urgent).
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config,
        engine=engine, now=20.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    content_state = updates[0]["payload"]["aps"]["content-state"]
    # A is still primary (urgent beats notify).
    assert content_state["deep_link_card_key"] == "doorbell:person:trkA"
    assert content_state["thumbnail_handle"] == a_handle


@pytest.mark.asyncio
async def test_card_with_no_media_ever_minted_omits_thumbnail_handle(sidecar_db_path: Path):
    """A card that never had any media minted (no engine/external_base_url)
    keeps omitting `thumbnail_handle` from the content-state entirely --
    not `""`, not `None` in the dict, genuinely absent, matching today's
    behavior."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)  # no external_base_url, no engine

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    assert "thumbnail_handle" not in starts[0]["payload"]["aps"]["content-state"]

    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1
    assert "thumbnail_handle" not in updates[0]["payload"]["aps"]["content-state"]


@pytest.mark.asyncio
async def test_start_payload_card_key_is_sentinel(sidecar_db_path: Path):
    """The start push's `attributes.card_key` must be the device sentinel,
    not the primary story's real card key -- that real key still reaches the
    app via content-state's `deep_link_card_key`. Regression for the prod
    bug where the two disagreed, which broke `attach_activity_token`'s
    upsert (see below) into overwriting the sentinel with a real card key.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    attributes = starts[0]["payload"]["aps"]["attributes"]
    assert attributes["card_key"] == store.DEVICE_SITUATION_ID
    assert attributes["track_id"] == store.DEVICE_TRACK_ID
    content_state = starts[0]["payload"]["aps"]["content-state"]
    assert content_state["deep_link_card_key"] == "doorbell:person:trkA"


@pytest.mark.asyncio
async def test_token_post_with_sentinel_situation_id_keeps_activity_findable(
    sidecar_db_path: Path,
):
    """Realistic full cycle: the app posts the token back with the SENTINEL
    situation_id it read off `attributes.card_key` -- `find_activity` must
    still find the row, the next mutation must send an UPDATE (not a second
    start), and the device must be `covered` (card push demoted to passive).
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=device.apns_token,
        situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID,
        token="perActivity1",
    )
    conn.commit()

    assert store.find_activity(conn, apns_token=device.apns_token) is not None

    before_card_sends = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # no duplicate start
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1

    new_card_sends = card_sends(transport)[before_card_sends:]
    assert new_card_sends
    for s in new_card_sends:
        assert s["payload"]["aps"]["interruption-level"] == "passive"


@pytest.mark.asyncio
async def test_token_post_with_real_card_key_does_not_clobber_sentinel(
    sidecar_db_path: Path,
):
    """REGRESSION for the exact prod failure: a stale/mismatched client
    posts the token with a REAL card key as `situation_id` (what the old
    client did) instead of the sentinel. The row must stay findable by
    `find_activity` (situation_id preserved as the sentinel, not
    overwritten) and must NOT cause a duplicate start on the next mutation.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    # Old/mismatched client: posts a real card key instead of the sentinel.
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=device.apns_token,
        situation_id="doorbell:person:trkA", track_id="trkA",
        token="perActivity1",
    )
    conn.commit()

    stored = conn.execute(
        "SELECT situation_id, track_id FROM push_activities WHERE activity_id = ?",
        (row["activity_id"],),
    ).fetchone()
    assert stored["situation_id"] == store.DEVICE_SITUATION_ID
    assert stored["track_id"] == store.DEVICE_TRACK_ID

    assert store.find_activity(conn, apns_token=device.apns_token) is not None

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # still no duplicate start
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1


@pytest.mark.asyncio
async def test_demo_row_insert_path_keeps_its_own_situation_id(sidecar_db_path: Path):
    """The demo row path (INSERT, no existing row for that activity_id)
    still stores whatever situation_id/track_id the app supplies, and stays
    inert (never surfaced by `find_activity`)."""
    conn = db.open_sidecar(sidecar_db_path)
    device = make_device()

    store.attach_activity_token(
        conn, activity_id="demo-activity-2", apns_token=device.apns_token,
        situation_id="demo:person:test-002", track_id="demo-test-002",
        token="demoToken2",
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM push_activities WHERE activity_id = 'demo-activity-2'"
    ).fetchone()
    assert row["situation_id"] == "demo:person:test-002"
    assert row["track_id"] == "demo-test-002"
    assert row["token"] == "demoToken2"
    assert store.find_activity(conn, apns_token=device.apns_token) is None


def card_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if not r.get("live_activity") and not r.get("test")]


@pytest.mark.asyncio
async def test_la_first_demotion_covers_all_stories(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    def _assert_all_passive(sends: list[dict]) -> None:
        assert sends
        for s in sends:
            aps = s["payload"]["aps"]
            assert aps["interruption-level"] == "passive"
            assert not aps.get("sound")
            assert aps["mutable-content"] == 1

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # LA start accepted -- the card still delivers (NSE must still run) but
    # demoted to passive.
    _assert_all_passive(card_sends(transport))
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    before = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )
    # B is a new story joining the already-live device activity -- the
    # ordinary card push for B must be demoted too, not just A's.
    new_sends = card_sends(transport)[before:]
    _assert_all_passive(new_sends)

    # A further mutation of B (escalation) is still covered/demoted.
    before = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=40.0,
    )
    _assert_all_passive(card_sends(transport)[before:])


@pytest.mark.asyncio
async def test_demo_row_not_found_as_device_activity(sidecar_db_path: Path):
    """The app's debug demo activity (Settings -> "Try") is a real row in
    `push_activities`, but it isn't the sidecar's device activity: it must
    never come back from `find_activity`/`find_dismissed_activity`."""
    conn = db.open_sidecar(sidecar_db_path)
    device = make_device()
    _open_demo_activity(conn, device=device)

    assert store.find_activity(conn, apns_token=device.apns_token) is None

    store.dismiss_activity(conn, "demo-activity-1")
    assert store.find_dismissed_activity(conn, apns_token=device.apns_token) is None


@pytest.mark.asyncio
async def test_deferred_end_sends_nothing_with_only_demo_row_open(sidecar_db_path: Path):
    """With only a demo row open (no device activity), the deferred-end path
    must find nothing to end and must not send anything at it."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    _open_demo_activity(conn, device=device)

    ended = await end_activity_if_card_closed(
        conn, device, transport, token="demoPerActivityToken", now=1.0,
    )
    assert ended is False
    assert la_sends(transport) == []

    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row is not None
    assert row["ended_at"] is None  # the demo row was left completely alone


@pytest.mark.asyncio
async def test_create_starts_genuine_activity_despite_open_demo_row(sidecar_db_path: Path):
    """A real CREATE mutation must start a genuine device activity rather
    than treating the open demo row as "the device activity" and routing an
    update to it instead."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    _open_demo_activity(conn, device=device)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )

    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # a genuine start, not a demo-row update

    rows = conn.execute(
        "SELECT situation_id FROM push_activities WHERE apns_token = ? AND ended_at IS NULL",
        (device.apns_token,),
    ).fetchall()
    situation_ids = {r["situation_id"] for r in rows}
    assert situation_ids == {store.DEVICE_SITUATION_ID, "demo:person:test-001"}


@pytest.mark.asyncio
async def test_demo_row_never_updated_or_ended_by_delivery(sidecar_db_path: Path):
    """Once a genuine device activity exists alongside the demo row, further
    delivery traffic (update, resolve) must only ever touch the device row --
    the demo row's own token must never appear as an update/end target."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)
    _open_demo_activity(conn, device=device)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(conn, device=device, card_key="doorbell:person:trkA", track_id="trkA",
                 token="perActivity1")

    await handle_delivery_resolve(
        "doorbell", "trkA", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=5.0,
    )

    demo_targets = [
        r for r in la_sends(transport)
        if r["token"] == "demoPerActivityToken"
        or r.get("collapse_id") == "demo:person:test-001"
    ]
    assert demo_targets == []

    demo_row = conn.execute(
        "SELECT * FROM push_activities WHERE activity_id = 'demo-activity-1'"
    ).fetchone()
    assert demo_row["ended_at"] is None  # untouched by the resolve/sweep path


@pytest.mark.asyncio
async def test_start_payload_attributes_card_key_is_sentinel(sidecar_db_path: Path):
    """Regression test for the prod bug: `attributes.card_key` on the start
    payload must be the device-scoped sentinel `store.DEVICE_SITUATION_ID`,
    NOT the real card key -- the app echoes `attributes.cardKey` back as the
    activity's `situation_id` when it posts its token, so if the start
    payload disagreed with `open_activity`'s own sentinel row, the app's
    token upload would silently retarget the row under a real card key and
    `find_activity` (sentinel-scoped) would never see it again. The real
    card key still routes deep-linking via content-state.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    attributes = starts[0]["payload"]["aps"]["attributes"]
    assert attributes["card_key"] == store.DEVICE_SITUATION_ID
    content_state = starts[0]["payload"]["aps"]["content-state"]
    assert content_state["deep_link_card_key"] == "doorbell:person:trkA"

    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["situation_id"] == store.DEVICE_SITUATION_ID


@pytest.mark.asyncio
async def test_full_cycle_token_posted_with_sentinel_situation_id(sidecar_db_path: Path):
    """The realistic happy path: the app posts its token using the sentinel
    `situation_id`/`track_id` straight off `attributes` (now correct, per
    the fix above). `find_activity` must still find the row, the next
    mutation must send an UPDATE (not a second start), and the device must
    land in `covered` so the ordinary card push is demoted to passive.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    attach_token(
        conn, device=device, card_key=store.DEVICE_SITUATION_ID,
        track_id=store.DEVICE_TRACK_ID, token="perActivity1",
    )

    assert store.find_activity(conn, apns_token=device.apns_token) is not None

    before_card_sends = len(card_sends(transport))
    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # no duplicate start
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1

    new_card_sends = card_sends(transport)[before_card_sends:]
    assert new_card_sends
    for s in new_card_sends:
        assert s["payload"]["aps"]["interruption-level"] == "passive"


@pytest.mark.asyncio
async def test_regression_app_posts_token_with_real_card_key(sidecar_db_path: Path):
    """Exact prod failure mode: a stale/mismatched client posts its token
    with a REAL card key as `situation_id` instead of the sentinel. Per the
    `attach_activity_token` fix, an UPSERT against an EXISTING row (the
    sidecar's own) must leave `situation_id`/`track_id` untouched, so the row
    stays findable under the sentinel and the next mutation is an UPDATE, not
    a duplicate start.
    """
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trkA", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["situation_id"] == store.DEVICE_SITUATION_ID

    # The mismatched/old client posts back a REAL card key, not the sentinel.
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=device.apns_token,
        situation_id="doorbell:person:trkA", track_id="trkA", token="perActivity1",
    )
    conn.commit()

    # The sentinel must have survived the upsert -- NOT clobbered.
    row = find_activity_row(conn, apns_token=device.apns_token)
    assert row["situation_id"] == store.DEVICE_SITUATION_ID
    assert row["track_id"] == store.DEVICE_TRACK_ID
    assert row["token"] == "perActivity1"

    assert store.find_activity(conn, apns_token=device.apns_token) is not None

    await handle_delivery_event(
        make_event("doorbell", "trkB", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=20.0,
    )

    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1  # NOT a duplicate start
    updates = [r for r in la_sends(transport) if r["event"] == "update"]
    assert len(updates) == 1

    rows = conn.execute(
        "SELECT COUNT(*) FROM push_activities WHERE apns_token = ? AND ended_at IS NULL",
        (device.apns_token,),
    ).fetchone()[0]
    assert rows == 1  # still exactly one open row for this device


@pytest.mark.asyncio
async def test_demo_row_insert_keeps_app_supplied_situation_id(sidecar_db_path: Path):
    """The INSERT branch (no existing row -- the app's debug demo activity,
    which the sidecar never opened) must keep storing the app-supplied
    `situation_id`/`track_id` exactly as before; only the UPDATE branch
    protects the sidecar's own sentinel row."""
    conn = db.open_sidecar(sidecar_db_path)
    device = make_device()

    store.attach_activity_token(
        conn, activity_id="demo-activity-2", apns_token=device.apns_token,
        situation_id="demo:person:test-002", track_id="demo-test-002",
        token="demoToken2",
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM push_activities WHERE activity_id = 'demo-activity-2'"
    ).fetchone()
    assert row["situation_id"] == "demo:person:test-002"
    assert row["track_id"] == "demo-test-002"
    assert row["token"] == "demoToken2"
    # Inert: never found as the device activity.
    assert store.find_activity(conn, apns_token=device.apns_token) is None
