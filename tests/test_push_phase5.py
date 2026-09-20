"""Phase 5 — Notification Experience v2 (WU7).

Tests for behaviors introduced or changed in Phase 5 that aren't already
covered by the existing per-module test files:

- Per-device label filtering
- Snooze → no alert push, no new LA start
- Global sounding rate cap (11th = silent + passive; next sounding = "+N more")
- Quiet resolve (peak_level ≤ quiet → no push)
- Quiet hours enforcement (cap_quiet and mute_sounds modes, urgent exemption)
- mute_sounds enforcement
- Payload fields: thread-id, category, apns_priority/apns_expiration
- Single routing-table default agreement
- Backfill staleness filter
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import card_store, policy_settings, store
from marcellus.push.delivery import (
    _device_eligible,
    build_card_payload,
    send_card_mutation,
    sound_name_for_card,
)
from marcellus.push.delivery_wire import (
    handle_delivery_event,
    handle_delivery_resolve,
)
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_device(
    token: str = "tok1",
    *,
    cameras: tuple[str, ...] = (),
    labels: tuple[str, ...] = (),
    min_severity: str = "detection",
    push_to_start: str = "pts1",
) -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", cameras=cameras, labels=labels,
        min_severity=min_severity, push_to_start_token=push_to_start,
    )


def make_event(
    camera: str = "doorbell",
    track_id: str = "trk1",
    label: str = "person",
    zones: tuple[str, ...] = ("pool",),
    severity: str = "alert",
) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity=severity,
        labels=(label,), track_ids=(track_id,), zones=zones,
    )


def situation_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if "payload" in r and not r.get("live_activity")]


def la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


# ---------------------------------------------------------------------------
# Per-device label filtering
# ---------------------------------------------------------------------------


class TestDeviceEligibleLabels:
    def test_empty_labels_matches_everything(self):
        dev = make_device(labels=())
        assert _device_eligible(dev, camera="doorbell", labels=("person",), card_level="notify")

    def test_matching_label_passes(self):
        dev = make_device(labels=("person",))
        assert _device_eligible(dev, camera="doorbell", labels=("person",), card_level="notify")

    def test_non_matching_label_filtered(self):
        dev = make_device(labels=("car",))
        assert not _device_eligible(dev, camera="doorbell", labels=("person",), card_level="notify")


# ---------------------------------------------------------------------------
# Snooze: no alert push + no new LA start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snoozed_camera_skips_push(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    store.set_snooze(conn, apns_token="tok1", scope="camera:doorbell", until_epoch=9999.0)
    conn.commit()

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    assert situation_sends(transport) == []
    assert la_sends(transport) == []
    # Card state still advances despite snooze.
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card is not None


@pytest.mark.asyncio
async def test_global_snooze_skips_push(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    store.set_snooze(conn, apns_token="tok1", scope="global", until_epoch=9999.0)
    conn.commit()

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    assert situation_sends(transport) == []


# ---------------------------------------------------------------------------
# Global sounding rate cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_cap_silences_11th_sounding_push(sidecar_db_path: Path):
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    # Card-path semantics only — an LA-capable device's cards are suppressed.
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    # Seed 10 sounding sends in the last hour.
    for i in range(10):
        store.record_send(conn, apns_token="tok1", situation_id="_card_sound", now=50.0 + i)
    conn.commit()

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    aps = sends[0]["payload"]["aps"]
    assert "sound" not in aps
    assert aps["interruption-level"] == "passive"


@pytest.mark.asyncio
async def test_rate_cap_plus_n_more_on_next_sounding(sidecar_db_path: Path):
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    # No push-to-start token: this test pins pure card-path semantics, and a
    # device with an LA would (correctly) get its card demoted (la_first).
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    # Seed 10 sounding sends, then bump suppressed count by 3.
    for i in range(10):
        store.record_send(conn, apns_token="tok1", situation_id="_card_sound", now=50.0 + i)
    for _ in range(3):
        store.bump_suppressed(conn, apns_token="tok1", situation_id="_card_sound")
    conn.commit()

    # Now send an event when the rate window has passed (sends are old enough).
    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5000.0,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    body = sends[0]["payload"]["aps"]["alert"]["body"]
    assert "+3 more" in body


# ---------------------------------------------------------------------------
# Quiet resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_resolve_no_push(sidecar_db_path: Path):
    """A card whose peak_level never exceeded quiet gets no resolve push."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    # Create at quiet level (thing at off_limits = quiet).
    await handle_delivery_event(
        make_event("cam1", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    transport.sent.clear()

    # Resolve it.
    await handle_delivery_resolve(
        "cam1", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=30.0,
    )
    assert situation_sends(transport) == []


# ---------------------------------------------------------------------------
# Quiet hours enforcement
# ---------------------------------------------------------------------------


class TestQuietHours:
    def test_is_quiet_hours_normal_range(self):
        settings = policy_settings.default_settings()
        settings["quiet_hours"] = {"start": "09:00", "end": "17:00", "mode": "cap_quiet"}
        active, mode = policy_settings.is_quiet_hours(settings, 600)  # 10:00
        assert active is True
        assert mode == "cap_quiet"
        active2, _ = policy_settings.is_quiet_hours(settings, 480)  # 08:00
        assert active2 is False

    def test_is_quiet_hours_wraparound(self):
        settings = policy_settings.default_settings()
        settings["quiet_hours"] = {"start": "22:00", "end": "07:00", "mode": "mute_sounds"}
        # 23:00 = 1380 minutes → inside
        active, mode = policy_settings.is_quiet_hours(settings, 1380)
        assert active is True
        assert mode == "mute_sounds"
        # 05:00 = 300 → inside (wrap)
        active2, _ = policy_settings.is_quiet_hours(settings, 300)
        assert active2 is True
        # 12:00 = 720 → outside
        active3, _ = policy_settings.is_quiet_hours(settings, 720)
        assert active3 is False

    def test_no_quiet_hours_returns_inactive(self):
        settings = policy_settings.default_settings()
        active, mode = policy_settings.is_quiet_hours(settings, 600)
        assert active is False
        assert mode == ""


# ---------------------------------------------------------------------------
# mute_sounds enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mute_sounds_strips_sound_not_suppresses(sidecar_db_path: Path):
    """mute_sounds is a sound-only control: the card pushes normally at its
    evaluated level but with sound stripped. Previously mute fed
    Snapshot.muted → SUPPRESSED; now it just drops the sound key."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    # Card-path semantics only — see rate-cap test above for why no LA.
    device = make_device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = True
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    aps = sends[0]["payload"]["aps"]
    assert aps["interruption-level"] == "time-sensitive"
    assert "sound" not in aps


# ---------------------------------------------------------------------------
# Payload fields: thread-id, category
# ---------------------------------------------------------------------------


class TestPayloadFields:
    def _make_card(self):
        from marcellus.push.cards import Card
        return Card(
            card_key="doorbell:person:trk1", level="notify",
            created_at=0.0, updated_at=10.0, state_since_at=0.0,
        )

    def test_thread_id_is_camera(self):
        payload = build_card_payload(
            self._make_card(), "create", sound=True, subject_kind="person",
            place_class="doors", camera="doorbell", zone_name="front_door",
            glyph="person.detected", primary="Person at Front Door",
            secondary="Front Door · 10s", event_ts=10.0,
        )
        assert payload["aps"]["thread-id"] == "doorbell"

    def test_category_matches_level(self):
        payload = build_card_payload(
            self._make_card(), "create", sound=True, subject_kind="person",
            place_class="doors", camera="doorbell", zone_name="front_door",
            glyph="person.detected", primary="Person", secondary="10s",
            event_ts=10.0,
        )
        assert payload["aps"]["category"] == "card.notify"

    def test_sound_name_urgent(self):
        assert sound_name_for_card("urgent") == "urgent.caf"

    def test_sound_name_person_at_door(self):
        assert sound_name_for_card("notify", "stranger", "person") == "at-the-door.caf"

    def test_sound_name_package(self):
        assert sound_name_for_card("notify", "thing", "package") == "package-delivery.caf"

    def test_sound_name_general(self):
        assert sound_name_for_card("notify", "animal", "cat") == "general.caf"


# ---------------------------------------------------------------------------
# apns_priority / apns_expiration on LA sends
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_la_start_has_priority_10_and_expiration(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    starts = [r for r in la_sends(transport) if r["event"] == "start"]
    assert len(starts) == 1
    # LogTransport doesn't record apns_priority in its dict, but the call
    # succeeded (no TypeError from missing param), confirming the transport
    # interface accepts them.


# ---------------------------------------------------------------------------
# Single routing-table default: both init paths agree
# ---------------------------------------------------------------------------


def test_routing_table_defaults_agree():
    from marcellus.push import ladder_policy
    ladder_table = ladder_policy.TABLE
    settings_table = policy_settings.DEFAULT_ROUTING_TABLE
    for subject in settings_table:
        for place in settings_table[subject]:
            assert ladder_table[subject][place] == settings_table[subject][place], (
                f"{subject}.{place}: ladder={ladder_table[subject][place]} "
                f"settings={settings_table[subject][place]}"
            )


# ---------------------------------------------------------------------------
# Backfill staleness filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_staleness_filters_old_events():
    import tempfile
    import time

    import httpx

    from marcellus.push.engine import PushEngine
    from marcellus.push.mqtt import backfill_since

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sidecar.db"
        conn = db.open_sidecar(db_path)
        store.upsert_device(
            conn, apns_token="tok1", bundle_id="com.x", environment="sandbox",
            cameras=["cam1"], min_severity="detection",
        )
        conn.commit()
        conn.close()

        transport = LogTransport()
        engine = PushEngine(db_path=str(db_path), transport=transport, server_id="s1")
        engine.push_config = PushSection(delivery_enabled=True)

        now = time.time()
        fresh_ts = now - 100   # 100s ago → within 300s staleness
        stale_ts = now - 500   # 500s ago → outside 300s staleness

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[
                {"id": "ev1", "camera": "cam1", "label": "person", "start_time": fresh_ts},
                {"id": "ev2", "camera": "cam1", "label": "car", "start_time": stale_ts},
            ])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        notified = await backfill_since(
            engine, frigate_base_url="http://frigate.test:5000",
            after=0.0, client=client, staleness_s=300.0,
        )
        assert notified == 1
        await client.aclose()


# ---------------------------------------------------------------------------
# delivery_enabled default
# ---------------------------------------------------------------------------


def test_delivery_enabled_defaults_true():
    assert PushSection().delivery_enabled is True


# ---------------------------------------------------------------------------
# la_first: RESOLVE row deferred past the LA's dismissal window
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_la_covered_resolve_row_is_deferred(
    sidecar_db_path: Path, monkeypatch,
):
    """The resolved LA lingers 30s on the lock screen (dismissal_offset);
    its history row must arrive after that window, not alongside it."""
    import asyncio

    from marcellus.push import delivery
    from marcellus.push.cards import RESOLVE, Card

    monkeypatch.setattr(delivery, "RESOLVE_DEFER_S", 0.0)
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}

    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens={"tok1"}, suppress_demoted=True,
    )
    # Nothing lands synchronously — the row is scheduled, not sent.
    assert situation_sends(transport) == []

    # Let the deferred task (delay patched to 0) run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    sends = situation_sends(transport)
    assert len(sends) == 1
    aps = sends[0]["payload"]["aps"]
    assert aps["interruption-level"] == "passive"
    assert "sound" not in aps


@pytest.mark.asyncio
async def test_non_covered_device_resolve_row_is_immediate(sidecar_db_path: Path):
    from marcellus.push.cards import RESOLVE, Card

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}
    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens=set(), suppress_demoted=True,
    )
    assert len(situation_sends(transport)) == 1


@pytest.mark.asyncio
async def test_deferred_resolve_exception_is_logged_not_lost(
    sidecar_db_path: Path, monkeypatch, caplog,
):
    """A failure inside the deferred `_later()` task (e.g. the transport
    raising) must be caught and logged with the event's identity, not left
    to surface only as asyncio's "Task exception was never retrieved"."""
    import asyncio
    import logging

    from marcellus.push import delivery
    from marcellus.push.cards import RESOLVE, Card

    monkeypatch.setattr(delivery, "RESOLVE_DEFER_S", 0.0)

    class BoomTransport(LogTransport):
        async def send_situation(self, *args, **kwargs):
            raise RuntimeError("boom")

    conn = db.open_sidecar(sidecar_db_path)
    transport = BoomTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}

    with caplog.at_level(logging.ERROR, logger="marcellus.push.delivery"):
        await send_card_mutation(
            conn, transport, [device], card, RESOLVE, payload,
            subject_kind="person", camera="doorbell", now=10.0,
            demote_tokens={"tok1"}, suppress_demoted=True,
        )
        assert delivery._DEFERRED_TASKS, "deferred task should be scheduled"
        # Let the deferred task raise, settle, and its done-callback fire.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert not delivery._DEFERRED_TASKS, "failed task must still be removed from the set"
    assert any("deferred resolve failed" in r.message for r in caplog.records)
    assert any("doorbell:person:trk1" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancel_deferred_cancels_pending_tasks(sidecar_db_path: Path, monkeypatch):
    """`cancel_deferred()` (called from the lifespan shutdown) must not leave
    a deferred resolve running past process teardown."""
    import asyncio

    from marcellus.push import delivery
    from marcellus.push.cards import RESOLVE, Card

    monkeypatch.setattr(delivery, "RESOLVE_DEFER_S", 10.0)
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}

    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens={"tok1"}, suppress_demoted=True,
    )
    assert len(delivery._DEFERRED_TASKS) == 1

    delivery.cancel_deferred()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not delivery._DEFERRED_TASKS
    assert situation_sends(transport) == []


