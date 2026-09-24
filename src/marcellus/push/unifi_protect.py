"""UniFi Protect doorbell-ring subscriber.

Owns a websocket connection to a UniFi OS console's Protect Integration API
(`/proxy/protect/integration/v1/subscribe/events`) and turns ring events into
calls into `push.doorbell`. Modeled on `push.mqtt.MqttReviewSubscriber`'s
lifecycle (reconnect-with-backoff, `run_forever`/`stop`, a `status()` for
Push Doctor) but talks to a completely different upstream and carries no MQTT
queue of its own -- ring traffic is low-volume enough (a handful a day, not
thousands an hour) that dispatching straight from the websocket read loop is
fine.

**The exact envelope of the Integration API's ring event is not confirmed**
against a real console as of this writing (no lab device to verify against).
Two shapes are accepted defensively:

* The documented "subscribe" wrapper: `{"type": "add", "item": {"type":
  "ring", "device": "<camera id>", ...}}` (and, generously, `"update"` for
  `type` as well, in case a ring is ever delivered as an update rather than
  an add).
* A flatter, unwrapped shape some integration examples show:
  `{"type": "ring", "device": "<camera id>", ...}`.

Anything else -- a different event `type`, a missing `device`/`item.device`,
non-JSON, a non-dict frame -- is logged at DEBUG and ignored; this module
never raises out of its read loop over a shape it doesn't recognize. If the
real API turns out to nest the camera id somewhere else entirely, only
`_parse_ring_event` needs to change.

No backfill: unlike `push.mqtt`'s Frigate-reviews backfill (which replays
`/api/events` after a broker blip), a missed doorbell ring has no server-side
record to replay from -- UniFi Protect's own event history isn't fetched
here by design (scope: ring -> push, not a Protect event mirror). A ring
that arrives while the socket is reconnecting is simply missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from marcellus.config import UnifiProtectSection

logger = logging.getLogger(__name__)

#: Ring events this module acts on. "update" is included defensively (see
#: module docstring) even though "add" is what every known integration
#: example shows.
_RING_ITEM_TYPES = ("add", "update")


def compute_backoff(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff, capped -- same shape as `push.mqtt.compute_backoff`."""
    return min(cap, base * (2.0**attempt))


@dataclass(frozen=True)
class RingEvent:
    """One parsed ring, camera id still in Protect's own vocabulary --
    `push.doorbell` is what maps it to a Frigate camera name."""

    protect_camera_id: str
    protect_event_id: str = ""
    raw_type: str = ""


def _parse_ring_event(payload: dict[str, Any]) -> RingEvent | None:
    """Defensive parse of one websocket text frame's JSON body.

    Returns `None` (never raises) for anything not recognizably a ring --
    the caller logs why at DEBUG so a real console's actual shape can be
    diagnosed from the logs without this module crashing on it.
    """
    if not isinstance(payload, dict):
        return None

    # Wrapped shape: {"type": "add"|"update", "item": {"type": "ring", ...}}
    outer_type = payload.get("type")
    item = payload.get("item")
    if outer_type in _RING_ITEM_TYPES and isinstance(item, dict):
        if item.get("type") != "ring":
            return None
        device = item.get("device")
        if not device or not isinstance(device, str):
            return None
        event_id = item.get("id") or payload.get("id") or ""
        return RingEvent(
            protect_camera_id=device,
            protect_event_id=str(event_id) if event_id else "",
            raw_type="wrapped",
        )

    # Flat shape: {"type": "ring", "device": "...", ...}
    if payload.get("type") == "ring":
        device = payload.get("device")
        if not device or not isinstance(device, str):
            return None
        event_id = payload.get("id") or ""
        return RingEvent(
            protect_camera_id=device,
            protect_event_id=str(event_id) if event_id else "",
            raw_type="flat",
        )

    return None


@dataclass
class _ProtectStatus:
    connected: bool = False
    last_ring_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    cameras_seen: int = 0


