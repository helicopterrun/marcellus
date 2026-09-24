from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.push import doorbell, store
from marcellus.push.transport import LogTransport


@pytest.fixture(autouse=True)
def _reset_dedup():
    doorbell.reset_ring_dedup_for_tests()
    yield
    doorbell.reset_ring_dedup_for_tests()


def _conn(tmp_path: Path):
    return db.open_sidecar(str(tmp_path / "sidecar.db"))


def _register(conn, token: str = "tok-1", **kw) -> None:
    store.upsert_device(
        conn,
        apns_token=token,
        bundle_id="com.x",
        environment="sandbox",
        cameras=[],
        labels=[],
        **kw,
    )
    conn.commit()


# -- camera mapping -----------------------------------------------------


def test_unmapped_camera_is_dropped(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    transport = LogTransport()
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="unknown-id",
            protect_event_id="",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
        )
    )
    assert outcome.skipped_unmapped is True
    assert outcome.sent == 0
    assert transport.sent == []


def test_mapped_camera_sends(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    transport = LogTransport()
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
        )
    )
    assert outcome.sent == 1
    assert outcome.frigate_camera == "front_door"
    assert len(transport.sent) == 1


# -- dedup ----------------------------------------------------------------


def test_dedup_window_drops_second_ring(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    transport = LogTransport()

    kwargs = dict(
        protect_camera_id="cam-1",
        protect_event_id="evt-1",
        cameras={"cam-1": "front_door"},
        conn=conn,
        transport=transport,
        frigate_base_url="http://frigate.test:5000",
        external_base_url="",
        situation_handle_ttl_s=3600.0,
        ring_dedup_seconds=20.0,
    )
    first = asyncio.run(doorbell.handle_ring(**kwargs, now=1000.0))
    second = asyncio.run(doorbell.handle_ring(**kwargs, now=1005.0))
    assert first.sent == 1
    assert second.sent == 0
    assert second.skipped_dedup is True
    assert len(transport.sent) == 1


def test_dedup_window_expires(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    transport = LogTransport()
    kwargs = dict(
        protect_camera_id="cam-1",
        protect_event_id="evt-1",
        cameras={"cam-1": "front_door"},
        conn=conn,
        transport=transport,
        frigate_base_url="http://frigate.test:5000",
        external_base_url="",
        situation_handle_ttl_s=3600.0,
        ring_dedup_seconds=20.0,
    )
    first = asyncio.run(doorbell.handle_ring(**kwargs, now=1000.0))
    second = asyncio.run(doorbell.handle_ring(**kwargs, now=1030.0))
    assert first.sent == 1
    assert second.sent == 1


# -- snoozes ----------------------------------------------------------------


def test_global_snooze_mutes_ring(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    store.set_snooze(conn, apns_token="tok-1", scope="global", until_epoch=99999999999.0)
    transport = LogTransport()
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
        )
    )
    assert outcome.sent == 0
    assert transport.sent == []


def test_camera_scoped_snooze_does_not_mute_ring(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn)
    store.set_snooze(
        conn, apns_token="tok-1", scope="camera:front_door", until_epoch=99999999999.0
    )
    transport = LogTransport()
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
        )
    )
    assert outcome.sent == 1


# -- doorbell_rings opt-out --------------------------------------------------


def test_doorbell_rings_disabled_skips_device(tmp_path: Path) -> None:
    import asyncio

    conn = _conn(tmp_path)
    _register(conn, doorbell_rings=False)
    transport = LogTransport()
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
        )
    )
    assert outcome.sent == 0
    assert transport.sent == []


# -- payload shape ------------------------------------------------------


def test_build_ring_payload_shape() -> None:
    payload = doorbell.build_ring_payload(
        frigate_camera="front_door",
        media="http://sidecar.test:5001/v1/push/thumbnail/h_abc123",
        protect_event_id="evt-1",
        device_timezone="",
        now=1_700_000_000.0,
    )
    assert payload["aps"]["alert"]["title"] == "Someone's at the door"
    assert payload["aps"]["alert"]["body"] == "Front Door"
    assert payload["aps"]["sound"] == "default"
    assert payload["aps"]["interruption-level"] == "time-sensitive"
    assert payload["aps"]["category"] == "doorbell.ring"
    assert payload["aps"]["thread-id"] == "doorbell"
    assert payload["media"] == "http://sidecar.test:5001/v1/push/thumbnail/h_abc123"
    assert payload["doorbell"] == {
        "camera": "front_door",
        "protect_event_id": "evt-1",
        "ts": 1_700_000_000.0,
    }


def test_build_ring_payload_uses_media_field_like_card_pipeline() -> None:
    """Same top-level `media` field/shape `push.delivery.build_card_payload`
    uses (a full redemption URL string), so the existing NSE attaches the
    image unchanged."""
    from marcellus.push.cards import Card
    from marcellus.push.delivery import build_card_payload

    card = Card(
        card_key="k1", level="notify", created_at=0.0, updated_at=0.0, state_since_at=0.0,
    )
    card_payload = build_card_payload(
        card, "create", sound=True, subject_kind="person", place_class="entry",
        camera="front_door", zone_name="", glyph="", primary="x", secondary="y",
        event_ts=0.0, media="http://example.test/media.jpg",
    )
    ring_payload = doorbell.build_ring_payload(
        frigate_camera="front_door", media="http://example.test/media.jpg",
        protect_event_id="", now=0.0,
    )
    assert "media" in card_payload
    assert "media" in ring_payload
    assert type(card_payload["media"]) is type(ring_payload["media"])


def test_build_ring_payload_with_timezone() -> None:
    payload = doorbell.build_ring_payload(
        frigate_camera="front_door", media=None, protect_event_id="",
        device_timezone="America/Los_Angeles", now=1_700_000_000.0,
    )
    assert "·" in payload["aps"]["alert"]["body"]