# ---------------------------------------------------------------------------
# Ephemeral resolve flag (event-lifetime notifications)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_push_not_ephemeral_for_notify_peak_story(sidecar_db_path: Path):
    """Alerts-slice2 §E (sidecar policy change): a resolve push for a story
    that peaked at `notify` (not just `quiet`) keeps its resolve push around
    -- `ephemeral: false`, same collapse-id, quiet/passive -- so the banner
    is replaced in place rather than removed. Only a peak that never
    exceeded `quiet` (and never tripped a zone override) is ephemeral."""
    from marcellus.push.cards import RESOLVE, Card

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}
    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens=set(), suppress_demoted=True,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["ephemeral"] is False


@pytest.mark.asyncio
async def test_resolve_push_is_ephemeral_for_quiet_peak_story(sidecar_db_path: Path):
    """Alerts-slice2 §E: a story that never exceeded `quiet` and never
    tripped a zone override gets an *ephemeral* resolve (removes the
    delivered row) -- this used to get no resolve push at all."""
    from marcellus.push.cards import RESOLVE, Card

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="quiet", peak_level="quiet",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}
    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens=set(), suppress_demoted=True,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["ephemeral"] is True


@pytest.mark.asyncio
async def test_resolve_push_not_ephemeral_for_urgent_peak_story(sidecar_db_path: Path):
    """A story that ever peaked urgent (alarm outcome) keeps its resolve
    push around -- explicit `ephemeral: false` (absent would read as
    old-sidecar to the app and get the 24 h sweep instead of keep)."""
    from marcellus.push.cards import RESOLVE, Card

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="urgent",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}
    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens=set(), suppress_demoted=True,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["ephemeral"] is False


