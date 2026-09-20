"""Phase 5 WU7 — additional tests covering gaps not in test_push_phase5.py.

Organized by the 9 areas from the Phase 5 spec:
1. Per-device filtering (cameras, min_severity, two-device integration)
2. Sounding rate cap (silent pushes don't spend budget, window sliding)
3. Urgent re-sound (wire shape, stops on resolve, counts against rate cap)
4. Quiet hours integration (cap_quiet, mute_sounds, urgent exempt)
5. Payload contract (LA relevance-score, stale-date, dismissal-date)
6. Delivery hints (apns_priority/expiration on relay wire)
7. Relay key (x-relay-key header present/absent)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import card_store, policy_settings, store
from marcellus.push.cards import Card, urgent_resound_due
from marcellus.push.delivery import (
    _device_eligible,
    apply_urgent_resound,
    build_card_payload,
    sound_name_for_card,
)
from marcellus.push.delivery_wire import (
    handle_delivery_event,
    handle_delivery_resolve,
)
from marcellus.push.live_activities import (
    build_content_state,
    build_la_end_payload,
    build_la_start_payload,
    build_la_update_payload,
)
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport, RelayTransport


def _device(
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


def _event(
    camera: str = "doorbell",
    track_id: str = "trk1",
    label: str = "person",
    zones: tuple[str, ...] = ("pool",),
) -> ReviewEvent:
    return ReviewEvent(
        review_id=f"r_{camera}_{track_id}", camera=camera, severity="alert",
        labels=(label,), track_ids=(track_id,), zones=zones,
    )


def _sit_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if "payload" in r and not r.get("live_activity")]


def _la_sends(transport: LogTransport) -> list[dict]:
    return [r for r in transport.sent if r.get("live_activity")]


# ── 1. Per-device filtering ─────────────────────────────────────────────

class TestDeviceEligibleExtended:
    def test_cameras_filter_excludes_non_matching(self):
        dev = _device(cameras=("patio",))
        assert not _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_cameras_filter_includes_matching(self):
        dev = _device(cameras=("doorbell", "patio"))
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_min_severity_alert_rejects_quiet(self):
        dev = _device(min_severity="alert")
        assert not _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="quiet",
        )

    def test_min_severity_alert_accepts_notify(self):
        dev = _device(min_severity="alert")
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="notify",
        )

    def test_min_severity_detection_accepts_quiet(self):
        dev = _device(min_severity="detection")
        assert _device_eligible(
            dev, camera="doorbell", labels=("person",), card_level="quiet",
        )


@pytest.mark.asyncio
async def test_two_devices_only_eligible_one_receives(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    dev_match = _device("tok_match", cameras=("doorbell",), push_to_start="")
    dev_miss = _device("tok_miss", cameras=("patio",), push_to_start="")
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[dev_match, dev_miss], transport=transport,
        config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["device_id"] == "d_tok_match"


# ── 2. Sounding rate cap extended ────────────────────────────────────────

@pytest.mark.asyncio
async def test_silent_push_does_not_spend_rate_budget(sidecar_db_path: Path):
    """A quiet-level push (no sound) should not count toward the 10/hr cap."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    await handle_delivery_event(
        _event("doorbell", "trk1", "package", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    count = store.count_sends_since(
        conn, apns_token="tok1", situation_id="_card_sound", since=0.0,
    )
    assert count == 0


@pytest.mark.asyncio
async def test_rate_cap_window_slides(sidecar_db_path: Path):
    """Sends older than 1 hour don't count — the 11th push sounds if old ones aged out."""
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    # Card-path semantics only: an LA-capable device would get demoted cards.
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    for i in range(10):
        store.record_send(
            conn, apns_token="tok1", situation_id="_card_sound", now=50.0 + i,
        )
    conn.commit()

    # now=5000 → all 10 sends are >3600s old, window is clear
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=5000.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert "sound" in sends[0]["payload"]["aps"]


# ── 3. Urgent re-sound ──────────────────────────────────────────────────

def test_urgent_resound_due_stops_at_max():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=5,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True, max_resounds=5)


def test_urgent_resound_due_fires_under_max():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=4,
    )
    assert urgent_resound_due(card, now=999, interval_s=120, enabled=True, max_resounds=5)


def test_urgent_resound_not_due_when_resolved():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=0, resolved=True,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


