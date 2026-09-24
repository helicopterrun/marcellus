from __future__ import annotations

from marcellus.push.unifi_protect import (
    RingEvent,
    _parse_ring_event,
    compute_backoff,
)


def test_compute_backoff_grows_and_caps() -> None:
    assert compute_backoff(0, base=2.0, cap=60.0) == 2.0
    assert compute_backoff(1, base=2.0, cap=60.0) == 4.0
    assert compute_backoff(10, base=2.0, cap=60.0) == 60.0


def test_parse_wrapped_ring_event() -> None:
    payload = {"type": "add", "item": {"type": "ring", "device": "cam-1", "id": "evt-1"}}
    ring = _parse_ring_event(payload)
    assert ring == RingEvent(
        protect_camera_id="cam-1", protect_event_id="evt-1", raw_type="wrapped"
    )


def test_parse_flat_ring_event() -> None:
    payload = {"type": "ring", "device": "cam-2"}
    ring = _parse_ring_event(payload)
    assert ring is not None
    assert ring.protect_camera_id == "cam-2"
    assert ring.raw_type == "flat"


def test_parse_wrapped_non_ring_item_ignored() -> None:
    payload = {"type": "add", "item": {"type": "motion", "device": "cam-1"}}
    assert _parse_ring_event(payload) is None


def test_parse_unknown_type_ignored() -> None:
    assert _parse_ring_event({"type": "remove", "item": {"type": "ring", "device": "x"}}) is None


def test_parse_missing_device_ignored() -> None:
    assert _parse_ring_event({"type": "ring"}) is None
    assert _parse_ring_event({"type": "add", "item": {"type": "ring"}}) is None


def test_parse_junk_ignored() -> None:
    assert _parse_ring_event({}) is None
    assert _parse_ring_event({"unrelated": True}) is None


def test_parse_non_dict_payload() -> None:
    assert _parse_ring_event([1, 2, 3]) is None  # type: ignore[arg-type]
    assert _parse_ring_event("not a dict") is None  # type: ignore[arg-type]
    assert _parse_ring_event(None) is None  # type: ignore[arg-type]


def test_parse_update_frame_ignored() -> None:
    # Protect follows each "add" with several "update" frames for the same
    # event; acting on them would re-ring the phone.
    payload = {"type": "update", "item": {"type": "ring", "device": "cam-1"}}
    assert _parse_ring_event(payload) is None