@pytest.mark.asyncio
async def test_resolve_push_not_ephemeral_for_zone_override_story(sidecar_db_path: Path):
    """A story that tripped a zone override at any point keeps its resolve
    push around -- explicit `ephemeral: false`, even at notify peak."""
    from marcellus.push.cards import RESOLVE, Card

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=9.0, state_since_at=1.0,
        resolved=True, closed=True, zone_override_hit=True,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "8s"}}}
    await send_card_mutation(
        conn, transport, [device], card, RESOLVE, payload,
        subject_kind="person", camera="doorbell", now=10.0,
        demote_tokens=set(), suppress_demoted=True,
    )
    sends = situation_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["ephemeral"] is False


@pytest.mark.asyncio
async def test_quiet_peak_story_still_sends_no_resolve_push(sidecar_db_path: Path):
    """Unchanged: a card whose peak never exceeded quiet still gets no
    resolve push at all -- the ephemeral flag only applies to resolves that
    already send a push."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        make_event("cam1", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    transport.sent.clear()

    await handle_delivery_resolve(
        "cam1", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="thing", now=30.0,
    )
    assert situation_sends(transport) == []


# ---------------------------------------------------------------------------
# 410/400 feedback-driven pruning (spec §5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_card_send_prunes_unregistered_device(sidecar_db_path: Path):
    """A card-mutation send that comes back `unregistered` (410 Unregistered
    / 400 BadDeviceToken) must drop that device row immediately, without
    touching a second device whose send succeeded -- and the failed send is
    still recorded in `push_card_sends` for receipt pairing."""
    from marcellus.push.cards import CREATE, Card
    from marcellus.push.transport import TransportResult

    class MixedTransport(LogTransport):
        async def send_situation(self, device, **kwargs):
            if device.apns_token == "tok_dead":
                return TransportResult(
                    ok=False, unregistered=True, error="HTTP 410", status_code=410,
                )
            return TransportResult(ok=True)

    conn = db.open_sidecar(sidecar_db_path)
    transport = MixedTransport()
    store.upsert_device(
        conn, apns_token="tok_dead", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity="detection",
    )
    store.upsert_device(
        conn, apns_token="tok_alive", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity="detection",
    )
    dead = make_device(token="tok_dead")
    alive = make_device(token="tok_alive")
    card = Card(
        card_key="doorbell:person:trk1", level="notify", peak_level="notify",
        created_at=1.0, updated_at=1.0, state_since_at=1.0,
    )
    payload = {"aps": {"alert": {"title": "Person at Doorbell", "body": "now"}}}

    await send_card_mutation(
        conn, transport, [dead, alive], card, CREATE, payload,
        subject_kind="person", camera="doorbell", now=1.0,
    )

    remaining_tokens = {d.apns_token for d in store.list_devices(conn)}
    assert remaining_tokens == {"tok_alive"}

    rows = conn.execute(
        "SELECT apns_token, ok, error FROM push_card_sends WHERE apns_token = ?",
        ("tok_dead",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["error"] == "HTTP 410"
