"""Bounded MQTT queue + single ordered consumer (Wave 2B §1).

Uses a fake engine (records calls in order) and a `SimpleNamespace` stand-in
for `PushSection` -- `mqtt_queue_max` has a `ge=100` pydantic constraint on
the real config model, but these tests need small values (3) to exercise
overflow/eviction without a 100-item fixture, so a real `PushSection` won't
do here.

Tests drive `MqttReviewSubscriber._enqueue` directly (bypassing the paho
callbacks' JSON parsing, already covered by `test_push_mqtt.py`) so each test
is about queue/consumer mechanics only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from marcellus.push import mqtt
from marcellus.push.mqtt import MqttReviewSubscriber
from marcellus.push.stats import STATS


class _RecordingHandler(logging.Handler):
    """Attached directly to the originating logger; see test_push_mqtt.py."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


class FakeEngine:
    """Records every dispatch call, in order, instead of touching a DB."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def handle_object_payload(self, payload: dict) -> int:
        self.calls.append(("events", payload))
        return 0

    async def handle_review_payload(self, payload: dict) -> int:
        self.calls.append(("reviews", payload))
        return 0

    def reset_tracks(self) -> None:
        self.calls.append(("reset", None))


def _fake_settings(queue_max: int = 2000) -> SimpleNamespace:
    return SimpleNamespace(
        capture_enabled=False,
        mqtt_queue_max=queue_max,
        mqtt_topic_reviews="frigate/reviews",
        mqtt_topic_events="frigate/events",
        mqtt_topic_available="frigate/available",
        dwell_source="events",
    )


def _subscriber(queue_max: int = 2000) -> tuple[MqttReviewSubscriber, FakeEngine]:
    engine = FakeEngine()
    settings = _fake_settings(queue_max)
    sub = MqttReviewSubscriber(settings, engine, frigate_base_url="http://frigate.test:5000")
    return sub, engine


def _events_item(track_id: str, msg_type: str = "new") -> Any:
    return mqtt._QueueItem(
        kind="events",
        payload={"after": {"camera": "doorbell", "id": track_id}, "type": msg_type},
        terminal=msg_type not in ("new", "update"),
        enqueued_at=time.time(),
    )


def _reviews_item(review_id: str) -> Any:
    return mqtt._QueueItem(
        kind="reviews",
        payload={
            "after": {"camera": "doorbell", "id": review_id, "severity": "alert",
                      "data": {"objects": ["person"], "detections": []}},
            "type": "new",
        },
        terminal=True,
        enqueued_at=time.time(),
    )


def _reset_item() -> Any:
    return mqtt._QueueItem(kind="reset", payload=None, terminal=True, enqueued_at=time.time())


async def _stop_and_await(sub: MqttReviewSubscriber) -> None:
    sub.stop()
    with contextlib.suppress(asyncio.CancelledError):
        if sub._consumer_task is not None:
            await sub._consumer_task


async def test_consumer_dispatches_in_enqueue_order() -> None:
    STATS.reset()
    sub, engine = _subscriber()
    sub._loop = asyncio.get_running_loop()
    sub.start_consumer()

    for i in range(5):
        sub._enqueue(_events_item(f"e{i}"))
    sub._enqueue(_reviews_item("r1"))
    await sub.drain()
    await _stop_and_await(sub)

    kinds = [c[0] for c in engine.calls]
    assert kinds == ["events"] * 5 + ["reviews"]
    event_ids = [c[1]["after"]["id"] for c in engine.calls if c[0] == "events"]
    assert event_ids == [f"e{i}" for i in range(5)]


async def test_overflow_drops_update_and_eviction_admits_terminal() -> None:
    STATS.reset()
    sub, _engine = _subscriber(queue_max=3)
    sub._loop = asyncio.get_running_loop()
    # Consumer deliberately not started yet: inspect the deque directly.

    sub._enqueue(_events_item("t1", "update"))
    sub._enqueue(_events_item("t2", "update"))
    sub._enqueue(_events_item("t3", "update"))
    assert len(sub._deque) == 3

    # A 4th "update" with the queue full is dropped, never enqueued.
    sub._enqueue(_events_item("t4", "update"))
    assert len(sub._deque) == 3
    assert [it.payload["after"]["id"] for it in sub._deque] == ["t1", "t2", "t3"]
    assert STATS.get("mqtt.dropped.overflow") == 1
    assert STATS.get("mqtt.queue.depth") == 3

    # A terminal "end" frame instead evicts the oldest non-terminal item.
    sub._enqueue(_events_item("tend", "end"))
    assert len(sub._deque) == 3
    assert [it.payload["after"]["id"] for it in sub._deque] == ["t2", "t3", "tend"]
    assert STATS.get("mqtt.dropped.overflow") == 1  # unchanged -- not a drop

    # A review is terminal too, and evicts the same way.
    sub._enqueue(_reviews_item("rev1"))
    assert len(sub._deque) == 3
    assert [it.payload["after"]["id"] for it in sub._deque] == ["t3", "tend", "rev1"]
    assert STATS.get("mqtt.dropped.overflow") == 1


async def test_reset_item_ordered_between_queued_frames() -> None:
    STATS.reset()
    sub, engine = _subscriber()
    sub._loop = asyncio.get_running_loop()
    sub.start_consumer()

    sub._enqueue(_events_item("before1"))
    sub._enqueue(_events_item("before2"))
    sub._enqueue(_reset_item())
    sub._enqueue(_events_item("after1"))
    await sub.drain()
    await _stop_and_await(sub)

    kinds = [c[0] for c in engine.calls]
    reset_idx = kinds.index("reset")
    assert kinds[:reset_idx] == ["events", "events"]
    assert kinds[reset_idx + 1:] == ["events"]
    event_ids = [c[1]["after"]["id"] for c in engine.calls if c[0] == "events"]
    assert event_ids == ["before1", "before2", "after1"]


async def test_handler_exception_is_logged_and_consumer_keeps_going() -> None:
    STATS.reset()
    sub, engine = _subscriber()
    sub._loop = asyncio.get_running_loop()

    orig_handle = engine.handle_object_payload

    async def _flaky(payload: dict) -> int:
        if payload["after"]["id"] == "boom":
            raise RuntimeError("kaboom")
        return await orig_handle(payload)

    engine.handle_object_payload = _flaky  # type: ignore[method-assign]

    sub.start_consumer()
    handler = _RecordingHandler()
    mqtt_logger = logging.getLogger("marcellus.push.mqtt")
    mqtt_logger.addHandler(handler)
    mqtt_logger.setLevel(logging.ERROR)
    try:
        sub._enqueue(_events_item("boom"))
        sub._enqueue(_events_item("ok"))
        await sub.drain()
    finally:
        mqtt_logger.removeHandler(handler)
        await _stop_and_await(sub)

    assert STATS.get("mqtt.consumer.errors") == 1
    assert "mqtt consumer handler failed for events frame" in handler.text
    event_ids = [c[1]["after"]["id"] for c in engine.calls if c[0] == "events"]
    assert event_ids == ["ok"]  # "boom" raised before recording; consumer moved on


async def test_stop_cancels_consumer_cleanly() -> None:
    sub, _engine = _subscriber()
    sub._loop = asyncio.get_running_loop()
    task = sub.start_consumer()
    sub.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