def test_urgent_resound_not_due_when_handled():
    card = Card(
        card_key="k", level="urgent", created_at=0, updated_at=0,
        last_sound_at=0, resound_count=0, handled=True,
    )
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


def test_resound_payload_is_escalate_with_sound():
    """A re-sound on the wire looks like an escalate with aps.sound set."""
    card = Card(
        card_key="doorbell:person:trk1", level="urgent",
        created_at=0, updated_at=0, state_since_at=0,
        last_sound_at=0, resound_count=1, peak_level="urgent",
    )
    card = apply_urgent_resound(card, now=500.0)
    payload = build_card_payload(
        card, "escalate", sound=True, subject_kind="person", place_class="off_limits",
        camera="doorbell", zone_name="pool", glyph="person.detected",
        primary="Person at Pool", secondary="Pool · 500s", event_ts=500.0,
    )
    assert payload["mutation"] == "escalate"
    assert payload["aps"]["sound"] == "urgent.caf"
    assert payload["aps"]["interruption-level"] == "time-sensitive"


@pytest.mark.asyncio
async def test_resound_stops_on_resolve(sidecar_db_path: Path):
    """Once a card is resolved, urgent_resound_due returns False."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    # Create an urgent card
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # Resolve it
    await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=[device], transport=transport,
        config=config, subject_kind="person", now=500.0,
    )
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card is not None
    assert card.resolved
    assert not urgent_resound_due(card, now=999, interval_s=120, enabled=True)


# ── 4. Quiet hours integration ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_quiet_hours_cap_quiet_caps_notify_to_quiet(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "cap_quiet"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    # quiet no longer pushes at all (user feedback 2026-08-14): a story
    # capped to quiet is recorded, not announced.
    assert _sit_sends(transport) == []
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card is not None and card.level == "quiet"


@pytest.mark.asyncio
async def test_quiet_hours_cap_quiet_exempts_urgent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "cap_quiet"}
    policy_settings.apply_settings(settings)

    # stranger + off_limits = urgent
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "urgent"


@pytest.mark.asyncio
async def test_quiet_hours_cap_quiet_with_mute_sounds_does_not_crash(sidecar_db_path: Path):
    """mute_sounds + cap_quiet: mute is now a sound-only control (not
    suppression), so the card evaluates normally, cap_quiet caps notify→quiet,
    and mute strips sound. Quiet never pushes (2026-08-14), so the card
    advances silently with no send and no crash."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = True
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "cap_quiet"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    assert _sit_sends(transport) == []
    card = card_store.get_card(conn, "doorbell:person:trk1")
    assert card is not None and card.level == "quiet"


@pytest.mark.asyncio
async def test_quiet_hours_mute_sounds_strips_sound(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "mute_sounds"}
    policy_settings.apply_settings(settings)

    # stranger + doors = notify (would normally sound)
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert sends[0]["payload"]["level"] == "notify"
    assert "sound" not in sends[0]["payload"]["aps"]


@pytest.mark.asyncio
async def test_quiet_hours_mute_sounds_exempts_urgent(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    # Card-path semantics only: an LA-capable device would get demoted cards.
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["quiet_hours"] = {"start": "00:00", "end": "23:59", "mode": "mute_sounds"}
    policy_settings.apply_settings(settings)

    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=100.0,
    )
    sends = _sit_sends(transport)
    assert len(sends) == 1
    assert "sound" in sends[0]["payload"]["aps"]


def test_quiet_hours_boundary_start_is_inclusive():
    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "09:00", "end": "17:00", "mode": "cap_quiet"}
    active, _ = policy_settings.is_quiet_hours(settings, 540)  # exactly 09:00
    assert active is True


def test_quiet_hours_boundary_end_is_exclusive():
    settings = policy_settings.default_settings()
    settings["quiet_hours"] = {"start": "09:00", "end": "17:00", "mode": "cap_quiet"}
    active, _ = policy_settings.is_quiet_hours(settings, 1020)  # exactly 17:00
    assert active is False


# ── 5. Payload contract — LA payloads ────────────────────────────────────

