"""UniFi Protect doorbell-ring subscriber.

Owns a websocket connection to a UniFi OS console's Protect Integration API
(`/proxy/protect/integration/v1/subscribe/events`) and turns ring events into
calls into `push.doorbell`. Modeled on `push.mqtt.MqttReviewSubscriber`'s
lifecycle (reconnect-with-backoff, `run_forever`/`stop`, a `status()` for
Push Doctor) but talks to a completely different upstream and carries no MQTT
queue of its own -- ring traffic is low-volume enough (a handful a day, not
thousands an hour) that dispatching straight from the websocket read loop is
fine.

The wrapper envelope was confirmed against a live Protect 7.2.105 console:
every event arrives as `{"type": "add"|"update", "item": {"type": ...,
"device": "<camera id>", ...}}`, with one "add" followed by several
"update" frames for the same event (which can outlast the dedup window).
Accepted shapes:

* The wrapper with `type == "add"` only -- "update" frames re-describe an
  event already announced and would re-ring the phone:
  `{"type": "add", "item": {"type": "ring", "device": "<camera id>", ...}}`.
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
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from marcellus.config import UnifiProtectSection

logger = logging.getLogger(__name__)

#: Ring events this module acts on. Only "add": Protect follows each "add"
#: with several "update" frames for the same event (see module docstring).
_RING_ITEM_TYPES = ("add",)


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
    #: epoch when the websocket was last observed disconnected (set on
    #: startup and on every disconnect, cleared on connect) -- `/healthz`
    #: uses this to tell "briefly reconnecting" from "down for minutes".
    disconnected_since: float | None = None


@dataclass
class ProtectCameraStatus:
    """One mapped camera's most recent state, from the `device_poll_loop`
    REST poll of `/proxy/protect/integration/v1/cameras` -- separate from
    the ring websocket, which carries no camera health info at all."""

    protect_id: str
    frigate_camera: str
    name: str | None
    model: str | None
    state: str | None
    has_lcd: bool
    checked_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "protect_id": self.protect_id,
            "frigate_camera": self.frigate_camera,
            "name": self.name,
            "model": self.model,
            "state": self.state,
            "has_lcd": self.has_lcd,
            "checked_at": self.checked_at,
        }


def _strip_sprite_title(original_name: str) -> str:
    """Image title = `originalName` with a trailing `.gif.png`, `.png`, or
    `.gif` suffix stripped, checked in that order (M-2, console fact)."""
    for suffix in (".gif.png", ".png", ".gif"):
        if original_name.endswith(suffix):
            return original_name[: -len(suffix)]
    return original_name


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
        self._status = _ProtectStatus(disconnected_since=time.time())
        self._task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self.console_version: str | None = None
        self.cameras: dict[str, ProtectCameraStatus] = {}
        self.last_poll_at: float | None = None
        self.last_poll_error: str | None = None
        #: M-2: `GET .../files/animations`' usable sprite entries, each
        #: `{"id": "image:<name>", "title": <stripped originalName>}`. Kept
        #: across a failed poll (stale beats empty, same as `self.cameras`).
        self.animations: list[dict[str, str]] = []
        #: Serializes `set_lcd_message` PATCH calls across every camera and
        #: enforces the >=1s console-write spacing (M-2 console fact).
        self._lcd_lock = asyncio.Lock()
        self._last_lcd_write_at: float | None = None

    # -- status (Push Doctor) -------------------------------------------------

    def status(self) -> dict[str, Any]:
        cam_list = list(self.cameras.values())
        mapped_ids = set(self.settings.cameras)
        if self.last_poll_at is None or not mapped_ids:
            mapped_cameras_connected = False
        else:
            seen = {c.protect_id: c for c in cam_list}
            mapped_cameras_connected = all(
                pid in seen and seen[pid].state == "CONNECTED" for pid in mapped_ids
            )
        return {
            "enabled": True,
            "connected": self._status.connected,
            "last_ring_at": self._status.last_ring_at,
            "last_error": self._status.last_error,
            "last_error_at": self._status.last_error_at,
            "cameras_configured": len(self.settings.cameras),
            "console_version": self.console_version,
            "last_poll_at": self.last_poll_at,
            "last_poll_error": self.last_poll_error,
            "cameras": [c.as_dict() for c in cam_list],
            "mapped_cameras_connected": mapped_cameras_connected,
        }

    @property
    def disconnected_since(self) -> float | None:
        """Epoch the websocket has been continuously disconnected since, or
        `None` if currently connected. `/healthz` reads this."""
        return self._status.disconnected_since

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
                if not isinstance(cam, dict):
                    continue
                mapped = self.settings.cameras.get(str(cam.get("id")))
                if mapped:
                    logger.info(
                        "unifi_protect: camera id=%s name=%r -> Frigate camera %r",
                        cam.get("id"), cam.get("name"), mapped,
                    )
                else:
                    logger.info(
                        "unifi_protect: camera id=%s name=%r -- add to unifi_protect.cameras "
                        "to map it to a Frigate camera",
                        cam.get("id"), cam.get("name"),
                    )
        return True

    # -- device (camera health) poll ---------------------------------------

    async def _fetch_meta_info(self) -> None:
        """`GET /meta/info` for `applicationVersion` -- called once at
        startup and again after every websocket (re)connect (M-1 spec).
        Non-fatal: a failure here just leaves `console_version` stale."""
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        try:
            resp = await self._client.get(
                f"{base}/proxy/protect/integration/v1/meta/info", headers=headers
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("unifi_protect: meta/info poll failed: %s", _exc_str(exc))
            return
        if isinstance(body, dict):
            version = body.get("applicationVersion")
            if isinstance(version, str):
                self.console_version = version

    async def poll_devices_once(self) -> None:
        """One `GET /proxy/protect/integration/v1/cameras`, parsing every
        camera present in `settings.cameras` into `self.cameras`.

        On a 429 or 5xx or network error, retries once after 5s; if that
        retry also fails, records `last_poll_error` and leaves the
        previously-known `self.cameras` state untouched (stale data beats no
        data for the `/healthz` "not CONNECTED" check).
        """
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        url = f"{base}/proxy/protect/integration/v1/cameras"

        async def _attempt() -> httpx.Response:
            resp = await self._client.get(url, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.raise_for_status()
            return resp

        try:
            try:
                resp = await _attempt()
            except httpx.HTTPError:
                await asyncio.sleep(5.0)
                resp = await _attempt()
            resp.raise_for_status()
            cameras = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_poll_error = _exc_str(exc)
            self.last_poll_at = time.time()
            logger.warning("unifi_protect: device poll failed: %s", _exc_str(exc))
            return

        now = time.time()
        if isinstance(cameras, list):
            for cam in cameras:
                if not isinstance(cam, dict):
                    continue
                protect_id = str(cam.get("id"))
                frigate_camera = self.settings.cameras.get(protect_id)
                if not frigate_camera:
                    continue  # unmapped camera -- not our concern here
                self.cameras[protect_id] = ProtectCameraStatus(
                    protect_id=protect_id,
                    frigate_camera=frigate_camera,
                    name=cam.get("name"),
                    model=cam.get("modelKey") or cam.get("model"),
                    state=cam.get("state"),
                    # Dahua/third-party cameras report a null lcdMessage --
                    # only a genuine Protect doorbell has an LCD to show one on.
                    has_lcd=cam.get("lcdMessage") is not None,
                    checked_at=now,
                )
        self.last_poll_error = None
        self.last_poll_at = now

    async def poll_animations_once(self) -> None:
        """One `GET /proxy/protect/integration/v1/files/animations` (M-2).

        Keeps only sprite entries (`name` ends `.png`, not `.png.gif`, the
        latter being a preview). Non-fatal: on any failure, leaves the
        previously-known `self.animations` list untouched.
        """
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        url = f"{base}/proxy/protect/integration/v1/files/animations"
        try:
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            files = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("unifi_protect: animations poll failed: %s", _exc_str(exc))
            return
        if not isinstance(files, list):
            return
        animations: list[dict[str, str]] = []
        for entry in files:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.endswith(".png") or name.endswith(".png.gif"):
                continue
            original_name = entry.get("originalName")
            title = _strip_sprite_title(original_name if isinstance(original_name, str) else name)
            animations.append({"id": f"image:{name}", "title": title, "name": name})
        self.animations = animations

    async def device_poll_loop(self) -> None:
        """Runs `poll_devices_once` on a `settings.device_poll_seconds`
        cadence until `stop()`. Fetches `/meta/info` once before the first
        poll (startup) -- `_connect_once` covers every reconnect after.

        `poll_devices_once`/`_fetch_meta_info` already catch httpx/JSON
        errors, but a malformed camera dict (bad key, wrong type) would
        raise a plain `TypeError`/`KeyError` -- same shape of risk
        `run_forever` guards against for the websocket loop, so this loop
        gets the same "never let one bad cycle kill the task" wrapper.
        """
        await self._fetch_meta_info()
        while not self._stopped:
            try:
                await self.poll_devices_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let one bad cycle kill the loop
                self.last_poll_error = _exc_str(exc)
                self.last_poll_at = time.time()
                logger.exception("unifi_protect: device poll loop error: %s", _exc_str(exc))
            if self._stopped:
                break
            await asyncio.sleep(2.0)
            try:
                await self.poll_animations_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - never let one bad cycle kill the loop
                logger.exception("unifi_protect: animations poll loop error: %s", _exc_str(exc))
            for _ in range(int(self.settings.device_poll_seconds)):
                if self._stopped:
                    break
                await asyncio.sleep(1.0)

    # -- LCD replies (M-2) --------------------------------------------------

    async def set_lcd_message(self, protect_id: str, lcd_message: dict[str, Any]) -> dict[str, Any]:
        """`PATCH /proxy/protect/integration/v1/cameras/{id}` with
        `{"lcdMessage": lcd_message}`.

        Serialized with `self._lcd_lock` (one lock shared across every
        camera -- the spec only requires console writes be serialized, not
        parallelized per-camera) and spaced >=1s apart from the previous
        console write. Raises `httpx.HTTPStatusError`/`httpx.HTTPError` on
        failure after one retry (2s sleep) for a 429/503; the caller maps
        that to a 502.
        """
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        url = f"{base}/proxy/protect/integration/v1/cameras/{protect_id}"
        async with self._lcd_lock:
            if self._last_lcd_write_at is not None:
                elapsed = time.time() - self._last_lcd_write_at
                if elapsed < 1.0:
                    await asyncio.sleep(1.0 - elapsed)

            async def _attempt() -> httpx.Response:
                resp = await self._client.patch(
                    url, headers=headers, json={"lcdMessage": lcd_message}, timeout=8.0
                )
                return resp

            resp = await _attempt()
            if resp.status_code == 429 or resp.status_code == 503:
                await asyncio.sleep(2.0)
                resp = await _attempt()
            self._last_lcd_write_at = time.time()
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json())

    async def fetch_snapshot(self, protect_id: str) -> bytes | None:
        """`GET /cameras/{id}/snapshot?highQuality=true` (M-2). Returns
        `None` on any failure -- the caller falls back to Frigate's own
        `latest.jpg`."""
        base = self.settings.console_url.rstrip("/")
        headers = {"X-API-KEY": self.settings.api_key}
        url = f"{base}/proxy/protect/integration/v1/cameras/{protect_id}/snapshot"
        try:
            resp = await self._client.get(
                url, headers=headers, params={"highQuality": "true"}, timeout=5.0
            )
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPError as exc:
            logger.info(
                "unifi_protect: snapshot fetch failed for %s: %s", protect_id, _exc_str(exc)
            )
            return None

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
                if self._status.disconnected_since is None:
                    self._status.disconnected_since = time.time()
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
            self._status.disconnected_since = None
            logger.info("unifi_protect: websocket connected")
            # Re-check application version on every (re)connect, not just
            # startup -- a console can be upgraded while the sidecar is
            # already running and reconnecting.
            await self._fetch_meta_info()
            async for message in ws:
                if self._stopped:
                    break
                await self._handle_message(message)
        self._status.connected = False
        if self._status.disconnected_since is None:
            self._status.disconnected_since = time.time()

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
        if self._poll_task is not None:
            self._poll_task.cancel()

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> asyncio.Task[None]:
        loop = loop or asyncio.get_event_loop()
        self._task = loop.create_task(self.run_forever())
        return self._task

    def start_device_poll(
        self, loop: asyncio.AbstractEventLoop | None = None
    ) -> asyncio.Task[None]:
        """Start `device_poll_loop` as its own task, tracked as
        `self._poll_task` -- `stop()`/`aclose()` cancel and await it, so a
        caller only needs this one call (no outer-retry wrapper needed the
        way `run_forever`'s task gets one: `device_poll_loop` already
        swallows non-cancellation exceptions itself)."""
        loop = loop or asyncio.get_event_loop()
        self._poll_task = loop.create_task(self.device_poll_loop())
        return self._poll_task

    async def aclose(self) -> None:
        self.stop()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._poll_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        if self._own_client:
            await self._client.aclose()


def _exc_str(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
