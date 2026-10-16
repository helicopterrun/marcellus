"""ExternalMqttHub tests: no broker; a fake paho client and direct callbacks."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from marcellus.external.mqtt_hub import ExternalMqttHub
from marcellus.external.types import Availability, Signal


class FakeSource:
    kind = "fake"
    name = "fake"
    base_url = "http://x"
    max_connections = 1
    subjects = ()
    media = ()

    def __init__(self, id: str, topics: list[str]) -> None:
        self.id = id
        self._topics = topics
        self.seen: list[tuple[str, bytes]] = []
        self.raise_on: bytes | None = None

    def mqtt_topics(self) -> list[str]:
        return self._topics

    def parse_mqtt(self, topic: str, payload: bytes) -> Signal | None:
        if payload == self.raise_on:
            raise ValueError("boom")
        self.seen.append((topic, payload))
        return Availability(self.id, True)


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, hub: ExternalMqttHub) -> None:
        self.hub = hub
        self.subscribed: list[tuple[str, int]] = []
        self.connected = False
        self.stopped = False
        self.disconnected = False
        FakeClient.instances.append(self)

    def connect(self, host: str, port: int) -> None:
        self.connected = True
        self.hub._on_connect(self, None, None, 0, None)

    def loop_start(self) -> None: ...

    def loop_stop(self) -> None:
        self.stopped = True

    def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))


def make_hub(sources: list[Any], on_signal: Any = None, **kw: Any) -> ExternalMqttHub:
    async def _noop(_s: Signal) -> None: ...

    return ExternalMqttHub(
        sources,
        host="h",
        port=1883,
        username="u",
        password="s3cret-pw",
        client_id="cid",
        on_signal=on_signal or _noop,
        backoff_base=0.01,
        backoff_cap=0.02,
        **kw,
    )


def msg(topic: str, payload: bytes) -> Any:
    return SimpleNamespace(topic=topic, payload=payload)


async def start_plumbing(hub: ExternalMqttHub) -> asyncio.Task[None]:
    hub._loop = asyncio.get_running_loop()
    hub._queue = asyncio.Queue(maxsize=hub._queue_max)
    return asyncio.get_running_loop().create_task(hub._consume())


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_routing_wildcard_exact_and_nonmatching() -> None:
    a = FakeSource("a", ["dev/a/#"])
    b = FakeSource("b", ["dev/b/status"])
    got: list[Signal] = []

    async def on_signal(s: Signal) -> None:
        got.append(s)

    hub = make_hub([a, b], on_signal)
    task = await start_plumbing(hub)
    hub._on_message(None, None, msg("dev/a/x/y", b"1"))
    hub._on_message(None, None, msg("dev/b/status", b"2"))
    hub._on_message(None, None, msg("dev/b/other", b"3"))
    hub._on_message(None, None, msg("nope", b"4"))
    await settle()
    assert a.seen == [("dev/a/x/y", b"1")]
    assert b.seen == [("dev/b/status", b"2")]
    assert len(got) == 2
    assert hub.status()["messages"] == 2
    task.cancel()


async def test_parse_error_counted_consumer_continues() -> None:
    a = FakeSource("a", ["t"])
    a.raise_on = b"bad"
    hub = make_hub([a])
    task = await start_plumbing(hub)
    hub._on_message(None, None, msg("t", b"bad"))
    hub._on_message(None, None, msg("t", b"good"))
    await settle()
    assert hub.status()["parse_errors"] == 1
    assert a.seen == [("t", b"good")]
    task.cancel()


async def test_on_signal_exception_does_not_kill_consumer() -> None:
    a = FakeSource("a", ["t"])
    calls = 0

    async def on_signal(_s: Signal) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("x")

    hub = make_hub([a], on_signal)
    task = await start_plumbing(hub)
    hub._on_message(None, None, msg("t", b"1"))
    hub._on_message(None, None, msg("t", b"2"))
    await settle()
    assert calls == 2
    assert not task.done()
    task.cancel()


async def test_queue_overflow_counts_dropped() -> None:
    a = FakeSource("a", ["t"])
    hub = make_hub([a], queue_max=2)
    hub._loop = asyncio.get_running_loop()
    hub._queue = asyncio.Queue(maxsize=2)  # no consumer: stays full
    for i in range(5):
        hub._on_message(None, None, msg("t", bytes([i])))
    await settle()
    assert hub.status()["dropped"] == 3
    assert hub._queue.qsize() == 2


async def test_run_idle_without_topics() -> None:
    hub = make_hub([FakeSource("a", [])])
    await asyncio.wait_for(hub.run(), 1)
    assert hub.status()["state"] == "idle"
    assert hub.status()["connected"] is False


async def test_subscribe_all_topics_on_connect_and_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeClient.instances = []
    a = FakeSource("a", ["x/#", "y"])
    b = FakeSource("b", ["z", "y"])
    hub = make_hub([a, b])
    monkeypatch.setattr(hub, "_build_client", lambda: FakeClient(hub))
    task = asyncio.get_running_loop().create_task(hub.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if FakeClient.instances and FakeClient.instances[0].subscribed:
            break
    first = FakeClient.instances[0]
    assert sorted(first.subscribed) == [("x/#", 1), ("y", 1), ("z", 1)]
    assert hub.status()["state"] == "connected"
    first.connected = False  # broker drop
    for _ in range(300):
        await asyncio.sleep(0.01)
        if len(FakeClient.instances) > 1 and FakeClient.instances[1].subscribed:
            break
    assert len(FakeClient.instances) > 1
    assert sorted(FakeClient.instances[1].subscribed) == [("x/#", 1), ("y", 1), ("z", 1)]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_cancellation_stops_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances = []
    hub = make_hub([FakeSource("a", ["t"])])
    monkeypatch.setattr(hub, "_build_client", lambda: FakeClient(hub))
    task = asyncio.get_running_loop().create_task(hub.run())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if FakeClient.instances and FakeClient.instances[0].connected:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    c = FakeClient.instances[0]
    assert c.stopped and c.disconnected
    assert hub.status()["connected"] is False


async def test_status_has_no_password() -> None:
    hub = make_hub([FakeSource("a", ["t"])])
    s = hub.status()
    assert "s3cret-pw" not in repr(s)
    assert set(s) == {
        "state",
        "connected",
        "last_message_at",
        "last_connect_error",
        "dropped",
        "parse_errors",
        "messages",
    }