class TestLaPayloadContract:
    def test_la_start_relevance_score_by_level(self):
        for level, expected in [("urgent", 1.0), ("notify", 0.75), ("quiet", 0.5), ("log", 0.25)]:
            state = build_content_state(
                level=level, mutation="create", glyph="g", primary="P",
                secondary="S", elapsed_seconds=0, card_key="k",
                thumbnail_handle=None, thumbnail_revision=1,
            )
            payload = build_la_start_payload(
                content_state=state, family="person", camera="c",
                track_id="t", card_key="k", now=1000.0,
            )
            assert payload["aps"]["relevance-score"] == expected, f"level={level}"

    def test_la_start_stale_date_is_now_plus_900(self):
        state = build_content_state(
            level="notify", mutation="create", glyph="g", primary="P",
            secondary="S", elapsed_seconds=0, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_start_payload(
            content_state=state, family="person", camera="c",
            track_id="t", card_key="k", now=1000.0, stale_s=900.0,
        )
        assert payload["aps"]["stale-date"] == 1900

    def test_la_update_stale_date(self):
        state = build_content_state(
            level="notify", mutation="enrich", glyph="g", primary="P",
            secondary="S", elapsed_seconds=10, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(
            content_state=state, now=2000.0, stale_s=900.0,
        )
        assert payload["aps"]["stale-date"] == 2900

    def test_la_end_dismissal_date_plus_30s(self):
        state = build_content_state(
            level="notify", mutation="resolve", glyph="g", primary="P",
            secondary="S", elapsed_seconds=60, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_end_payload(content_state=state, now=3000.0, dismissal_offset=30.0)
        assert payload["aps"]["dismissal-date"] == 3030

    def test_la_update_escalation_alert_has_sound_and_interruption(self):
        state = build_content_state(
            level="urgent", mutation="escalate", glyph="g", primary="P",
            secondary="S", elapsed_seconds=30, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(
            content_state=state, now=1000.0, stale_s=900.0,
            alert=True, alert_title="P", alert_body="S",
            sound="urgent.caf", interruption_level="time-sensitive",
        )
        assert payload["aps"]["alert"] == {"title": "P", "body": "S", "sound": "urgent.caf"}
        assert payload["aps"]["interruption-level"] == "time-sensitive"

    def test_la_update_no_alert_has_no_sound(self):
        state = build_content_state(
            level="notify", mutation="enrich", glyph="g", primary="P",
            secondary="S", elapsed_seconds=10, card_key="k",
            thumbnail_handle=None, thumbnail_revision=1,
        )
        payload = build_la_update_payload(content_state=state, now=1000.0)
        assert "alert" not in payload["aps"]
        assert "sound" not in payload["aps"]


def test_sound_name_known_person_at_door():
    assert sound_name_for_card("notify", "known", "person") == "at-the-door.caf"


# ── 6. Delivery hints — apns_priority/expiration on relay wire ───────────

@pytest.mark.asyncio
async def test_relay_situation_carries_priority_and_expiration():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_situation(
        dev, payload={"aps": {}}, collapse_id="c1",
        apns_priority=10, apns_expiration=99999,
    )
    assert captured["json"]["apns_priority"] == 10
    assert captured["json"]["apns_expiration"] == 99999
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_situation_omits_priority_when_none():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert "apns_priority" not in captured["json"]
    assert "apns_expiration" not in captured["json"]
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_la_carries_priority_and_expiration():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client)
    dev = _device()
    await relay.send_live_activity(
        dev, token="tok", payload={"aps": {}}, collapse_id="c1",
        event="update", apns_priority=5, apns_expiration=88888,
    )
    assert captured["json"]["apns_priority"] == 5
    assert captured["json"]["apns_expiration"] == 88888
    await relay.aclose()


# ── 7. Relay key on the wire ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relay_key_header_present_when_set():
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="secret123")
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert captured_headers["x-relay-key"] == "secret123"
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_key_header_absent_when_empty():
    captured_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="")
    dev = _device()
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c1")
    assert "x-relay-key" not in captured_headers
    await relay.aclose()


@pytest.mark.asyncio
async def test_relay_key_on_la_and_test_endpoints():
    """Every relay method (send, send_situation, send_live_activity, send_test)
    carries the x-relay-key header."""
    seen_headers: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.test", client=client, relay_key="k1")
    dev = _device()
    await relay.send(
        dev, handle="h", server_id="s", severity="alert", collapse_id="c",
    )
    await relay.send_situation(dev, payload={"aps": {}}, collapse_id="c")
    await relay.send_live_activity(
        dev, token="t", payload={"aps": {}}, collapse_id="c", event="start",
    )
    await relay.send_test(dev)
    assert all(h.get("x-relay-key") == "k1" for h in seen_headers)
    assert len(seen_headers) == 4
    await relay.aclose()


def test_relay_without_key_logs_critical_at_startup(caplog):
    """`transport = "relay"` with an empty relay_key is a live misconfiguration
    (every push goes out unauthenticated) -- startup must say so at CRITICAL,
    without refusing to start (a deploy mid-upgrade shouldn't hard-fail)."""
    from marcellus.config import PushSection, Settings
    from marcellus.server import _build_push_transport

    settings = Settings(push=PushSection(transport="relay", relay_key=""))
    with caplog.at_level(logging.CRITICAL, logger="marcellus.server"):
        transport = _build_push_transport(settings)
    assert isinstance(transport, RelayTransport)
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical and "relay_key" in critical[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.CRITICAL, logger="marcellus.server"):
        _build_push_transport(Settings(push=PushSection(transport="relay", relay_key="k1")))
    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


# ── 8. Sound filename correctness ───────────────────────────────────────

_VALID_SOUNDS = frozenset([
    "at-the-door.caf", "confirmation.caf", "elevated.caf", "general.caf",
    "investigate.caf", "package-delivery.caf", "urgent.caf", "watch.caf",
])


class TestSoundFilenames:
    def test_sound_name_uses_label_not_place_class(self):
        assert sound_name_for_card("notify", "stranger", "person") == "at-the-door.caf"
        assert sound_name_for_card("notify", "stranger", "doors") == "general.caf"

    def test_sound_name_package(self):
        assert sound_name_for_card("notify", "thing", "package") == "package-delivery.caf"

    def test_sound_name_urgent_always(self):
        assert sound_name_for_card("urgent", "stranger", "person") == "urgent.caf"
        assert sound_name_for_card("urgent", "thing", "package") == "urgent.caf"

    def test_all_sound_names_are_bare_filenames(self):
        cases = [
            ("urgent", "stranger", "person"),
            ("notify", "stranger", "person"),
            ("notify", "thing", "package"),
            ("notify", "thing", "car"),
            ("notify", "animal", "dog"),
            ("quiet", "stranger", "person"),
        ]
        for level, kind, label in cases:
            name = sound_name_for_card(level, kind, label)
            assert "/" not in name, f"sound name {name!r} contains a path separator"
            assert name in _VALID_SOUNDS, f"sound name {name!r} not in app bundle catalog"

    def test_la_start_attributes_match_swift_contract(self):
        state = build_content_state(
            level="notify", mutation="create", glyph="person.detected",
            primary="P", secondary="S", elapsed_seconds=0,
            card_key="doorbell:person:t1",
            thumbnail_handle="h_abc", thumbnail_revision=1,
        )
        payload = build_la_start_payload(
            content_state=state, family="person", camera="doorbell",
            track_id="t1", card_key="doorbell:person:t1", now=1000.0,
        )
        attrs = payload["aps"]["attributes"]
        assert set(attrs) == {"card_key", "family", "camera", "track_id"}
        cs = payload["aps"]["content-state"]
        expected_cs_keys = {
            "level", "mutation", "glyph", "primary", "secondary",
            "elapsed_seconds", "deep_link_card_key",
            "thumbnail_handle", "thumbnail_revision",
        }
        assert set(cs) == expected_cs_keys
        assert payload["aps"]["attributes-type"] == "ElsinoreActivityAttributes"
        assert "alert" in payload["aps"]
        assert payload["aps"]["alert"]["title"] == "P"
        assert payload["aps"]["alert"]["body"] == "S"

    def test_la_content_state_thumbnail_revision_always_present(self):
        state = build_content_state(
            level="notify", mutation="create", glyph="g",
            primary="P", secondary="S", elapsed_seconds=0,
            card_key="k", thumbnail_handle=None, thumbnail_revision=0,
        )
        assert "thumbnail_revision" in state
        assert state["thumbnail_revision"] == 0
        assert "thumbnail_handle" not in state

    def test_build_card_payload_sound_uses_label(self):
        card = Card(
            card_key="doorbell:person:t1", level="notify", peak_level="notify",
            sound_count=0, state_since_at=1000.0, created_at=1000.0, updated_at=1000.0,
        )
        payload = build_card_payload(
            card, "create", sound=True, subject_kind="person",
            place_class="doors", label="person", camera="doorbell",
            zone_name="front_door", glyph="person.detected",
            primary="Someone at Front Door", secondary="Front Door · 0s",
            event_ts=1000.0,
        )
        assert payload["aps"]["sound"] == "at-the-door.caf"


# ── 9. LA activity persistence (conn.commit) ─────────────────────────────

@pytest.mark.asyncio
async def test_la_push_to_start_persists_across_mutations(tmp_path):
    """Regression: open_activity must be committed so subsequent mutations
    find the activity row. Without commit, conn.close() rolls it back."""
    db_path = tmp_path / "sidecar.db"
    conn = db.open_sidecar(db_path)
    dev = _device()
    store.upsert_device(
        conn, apns_token=dev.apns_token, bundle_id=dev.bundle_id,
        environment=dev.environment, cameras=[], min_severity="detection",
        push_to_start_token=dev.push_to_start_token,
    )
    conn.commit()
    conn.close()

    transport = LogTransport()
    config = PushSection(delivery_enabled=True)
    policy_settings.apply_settings(policy_settings.default_settings())

    event_create = _event(camera="doorbell", zones=("front_door",), label="person")
    event_enrich = ReviewEvent(
        review_id=event_create.review_id, camera="doorbell", severity="alert",
        labels=("person",), track_ids=event_create.track_ids, zones=("front_door",),
    )

    from marcellus.push.engine import PushEngine
    engine = PushEngine(db_path=str(db_path), transport=transport, server_id="test")
    engine.push_config = config

    conn1 = engine._conn()
    await handle_delivery_event(
        event_create, conn=conn1, devices=[dev], transport=transport,
        config=config, now=1000.0,
    )
    conn1.close()

    la_starts = [s for s in transport.sent if s.get("live_activity") and s.get("event") == "start"]
    assert len(la_starts) == 1, "push-to-start should fire on create"

    # Simulate the app attaching a per-activity token (iOS gives it after
    # ActivityKit creates the Live Activity from the push-to-start).
    conn_attach = engine._conn()
    activity_row = store.find_activity(conn_attach, apns_token=dev.apns_token)
    assert activity_row is not None, "activity row must survive conn1.close()"
    store.attach_activity_token(
        conn_attach, activity_id=activity_row["activity_id"],
        apns_token=dev.apns_token, situation_id=store.DEVICE_SITUATION_ID,
        track_id=store.DEVICE_TRACK_ID, token="per-activity-tok",
    )
    conn_attach.commit()
    conn_attach.close()

    conn2 = engine._conn()
    await handle_delivery_event(
        event_enrich, conn=conn2, devices=[dev], transport=transport,
        config=config, now=1005.0,
    )
    conn2.close()

    la_updates = [
        s for s in transport.sent
        if s.get("live_activity") and s.get("event") == "update"
    ]
    assert len(la_updates) >= 1, (
        "LA update should fire on enrich (activity row must survive conn.close)"
    )


# ── 10. mute_sounds refinements ────────────────────────────────────────────


def test_fresh_settings_default_muted():
    """Change 2: fresh settings (no stored value) default mute_sounds=True."""
    settings = policy_settings.default_settings()
    assert settings["mute_sounds"] is True


@pytest.mark.asyncio
async def test_unmuted_story_sounded_at_start_silent_at_escalation(sidecar_db_path: Path):
    """Change 3: one sound per story — a card that sounded at create doesn't
    sound again at escalation."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)

    # Create at notify: sounds (budget spent).
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("front_door",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    create_aps = _sit_sends(transport)[0]["payload"]["aps"]
    assert create_aps.get("sound")

    # Escalate to urgent: no sound (budget exhausted).
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    esc_aps = _sit_sends(transport)[-1]["payload"]["aps"]
    assert esc_aps["interruption-level"] == "time-sensitive"
    assert "sound" not in esc_aps


@pytest.mark.asyncio
async def test_unmuted_story_silent_at_start_sounds_at_urgent(sidecar_db_path: Path):
    """Change 3: a story that was silent at create (quiet level) still sounds
    when it escalates to urgent."""
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device(push_to_start="")
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    policy_settings.apply_settings(settings)

    # Create at quiet (person at yard): no sound.
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("yard",)),
        conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    # quiet create no longer pushes at all (2026-08-14).
    assert _sit_sends(transport) == []

    # Escalate to urgent (pool): sounds (budget still available).
    await handle_delivery_event(
        _event("doorbell", "trk1", "person", zones=("pool",)),
        conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    esc_aps = _sit_sends(transport)[-1]["payload"]["aps"]
    assert esc_aps["interruption-level"] == "time-sensitive"
    assert esc_aps.get("sound") == "urgent.caf"
