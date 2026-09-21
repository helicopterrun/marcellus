"""MQTT `frigate/reviews` subscriber, with offline detection and backfill.

Event source is MQTT, not `/api/events` polling (spec's "Architecture at a
glance" -- already settled upstream in PROJECT_PLAN §5.2/§12.5/§12). This
module owns the paho-mqtt connection; `push.engine.PushEngine` owns what
happens once a message arrives, so the two are testable independently --
this file's own tests exercise `on_message`/backoff/backfill logic directly,
never a real broker.

Frigate-offline handling (spec §5): the plain-text (not JSON) `offline`
payload on `frigate/available`, or `offline_silence_s` of broker silence,
means "no alerts can fire, not that devices are stale" -- the sidecar must
not treat that window as needing feedback-loop cleanup. On reconnect it
back-fills the gap via `GET /api/events?after=<lastSeen-backfill_lookback_s>`
before resuming live pushes (§12.6's stale/live/recovering model, reused).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from marcellus.push.log_context import reset_push_context, set_push_context
from marcellus.push.models import ReviewEvent
from marcellus.push.stats import STATS

if TYPE_CHECKING:  # pragma: no cover - typing only
    import paho.mqtt.client as mqtt

    from marcellus.config import PushSection
    from marcellus.push.engine import PushEngine

logger = logging.getLogger(__name__)

#: Overflow-drop warning is rate limited module-wide (spec: "module-level
#: last-warn timestamp"), not per-subscriber -- there is only ever one
#: subscriber per process, and this keeps the throttle simple.
_last_drop_warn_at = 0.0
_dropped_since_warn = 0
_DROP_WARN_INTERVAL_S = 30.0


@dataclass
class _QueueItem:
    kind: Literal["events", "reviews", "reset"]
    payload: dict[str, Any] | None
    #: Never dropped on overflow: every reviews/reset item, plus an events
    #: item whose `type` isn't "new"/"update" (i.e. "end" -- see
    #: `_terminal_events_item`).
    terminal: bool
    enqueued_at: float


def compute_backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff, capped. `attempt` is 0-indexed (first retry ->
    `attempt=0`)."""
    return min(cap, base * (2.0**attempt))


async def backfill_since(
    engine: PushEngine,
    *,
    frigate_base_url: str,
    after: float,
    client: httpx.AsyncClient | None = None,
    timeout: float = 10.0,
    staleness_s: float = 300.0,
) -> int:
    """Back-fill any alert-worthy events Frigate recorded during a broker
    blip, evaluating each against registered devices' filters before
    resuming live pushes (spec §5, "MQTT broker unreachable").

    `/api/events` (unlike `frigate/reviews`) has no `severity` field -- there
    is no live review-item concept to replay, only finished tracked-object
    events. Resolution: treat every backfilled event as `severity="alert"`
    (the conservative choice -- a missed alert during an outage is worse than
    an extra one) with `labels=(event.label,)`, so a device's camera/label
    filters still apply; `min_severity="detection"` devices also match
    (alert only ever admits detection-tier subscribers by the same rank
    check used for live events).
    """
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    notified = 0
    try:
        url = f"{frigate_base_url.rstrip('/')}/api/events"
        try:
            resp = await client.get(url, params={"after": after})
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("push: backfill request to %s failed", url)
            return 0
        try:
            events = resp.json()
        except ValueError:
            events = []
        if not isinstance(events, list):
            return 0
        now = time.time()
        for raw in events:
            if not isinstance(raw, dict):
                continue
            camera = raw.get("camera")
            label = raw.get("label")
            event_id = raw.get("id")
            if not camera or not event_id:
                continue
            start_time = raw.get("start_time")
            if isinstance(start_time, (int, float)) and (now - start_time) > staleness_s:
                continue
            event = ReviewEvent(
                review_id=str(event_id),
                camera=str(camera),
                severity="alert",
                labels=(str(label),) if label else (),
                msg_type="new",
                event_id=str(event_id),
                synthetic=True,
            )
            notified += await engine.handle_event(event)
        return notified
    finally:
        if own_client:
            await client.aclose()


