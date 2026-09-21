from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx

from marcellus.config import PushSection
from marcellus.push.engine import PushEngine
from marcellus.push.mqtt import MqttReviewSubscriber, backfill_since, compute_backoff
from marcellus.push.stats import STATS
from marcellus.push.transport import LogTransport


class _RecordingHandler(logging.Handler):
    """Attached directly to the originating logger so the assertions don't
    depend on root-logger propagation or on how `caplog` is configured."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


def test_compute_backoff_grows_and_caps():
    assert compute_backoff(0, base=2.0, cap=60.0) == 2.0
    assert compute_backoff(1, base=2.0, cap=60.0) == 4.0
    assert compute_backoff(2, base=2.0, cap=60.0) == 8.0
    assert compute_backoff(10, base=2.0, cap=60.0) == 60.0


def _subscriber(tmp_path: Path) -> tuple[MqttReviewSubscriber, PushEngine]:
    settings = PushSection()
    engine = PushEngine(
        db_path=str(tmp_path / "sidecar.db"),
        transport=LogTransport(),
        server_id="s1",
    )
    sub = MqttReviewSubscriber(settings, engine, frigate_base_url="http://frigate.test:5000")
    return sub, engine


def test_on_message_reviews_dispatches_to_engine(tmp_path: Path) -> None:
    sub, engine = _subscriber(tmp_path)
    loop = asyncio.new_event_loop()
    sub._loop = loop
    payload = {
        "type": "new",
        "after": {
            "id": "r1",
            "camera": "doorbell",
            "severity": "alert",
            "data": {"objects": ["person"], "detections": []},
        },
    }
    msg = SimpleNamespace(topic="frigate/reviews", payload=json.dumps(payload).encode())
    sub.on_message(None, None, msg)
    # Drain the scheduled coroutine.
    loop.run_until_complete(asyncio.sleep(0.05))
    loop.close()
    assert sub.last_seen > 0


def test_on_message_malformed_json_does_not_raise(tmp_path: Path) -> None:
    sub, _ = _subscriber(tmp_path)
    sub._loop = asyncio.new_event_loop()
    msg = SimpleNamespace(topic="frigate/reviews", payload=b"{not json")
    sub.on_message(None, None, msg)  # should not raise
    sub._loop.close()


async def test_reviews_dispatch_exception_is_logged_not_swallowed(tmp_path: Path) -> None:
    """A push that raises partway through must leave a trace. Pre-Wave-2B this
    covered the fire-and-forget `run_coroutine_threadsafe` future; the queue
    consumer (`_consume`) now owns this contract instead -- it must log and
    keep consuming rather than let the handler's exception vanish or kill the
    consumer task."""
    STATS.reset()
    sub, engine = _subscriber(tmp_path)

    async def _boom(payload: dict) -> int:
        raise RuntimeError("kaboom")

    engine.handle_review_payload = _boom  # type: ignore[method-assign]

    sub._loop = asyncio.get_running_loop()
    sub.start_consumer()
    handler = _RecordingHandler()
    mqtt_logger = logging.getLogger("marcellus.push.mqtt")
    mqtt_logger.addHandler(handler)
    mqtt_logger.setLevel(logging.ERROR)
    try:
        payload = {
            "type": "new",
            "after": {
                "id": "r1",
                "camera": "doorbell",
                "severity": "alert",
                "data": {"objects": ["person"], "detections": []},
            },
        }
        msg = SimpleNamespace(topic="frigate/reviews", payload=json.dumps(payload).encode())
        sub.on_message(None, None, msg)
        await sub.drain()
    finally:
        mqtt_logger.removeHandler(handler)
        sub.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await sub._consumer_task
    assert "mqtt consumer handler failed for reviews frame" in handler.text
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in handler.records)
    assert STATS.get("mqtt.consumer.errors") == 1


async def test_events_dispatch_exception_is_logged_not_swallowed(tmp_path: Path) -> None:
    """Same contract as the reviews test above, for the `frigate/events`
    frame kind."""
    STATS.reset()
    sub, engine = _subscriber(tmp_path)

    async def _boom(payload: dict) -> int:
        raise RuntimeError("kaboom")

    engine.handle_object_payload = _boom  # type: ignore[method-assign]

    sub._loop = asyncio.get_running_loop()
    sub.start_consumer()
    handler = _RecordingHandler()
    mqtt_logger = logging.getLogger("marcellus.push.mqtt")
    mqtt_logger.addHandler(handler)
    mqtt_logger.setLevel(logging.ERROR)
    try:
        payload = {"after": {"camera": "doorbell", "id": "t1"}, "type": "new"}
        msg = SimpleNamespace(topic="frigate/events", payload=json.dumps(payload).encode())
        sub.on_message(None, None, msg)
        await sub.drain()
    finally:
        mqtt_logger.removeHandler(handler)
        sub.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await sub._consumer_task
    assert "mqtt consumer handler failed for events frame" in handler.text
    assert any(r.exc_info and "kaboom" in str(r.exc_info[1]) for r in handler.records)
    assert STATS.get("mqtt.consumer.errors") == 1


def test_available_offline_marks_frigate_offline(tmp_path: Path) -> None:
    sub, _ = _subscriber(tmp_path)
    msg = SimpleNamespace(topic="frigate/available", payload=b"offline")
    sub.on_message(None, None, msg)
    assert sub.frigate_online is False


def test_available_online_marks_frigate_online(tmp_path: Path) -> None:
    sub, _ = _subscriber(tmp_path)
    sub.frigate_online = False
    msg = SimpleNamespace(topic="frigate/available", payload=b"online")
    sub.on_message(None, None, msg)
    assert sub.frigate_online is True


def test_is_stale(tmp_path: Path) -> None:
    sub, _ = _subscriber(tmp_path)
    sub.settings.offline_silence_s = 60.0
    sub.last_seen = 1000.0
    assert not sub.is_stale(now=1010.0)
    assert sub.is_stale(now=1061.0)


async def test_backfill_since_dispatches_matching_events(tmp_path: Path) -> None:
    from marcellus import db
    from marcellus.push import store

    db_path = tmp_path / "sidecar.db"
    conn = db.open_sidecar(db_path)
    store.upsert_device(
        conn,
        apns_token="tok1",
        bundle_id="com.x",
        environment="sandbox",
        cameras=["doorbell"],
        min_severity="detection",
    )
    conn.commit()
    conn.close()

    transport = LogTransport()
    engine = PushEngine(db_path=str(db_path), transport=transport, server_id="s1")
    from marcellus.config import PushSection

    engine.push_config = PushSection(delivery_enabled=True)

    seen: list = []
    engine.on_review = seen.append

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": "ev1", "camera": "doorbell", "label": "person"},
                {"id": "ev2", "camera": "garden", "label": "car"},
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notified = await backfill_since(
        engine,
        frigate_base_url="http://frigate.test:5000",
        after=0.0,
        client=client,
    )
    # Both events create cards (card pipeline evaluates every event). No zones
    # → street place class → log level → no push, but the card is still mutated.
    assert notified == 2
    # Backfilled events are object-tracking events (/api/events), not review
    # segments -- they must be marked synthetic so PushEngine.handle_event
    # skips the on_review (encounters) hook for them.
    assert seen == []
    await client.aclose()


async def test_backfill_since_handles_request_error(tmp_path: Path) -> None:
    engine = PushEngine(
        db_path=str(tmp_path / "sidecar.db"), transport=LogTransport(), server_id="s1"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("frigate is down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notified = await backfill_since(
        engine,
        frigate_base_url="http://frigate.test:5000",
        after=0.0,
        client=client,
    )
    assert notified == 0
    await client.aclose()