class ProtectRingSubscriber:
    """Owns the Protect websocket connection; hands parsed `RingEvent`s to
    `on_ring`.

    `on_ring` is injected (rather than this module importing `push.doorbell`
    directly) for the same testability reason `MqttReviewSubscriber` takes a
    `PushEngine`: the reconnect/backoff/parsing logic here is exercised
    without a real console, and the send logic in `push.doorbell` is
    exercised without a real websocket.
    """

    def __init__(
        self,
        settings: UnifiProtectSection,
        on_ring: Callable[[RingEvent], Awaitable[None]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.on_ring = on_ring
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(verify=settings.verify_tls, timeout=10.0)
        self._stopped = False
        self._status = _ProtectStatus()
        self._task: asyncio.Task[None] | None = None

    # -- status (Push Doctor) -------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "connected": self._status.connected,
            "last_ring_at": self._status.last_ring_at,
            "last_error": self._status.last_error,
            "last_error_at": self._status.last_error_at,
            "cameras_configured": len(self.settings.cameras),
        }

    # -- startup validation ----------------------------------------------------

    async def validate_and_log_cameras(self) -> bool:
        """`GET .../meta/info` then `.../cameras`, logging the camera id/name
        list so an operator can fill in `unifi_protect.cameras`. Returns
        False (logged, non-fatal) on any failure -- a console that's briefly
        unreachable at startup must not block the rest of the sidecar."""
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        try:
            meta = await self._client.get(
                f"{base}/proxy/protect/integration/v1/meta/info", headers=headers
            )
            meta.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("unifi_protect: meta/info check failed: %s", _exc_str(exc))
            self._status.last_error = _exc_str(exc)
            self._status.last_error_at = time.time()
            return False
        try:
            cams = await self._client.get(
                f"{base}/proxy/protect/integration/v1/cameras", headers=headers
            )
            cams.raise_for_status()
            cameras = cams.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("unifi_protect: cameras list failed: %s", _exc_str(exc))
            self._status.last_error = _exc_str(exc)
            self._status.last_error_at = time.time()
            return False
        if isinstance(cameras, list):
            self._status.cameras_seen = len(cameras)
            for cam in cameras:
                if isinstance(cam, dict):
                    logger.info(
                        "unifi_protect: camera id=%s name=%r -- add to unifi_protect.cameras "
                        "to map it to a Frigate camera",
                        cam.get("id"), cam.get("name"),
                    )
        return True

    # -- websocket loop ----------------------------------------------------

    async def run_forever(self) -> None:
        attempt = 0
        await self.validate_and_log_cameras()
        while not self._stopped:
            try:
                await self._connect_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let one bad frame kill the loop
                self._status.connected = False
                self._status.last_error = _exc_str(exc)
                self._status.last_error_at = time.time()
                logger.warning("unifi_protect: websocket loop error: %s", _exc_str(exc))
            if self._stopped:
                break
            backoff = compute_backoff(attempt)
            attempt += 1
            await asyncio.sleep(backoff)

    async def _connect_once(self) -> None:
        import websockets

        base = self.settings.console_url.rstrip("/")
        ws_url = base.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/proxy/protect/integration/v1/subscribe/events"
        headers = {"X-API-KEY": self.settings.api_key}
        connect_kwargs: dict[str, Any] = {"additional_headers": headers}
        if not self.settings.verify_tls:
            import ssl

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_kwargs["ssl"] = ctx

        async with websockets.connect(ws_url, **connect_kwargs) as ws:
            self._status.connected = True
            logger.info("unifi_protect: websocket connected")
            async for message in ws:
                if self._stopped:
                    break
                await self._handle_message(message)
        self._status.connected = False

    async def _handle_message(self, message: Any) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            logger.debug("unifi_protect: dropping non-JSON frame")
            return
        ring = _parse_ring_event(payload)
        if ring is None:
            logger.debug("unifi_protect: ignoring frame (not a recognized ring shape)")
            return
        self._status.last_ring_at = time.time()
        try:
            await self.on_ring(ring)
        except Exception:  # noqa: BLE001 - a bad ring must not kill the socket
            logger.exception("unifi_protect: on_ring handler failed")

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task[None]:
        loop = loop or asyncio.get_event_loop()
        self._task = loop.create_task(self.run_forever())
        return self._task

    async def aclose(self) -> None:
        self.stop()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._own_client:
            await self._client.aclose()


def _exc_str(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