class MqttReviewSubscriber:
    """Owns the paho-mqtt connection and bridges its callbacks onto the
    asyncio loop the engine runs on.

    paho-mqtt's network loop runs on its own thread (`loop_start`); callbacks
    therefore hand off to the event loop via `call_soon_threadsafe` rather
    than calling engine coroutines directly from the paho thread.
    """

    def __init__(
        self,
        settings: PushSection,
        engine: PushEngine,
        *,
        frigate_base_url: str,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.frigate_base_url = frigate_base_url
        self._loop = loop
        self.last_seen: float = time.time()
        self.last_review_at: float | None = None
        self.frigate_online: bool = True
        self._client: mqtt.Client | None = None
        self._stopped = False
        # Flight recorder (capture.py): every consumed reviews/events message,
        # so real situations replay exactly. Never on the failure path — a
        # capture error logs once and the pipeline continues.
        self._capture: MqttCapture | None = None
        if settings.capture_enabled:
            from marcellus.push.capture import MqttCapture

            capture_path = settings.capture_path or ""
            if not capture_path:
                capture_path = str(Path(settings.push_settings_path).parent / "mqtt-capture.jsonl")
            self._capture = MqttCapture(capture_path, max_bytes=settings.capture_max_bytes)

        # Bounded queue with one ordered consumer (spec §1). A plain
        # `collections.deque` + `asyncio.Event`, not `asyncio.Queue`: a
        # terminal/review/reset item that arrives with the queue full must
        # still be enqueued (evicting an oldest non-terminal item, or -- if
        # nothing is evictable -- exceeding maxsize outright), and
        # `asyncio.Queue.put_nowait` has no way to do either; a deque has no
        # size limit of its own; the accounting below is what enforces
        # `mqtt_queue_max`. Every mutation happens as a callback on the
        # asyncio loop (via `call_soon_threadsafe` from the paho thread, or
        # directly from `_consume`/`on_message` once already on the loop), so
        # there is never a true data race despite the multi-threaded source.
        self._deque: deque[_QueueItem] = deque()
        self._wake: asyncio.Event = asyncio.Event()
        self._consumer_task: asyncio.Task[None] | None = None
        #: True while the consumer is inside a dispatch -- `drain()` (tests)
        #: waits for this *and* an empty deque, so it doesn't return between
        #: "popped the item" and "finished awaiting the handler".
        self._dispatching = False

    def _terminal_events_item(self, payload: dict[str, Any]) -> bool:
        """Only a `new`/`update` object frame is droppable; `end` (and
        anything else this topic might send) is treated conservatively as
        terminal so it's never the one evicted."""
        return payload.get("type") not in ("new", "update")

    def _enqueue(self, item: _QueueItem) -> None:
        """Runs on the event loop -- scheduled via `call_soon_threadsafe`
        from the paho thread (or called directly once already on the loop,
        e.g. from `on_message` in tests). See §1: drop a non-terminal
        overflow item, or evict the oldest non-terminal item to make room for
        a terminal one."""
        global _last_drop_warn_at, _dropped_since_warn
        maxsize = self.settings.mqtt_queue_max
        if len(self._deque) >= maxsize:
            if not item.terminal:
                STATS.incr("mqtt.dropped.overflow")
                _dropped_since_warn += 1
                now = time.time()
                if now - _last_drop_warn_at >= _DROP_WARN_INTERVAL_S:
                    logger.warning(
                        "push: mqtt queue full (max=%d) -- dropped %d event frame(s) "
                        "since last warning",
                        maxsize,
                        _dropped_since_warn,
                    )
                    _last_drop_warn_at = now
                    _dropped_since_warn = 0
                self._update_gauges()
                return
            for i, existing in enumerate(self._deque):
                if not existing.terminal:
                    del self._deque[i]
                    break
            # else: nothing evictable -- enqueue anyway, deliberately
            # exceeding maxsize by one (spec §1); a deque has no size cap of
            # its own to fight here.
        self._deque.append(item)
        if item.kind == "events":
            STATS.incr("mqtt.msg.events")
        elif item.kind == "reviews":
            STATS.incr("mqtt.msg.reviews")
        self._update_gauges()
        self._wake.set()

    def _update_gauges(self) -> None:
        depth = len(self._deque)
        STATS.gauge("mqtt.queue.depth", depth)
        STATS.gauge_max("mqtt.queue.high_water", depth)

    async def _consume(self) -> None:
        """The single ordered consumer (spec §1): pops items left-to-right
        and dispatches them one at a time, so `handle_events_payload` /
        `handle_reviews_payload` / `reset_tracks` calls stay in enqueue
        order relative to each other."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._deque:
                item = self._deque.popleft()
                self._update_gauges()
                self._dispatching = True
                try:
                    await self._dispatch(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("push: mqtt consumer handler failed for %s frame", item.kind)
                    STATS.incr("mqtt.consumer.errors")
                finally:
                    self._dispatching = False

    async def _dispatch(self, item: _QueueItem) -> None:
        if item.kind == "reset":
            self.engine.reset_tracks()
            return
        payload = item.payload or {}
        after = payload.get("after") or {}
        if not isinstance(after, dict):
            after = {}
        if item.kind == "events":
            token = set_push_context(after.get("camera"), after.get("id"), None)
            try:
                await self.engine.handle_object_payload(payload)
            finally:
                reset_push_context(token)
        elif item.kind == "reviews":
            token = set_push_context(after.get("camera"), None, after.get("id"))
            try:
                await self.engine.handle_review_payload(payload)
            finally:
                reset_push_context(token)

    def start_consumer(self) -> asyncio.Task[None]:
        """Idempotent: creates the consumer task on the running loop if it
        isn't already running. `run_forever` calls this; tests that don't go
        through `run_forever` (no real broker) can call it directly after
        setting `self._loop`."""
        if self._consumer_task is None:
            loop = self._loop or asyncio.get_running_loop()
            self._consumer_task = loop.create_task(self._consume())
        return self._consumer_task

    async def drain(self) -> None:
        """Test helper: await until the deque is empty and no dispatch is
        in flight.

        Always yields at least once before checking (a do-while, not a
        while): a caller that just called `on_message`/`_enqueue` on this
        same loop (the common test shape -- no real paho thread involved)
        has only *scheduled* the enqueue via `call_soon_threadsafe`, which
        hasn't run yet at the point `drain()` is entered, so checking the
        deque first would see it as already empty and return immediately."""
        while True:
            await asyncio.sleep(0)
            if not self._deque and not self._dispatching:
                return

    def _handle_reviews_message(self, payload_bytes: bytes) -> None:
        self.last_seen = time.time()
        self.last_review_at = self.last_seen
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("push: dropping malformed frigate/reviews message")
            return
        if not isinstance(payload, dict):
            return
        loop = self._loop
        if loop is None:
            return
        item = _QueueItem(kind="reviews", payload=payload, terminal=True, enqueued_at=time.time())
        loop.call_soon_threadsafe(self._enqueue, item)

    def _handle_events_message(self, payload_bytes: bytes) -> None:
        """`frigate/events` -- dwell input only, never a push trigger.

        Deliberately does *not* touch `last_seen`: this topic is chatty enough
        (thousands of messages an hour) that letting it feed the staleness
        clock would mask a `frigate/reviews` subscription that had silently
        stopped delivering, which is exactly the outage the backfill exists to
        catch.
        """
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        loop = self._loop
        if loop is None:
            return
        item = _QueueItem(
            kind="events",
            payload=payload,
            terminal=self._terminal_events_item(payload),
            enqueued_at=time.time(),
        )
        loop.call_soon_threadsafe(self._enqueue, item)

    def _handle_available_message(self, payload_bytes: bytes) -> None:
        self.last_seen = time.time()
        text = payload_bytes.decode("utf-8", errors="replace").strip()
        # `frigate/available` is plain text, not JSON -- "online" | "offline".
        self.frigate_online = text != "offline"
        if not self.frigate_online:
            logger.info("push: frigate reports offline via frigate/available")

    def on_message(self, _client: Any, _userdata: Any, msg: mqtt.MQTTMessage) -> None:
        if msg.topic == self.settings.mqtt_topic_reviews:
            if self._capture is not None:
                self._capture.append(msg.topic, msg.payload)
            self._handle_reviews_message(msg.payload)
        elif msg.topic == self.settings.mqtt_topic_events:
            if self._capture is not None:
                self._capture.append(msg.topic, msg.payload)
            self._handle_events_message(msg.payload)
        elif msg.topic == self.settings.mqtt_topic_available:
            self._handle_available_message(msg.payload)

    @property
    def connected(self) -> bool:
        """True while the paho client holds a live broker connection.

        Surfaced by /healthz so a dead subscriber (the 2026-08-11 41-hour
        silent outage) is visible to the container/systemd healthcheck
        instead of only to someone reading the logs.
        """
        return self._client is not None and self._client.is_connected()

    def is_stale(self, *, now: float | None = None) -> bool:
        """True once the broker's been silent long enough that Frigate might
        be offline (spec §5 / §12.6's stale/live model)."""
        now = time.time() if now is None else now
        return (now - self.last_seen) > self.settings.offline_silence_s

    async def resume_with_backfill(self) -> int:
        """Called after a reconnect (or a stale window resolves): back-fill
        the gap before treating live pushes as caught up."""
        after = self.last_seen - self.settings.backfill_lookback_s
        return await backfill_since(
            self.engine,
            frigate_base_url=self.frigate_base_url,
            after=after,
            staleness_s=self.settings.delivery_backfill_staleness_s,
        )

    def build_client(self) -> mqtt.Client:
        import paho.mqtt.client as mqtt_client

        client = mqtt_client.Client(
            client_id=self.settings.mqtt_client_id,
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]  # paho re-exports it without __all__
        )
        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        client.on_message = self.on_message

        def _on_connect(c: Any, _u: Any, _f: Any, _rc: Any, _props: Any = None) -> None:
            c.subscribe(self.settings.mqtt_topic_reviews)
            c.subscribe(self.settings.mqtt_topic_available)
            if self.settings.dwell_source == "events":
                c.subscribe(self.settings.mqtt_topic_events)
            self.last_seen = time.time()
            STATS.incr("mqtt.reconnect")
            # Track ids are per-Frigate-lifetime: a disconnect may well have
            # been Frigate restarting, and held dwell state would then be
            # attributed to whatever object inherits the id (handoff item 8).
            # Enqueued (not called directly) so the reset applies in order
            # relative to frames already queued ahead of it -- this runs on
            # the paho thread, same as `on_message`.
            loop = self._loop
            if loop is not None:
                item = _QueueItem(
                    kind="reset", payload=None, terminal=True, enqueued_at=time.time()
                )
                loop.call_soon_threadsafe(self._enqueue, item)
            else:
                self.engine.reset_tracks()

        client.on_connect = _on_connect
        self._client = client
        return client

    async def run_forever(self) -> None:
        """Connect, reconnecting with backoff on failure, forever (until
        `stop()`). Meant to be run as a background asyncio task."""
        self._loop = self._loop or asyncio.get_running_loop()
        self.start_consumer()
        try:
            attempt = 0
            while not self._stopped:
                client = self.build_client()
                try:
                    await asyncio.to_thread(
                        client.connect, self.settings.mqtt_host, self.settings.mqtt_port
                    )
                    client.loop_start()
                    # CONNACK is processed on paho's network thread *after*
                    # `connect()` returns — checking `is_connected()` immediately
                    # is a race that a low-latency LAN usually wins and anything
                    # slower (an ssh-tunneled broker, diagnosed 2026-08-14)
                    # always loses, producing a silent reconnect storm. Give the
                    # handshake a bounded moment to land first.
                    for _ in range(100):
                        if client.is_connected() or self._stopped:
                            break
                        await asyncio.sleep(0.1)
                    attempt = 0
                    # Idle until the connection drops or we're asked to stop.
                    while not self._stopped and client.is_connected():
                        await asyncio.sleep(1.0)
                        if self.is_stale():
                            # A bug in downstream event processing must not take
                            # the whole subscriber down with it -- that's exactly
                            # what happened 2026-08-11: an unhandled ValueError
                            # from here escaped run_forever entirely and the
                            # subscriber task died silently until the service was
                            # restarted, 41 hours later.
                            try:
                                await self.resume_with_backfill()
                            except Exception:
                                logger.exception("push: backfill after stale window failed")
                            self.last_seen = time.time()
                except (OSError, ConnectionError) as exc:
                    logger.warning(
                        "push: mqtt connect to %s:%s failed: %s",
                        self.settings.mqtt_host,
                        self.settings.mqtt_port,
                        exc,
                    )
                finally:
                    client.loop_stop()
                    with contextlib.suppress(Exception):
                        client.disconnect()
                if self._stopped:
                    break
                backoff = compute_backoff(
                    attempt,
                    self.settings.reconnect_backoff_s,
                    self.settings.reconnect_backoff_max_s,
                )
                attempt += 1
                await asyncio.sleep(backoff)
        finally:
            # Cancelled either because `stop()` broke the loop above (normal
            # shutdown) or because this task itself was cancelled from
            # outside (server.py's lifespan) -- either way the consumer must
            # not outlive `run_forever` as an orphaned pending task (no "Task
            # was destroyed but it is pending" warnings).
            task, self._consumer_task = self._consumer_task, None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def stop(self) -> None:
        self._stopped = True
        if self._consumer_task is not None:
            self._consumer_task.cancel()
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.disconnect()
