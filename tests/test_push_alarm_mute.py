"""An Alarm (engine level `urgent`) under `mute_sounds` must be silent: no
sound on the first push, no sound-budget booked, and no repeating re-sound
(a repeat exists only to sound again)."""
from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import card_store, delivery, delivery_wire, policy_settings
from marcellus.push.delivery_wire import handle_delivery_event
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport

CARD_KEY = "cam-a:package:trkA"


def _device() -> Device:
    return Device(
        apns_token="tok1", device_id="d_tok1", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity="detection",
    )


def _apply(*, mute: bool) -> None:
    s = policy_settings.default_settings()
    s["ladder"]["package"]["yard"] = "urgent"
    s["mute_sounds"] = mute
    policy_settings.apply_settings(s)


async def _alarm(sidecar_db_path: Path, *, mute: bool):
    _apply(mute=mute)
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = _device()
    await handle_delivery_event(
        ReviewEvent(
            review_id="r1", camera="cam-a", severity="alert", labels=("package",),
            track_ids=("trkA",), zones=("driveway",),
        ),
        conn=conn, devices=[device], transport=transport,
        config=PushSection(delivery_enabled=True), now=0.0,
    )
    return conn, transport, device


def _sounds(transport: LogTransport) -> list:
    return [
        s["payload"]["aps"].get("sound") for s in transport.sent
        if s.get("payload") and "aps" in s["payload"] and s["payload"]["aps"].get("sound")
    ]


async def _sweep(conn, transport, device, *, now, muted):
    return await delivery.sweep_urgent_resound(
        conn, transport, [device], now=now, interval_s=120.0, enabled=True, max_resounds=5,
        payload_for_resound=delivery_wire.resound_payload_for, muted=muted,
    )


@pytest.mark.asyncio
async def test_muted_alarm_first_push_has_no_sound_and_books_no_budget(sidecar_db_path: Path):
    conn, transport, _ = await _alarm(sidecar_db_path, mute=True)
    card = card_store.get_card(conn, CARD_KEY)
    assert card.level == "urgent"
    assert _sounds(transport) == []
    assert card.sound_count == 0
    assert card.last_sound_at is None


@pytest.mark.asyncio
async def test_muted_alarm_never_repeats_or_sounds(sidecar_db_path: Path):
    conn, transport, device = await _alarm(sidecar_db_path, mute=True)
    before = len(transport.sent)
    for t in (120.0, 240.0, 360.0):
        assert await _sweep(conn, transport, device, now=t, muted=True) == 0
    assert len(transport.sent) == before
    assert _sounds(transport) == []


@pytest.mark.asyncio
async def test_resound_payload_is_silent_when_muted(sidecar_db_path: Path):
    conn, _, _ = await _alarm(sidecar_db_path, mute=True)
    card = card_store.get_card(conn, CARD_KEY)
    ctx = dict(card_store.list_open_urgent_cards(conn)[0][1])
    assert "sound" not in delivery_wire.resound_payload_for(card, ctx)["aps"]


@pytest.mark.asyncio
async def test_unmuted_alarm_still_sounds_and_repeats_up_to_five(sidecar_db_path: Path):
    conn, transport, device = await _alarm(sidecar_db_path, mute=False)
    assert len(_sounds(transport)) == 1
    assert await _sweep(conn, transport, device, now=60.0, muted=False) == 0  # < 120 s
    fired = 0
    for i in range(1, 8):
        fired += await _sweep(conn, transport, device, now=120.0 * i + 1, muted=False)
    assert fired == 5
    assert len(_sounds(transport)) == 6  # first alarm + 5 repeats
