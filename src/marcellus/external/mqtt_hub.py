"""MQTT ingest for external event sources (one shared broker connection).

Why this is not `push.mqtt.MqttReviewSubscriber`: the hub runs even when push
is disabled, its staleness model differs (sources report their own
availability; there is no Frigate silence window or backfill), and a source
parser bug must never be able to take push down. So it owns a separate paho
client with its own queue and consumer.

Shape mirrors the push subscriber: paho's network thread (`loop_start`)
hands messages to the asyncio loop with `call_soon_threadsafe` into a bounded
queue; one consumer coroutine parses and dispatches. The paho thread never
blocks -- on overflow the message is dropped and counted.

Credentials are never logged and never appear in `status()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from marcellus.external.types import ExternalEventSource, Signal
from marcellus.push.mqtt import compute_backoff

if TYPE_CHECKING:  # pragma: no cover - typing only
    import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class ExternalMqttHub:
    def __init__(
        self,
        sources: Sequence[ExternalEventSource],
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        client_id: str,
        on_signal: Callable[[Signal], Awaitable[None]],
        queue_max: int = 1000,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
    ) -> None:
        self._sources = list(sources)
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._client_id = client_id
        self._on_signal = on_signal
        self._queue_max = queue_max
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap

        self._topics: list[str] = []
        seen: set[str] = set()
        for src in self._sources:
            for t in src.mqtt_topics():
                if t not in seen:
                    seen.add(t)
                    self._topics.append(t)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[ExternalEventSource, str, bytes]] | None = None
        self._client: mqtt.Client | None = None
        self._state = "idle" if not self._topics else "disconnected"
        self._last_message_at: float | None = None
        self._last_connect_error: str | None = None
        self._dropped = 0
        self._parse_errors = 0
        self._messages = 0

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "connected": self._state == "connected",
            "last_message_at": self._last_message_at,
            "last_connect_error": self._last_connect_error,
            "dropped": self._dropped,
            "parse_errors": self._parse_errors,
            "messages": self._messages,
        }

    # ------------------------------------------------------------- paho thread

    def _on_connect(self, client: Any, _u: Any, _f: Any, _rc: Any, _props: Any = None) -> None:
        # Re-subscribe on every (re)connect: broker sessions are not assumed.
        for topic in self._topics:
            client.subscribe(topic, qos=1)
        self._state = "connected"
        self._last_connect_error = None
        logger.info(
            "external mqtt: connected to %s:%s as %s (%d topic filter(s))",
            self._host,
            self._port,
            self._client_id,
            len(self._topics),
        )

    def _on_disconnect(self, *_args: Any) -> None:
        if self._state == "connected":
            self._state = "disconnected"

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        """Runs on paho's thread: route and hand off, never block."""
        from paho.mqtt.client import topic_matches_sub

        topic: str = msg.topic
        payload: bytes = bytes(msg.payload)
        loop = self._loop
        if loop is None:
            return
        for src in self._sources:
            if any(topic_matches_sub(f, topic) for f in src.mqtt_topics()):
                try:
                    loop.call_soon_threadsafe(self._enqueue, src, topic, payload)
                except RuntimeError:  # loop closed during shutdown
                    return

    # --------------------------------------------------------------- event loop

    def _enqueue(self, src: ExternalEventSource, topic: str, payload: bytes) -> None:
        queue = self._queue
        if queue is None:
            return
        self._last_message_at = time.time()
        self._messages += 1
        try:
            queue.put_nowait((src, topic, payload))
        except asyncio.QueueFull:
            self._dropped += 1

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            src, topic, payload = await self._queue.get()
            try:
                signal = src.parse_mqtt(topic, payload)
            except Exception:
                self._parse_errors += 1
                logger.exception("external mqtt: parse failed for %s on %s", src.id, topic)
                continue
            if signal is None:
                continue
            try:
                await self._on_signal(signal)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("external mqtt: on_signal failed for %s", src.id)

    def _build_client(self) -> mqtt.Client:
        import paho.mqtt.client as mqtt_client

        client = mqtt_client.Client(
            client_id=self._client_id,
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]  # paho re-exports it without __all__
        )
        if self._username:
            client.username_pw_set(self._username, self._password)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    async def run(self) -> None:
        if not self._topics:
            self._state = "idle"
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self._queue_max)
        consumer = self._loop.create_task(self._consume())
        attempt = 0
        try:
            while True:
                client = self._build_client()
                self._client = client
                self._state = "connecting"
                try:
                    await asyncio.to_thread(client.connect, self._host, self._port)
                    client.loop_start()
                    # CONNACK lands on paho's thread after connect() returns;
                    # give the handshake a bounded moment (see push/mqtt.py).
                    for _ in range(100):
                        if client.is_connected():
                            break
                        await asyncio.sleep(0.1)
                    if client.is_connected():
                        attempt = 0
                    while client.is_connected():
                        await asyncio.sleep(1.0)
                except (OSError, ConnectionError) as exc:
                    self._last_connect_error = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "external mqtt: connect to %s:%s failed: %s",
                        self._host,
                        self._port,
                        exc,
                    )
                finally:
                    self._state = "disconnected"
                    client.loop_stop()
                    with contextlib.suppress(Exception):
                        client.disconnect()
                await asyncio.sleep(compute_backoff(attempt, self._backoff_base, self._backoff_cap))
                attempt += 1
        finally:
            self._state = "disconnected"
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
