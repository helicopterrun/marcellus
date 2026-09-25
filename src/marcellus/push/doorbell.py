"""UniFi Protect doorbell ring -> push send path.

Deliberately outside the card/attention-ladder/rate-limiter pipeline
(`push/delivery_wire.py`, `push/engine.py`): a ring is not a Frigate review
item and never becomes one, there is no "level" to route it through, and it
must never be throttled by `push_sends`' rolling per-situation window -- the
whole feature is "someone is standing at the door right now", which the
ladder's tiers, snooze-by-camera-doesn't-mute-it exception, and dedup window
below all exist to preserve.

Fan-out is simple: every registered `push_devices` row with
`doorbell_rings` on gets one send, gated only by (a) a *global* snooze -- a
camera-scoped snooze deliberately does **not** mute a ring, see
`_should_send` -- and (b) a short per-camera server-side dedup window
(`unifi_protect.ring_dedup_seconds`) collapsing a duplicate "add" frame for
one physical press into a single push.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from marcellus.push import store
from marcellus.push.models import Device
from marcellus.push.thumbnails import fetch_thumbnail
from marcellus.push.transport import PushTransport, TransportResult

logger = logging.getLogger(__name__)

#: card_key namespace `push_card_sends` records ring sends under, so Push
#: Doctor / device stats see them without a new table (spec's "existing
#: send-tracking table if one fits").
_CARD_KEY_PREFIX = "doorbell:"
_MUTATION = "ring"

#: camera -> epoch of the last ring actually sent for it. Module-level and
#: in-memory, same reasoning as `routes/push.py`'s `_last_test_push_at`: a
#: restart re-arming the dedup window is fine, this only ever needs to
#: survive within one process's uptime.
_last_ring_sent_at: dict[str, float] = {}

#: Hard ceiling on the combined Protect-then-Frigate snapshot prewarm
#: (fix 2, review blocker): a dead console must never push the ring send
#: itself past this. Kept well under APNs' own delivery expectations.
DEFAULT_SNAPSHOT_PREWARM_TIMEOUT_S = 5.0

#: The Frigate leg's own budget within that combined window -- must leave
#: room for the Protect attempt (which has already run by the time this
#: fires) rather than being able to consume the whole outer timeout itself.
DEFAULT_FRIGATE_FALLBACK_TIMEOUT_S = 2.5


def reset_ring_dedup_for_tests() -> None:
    _last_ring_sent_at.clear()


@dataclass(frozen=True)
class RingOutcome:
    frigate_camera: str | None
    sent: int
    skipped_unmapped: bool = False
    skipped_dedup: bool = False


def frigate_camera_for(protect_camera_id: str, cameras: dict[str, str]) -> str | None:
    return cameras.get(protect_camera_id)


def _pretty_camera(camera: str) -> str:
    return camera.replace("_", " ").replace("-", " ").title()


def _local_time_str(epoch: float, tz_name: str) -> str | None:
    """`HH:MM AM/PM` in the device's own timezone, or `None` if `tz_name`
    isn't set/known -- the caller falls back to no time-of-day suffix rather
    than guessing the sidecar host's own zone, which is very often wrong for
    a phone."""
    if not tz_name:
        return None
    try:
        import datetime as _dt
        from zoneinfo import ZoneInfo

        dt = _dt.datetime.fromtimestamp(epoch, tz=ZoneInfo(tz_name))
        return dt.strftime("%-I:%M %p")
    except Exception:  # noqa: BLE001 - bad/unknown tz name must not block the send
        return None


def is_dedup_window_active(camera: str, *, now: float, window_s: float) -> bool:
    last = _last_ring_sent_at.get(camera)
    return last is not None and (now - last) < window_s


#: M-2 default slot order for a device that has never set its own
#: `doorbell_slots` -- must match `config._default_lcd_presets`' keys.
DEFAULT_LCD_SLOT_IDS = ("leave_package", "be_right_there", "do_not_disturb")


def resolve_lcd_slots(
    slot_ids: tuple[str, ...],
    *,
    presets: dict[str, Any],
    animations: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Resolve up to 3 configured option ids into the ring payload's
    `doorbell.lcd_slots` (M-2 spec point 5).

    Drops any id that no longer resolves to a known preset or image, keeping
    the `slot` numbering as the id's original 1-based position in the
    device's slot list -- NOT a sequential renumbering after dropping
    unresolvable ids. A device's slot buttons are fixed physical/UI
    positions, so if slot 2 no longer resolves, the remaining output must
    still say `slot: 3` for what was configured third, not `slot: 2`.
    """
    animations_by_id = {a["id"]: a for a in animations}
    resolved: list[dict[str, Any]] = []
    for position, slot_id in enumerate(slot_ids, start=1):
        title: str | None = None
        if slot_id in presets:
            preset = presets[slot_id]
            title = preset.title if hasattr(preset, "title") else preset.get("title")
        elif slot_id in animations_by_id:
            title = animations_by_id[slot_id]["title"]
        if title is None:
            continue
        resolved.append({"slot": position, "id": slot_id, "title": title})
    return resolved


def build_ring_payload(
    *,
    frigate_camera: str,
    media: str | None,
    protect_event_id: str,
    device_timezone: str = "",
    now: float | None = None,
    has_lcd: bool = False,
    lcd_slots: list[dict[str, Any]] | None = None,
    custom_reply_max_chars: int = 30,
    custom_reply_duration_s: int = 120,
) -> dict[str, Any]:
    """The full APNs body for one doorbell-ring push.

    Uses the card pipeline's own `media` field (top-level URL string,
    `push/delivery.py`'s `build_card_payload`) rather than the v1 situations
    `{handle, server_id}` pair, so the existing NSE -- which already knows
    how to redeem a `media` URL -- attaches the snapshot unchanged.
    """
    now = time.time() if now is None else now
    display = _pretty_camera(frigate_camera)
    local_time = _local_time_str(now, device_timezone)
    body = f"{display} · {local_time}" if local_time else display
    aps: dict[str, Any] = {
        "alert": {"title": "Someone's at the door", "body": body},
        "sound": "default",
        "interruption-level": "time-sensitive",
        "category": "doorbell.ring",
        "thread-id": "doorbell",
        "mutable-content": 1,
    }
    doorbell: dict[str, Any] = {
        "camera": frigate_camera,
        "protect_event_id": protect_event_id,
        "ts": round(now, 3),
    }
    if has_lcd:
        doorbell["lcd_slots"] = lcd_slots or []
        doorbell["custom_reply"] = {
            "max_chars": custom_reply_max_chars,
            "duration_s": custom_reply_duration_s,
        }
    payload: dict[str, Any] = {"aps": aps, "doorbell": doorbell}
    if media:
        payload["media"] = media
    return payload


def _should_send(device: Device, *, frigate_camera: str, snoozed_scopes: set[str]) -> bool:
    if not device.doorbell_rings:
        return False
    # Global snooze mutes a ring same as everything else; a camera-scoped
    # snooze deliberately does NOT -- a ring is a person at the door, and
    # muting "camera:front-door" (e.g. to quiet routine detections) must not
    # also silence someone actually pressing the button.
    return "global" not in snoozed_scopes


async def handle_ring(
    *,
    protect_camera_id: str,
    protect_event_id: str,
    cameras: dict[str, str],
    conn: Any,
    transport: PushTransport,
    frigate_base_url: str,
    external_base_url: str,
    situation_handle_ttl_s: float,
    ring_dedup_seconds: float,
    now: float | None = None,
    has_lcd: bool = False,
    lcd_presets: dict[str, Any] | None = None,
    animations: list[dict[str, str]] | None = None,
    custom_reply_max_chars: int = 30,
    custom_reply_duration_s: int = 120,
    ring_snapshot: str = "frigate",
    protect_snapshot_fetcher: Callable[[str], Awaitable[bytes | None]] | None = None,
    http_client: httpx.AsyncClient | None = None,
    snapshot_prewarm_timeout_s: float = DEFAULT_SNAPSHOT_PREWARM_TIMEOUT_S,
    frigate_fallback_timeout_s: float = DEFAULT_FRIGATE_FALLBACK_TIMEOUT_S,
) -> RingOutcome:
    """Send one ring to every eligible device. `conn` is an already-open
    sidecar DB connection (caller owns its lifecycle, same convention as
    `push/engine.py`'s handlers)."""
    now = time.time() if now is None else now

    frigate_camera = frigate_camera_for(protect_camera_id, cameras)
    if frigate_camera is None:
        logger.debug(
            "unifi_protect: ring for unmapped camera id=%s -- add it to unifi_protect.cameras",
            protect_camera_id,
        )
        return RingOutcome(frigate_camera=None, sent=0, skipped_unmapped=True)

    if is_dedup_window_active(frigate_camera, now=now, window_s=ring_dedup_seconds):
        logger.debug(
            "unifi_protect: dropping duplicate ring for camera=%s (dedup window %.0fs)",
            frigate_camera,
            ring_dedup_seconds,
        )
        return RingOutcome(frigate_camera=frigate_camera, sent=0, skipped_dedup=True)

    devices = store.list_devices(conn)
    eligible = [d for d in devices if d.doorbell_rings]
    if not eligible:
        return RingOutcome(frigate_camera=frigate_camera, sent=0)

    media: str | None = None
    if external_base_url:
        handle = store.mint_handle(
            conn,
            camera=frigate_camera,
            event_id="",
            review_id=f"doorbell-{protect_event_id or int(now)}",
            ttl_s=situation_handle_ttl_s,
        )
        media = f"{external_base_url.rstrip('/')}/v1/push/thumbnail/{handle}"

        async def _prewarm() -> bytes | None:
            jpeg: bytes | None = None
            if ring_snapshot == "protect" and protect_snapshot_fetcher is not None:
                jpeg = await protect_snapshot_fetcher(protect_camera_id)
            if jpeg is None and frigate_base_url:
                if http_client is not None:
                    jpeg = await fetch_thumbnail(
                        http_client,
                        frigate_base_url=frigate_base_url,
                        camera=frigate_camera,
                        event_id="",
                        timeout=frigate_fallback_timeout_s,
                    )
                else:
                    # No shared client was supplied (e.g. a caller/test that
                    # doesn't have one handy) -- fall back to a short-lived
                    # client rather than require one everywhere.
                    async with httpx.AsyncClient() as client:
                        jpeg = await fetch_thumbnail(
                            client,
                            frigate_base_url=frigate_base_url,
                            camera=frigate_camera,
                            event_id="",
                            timeout=frigate_fallback_timeout_s,
                        )
            return jpeg

        # Fix 2 (review blocker): the Protect fetch (up to its own 5s
        # timeout) plus the Frigate fallback fetch could together take up to
        # ~10s worst case, delaying the ring push past any reasonable bound
        # if the console is dead. Bound the combined attempt and, on
        # timeout, fall back to the pre-this-PR behavior -- mint the handle
        # without prewarmed bytes -- rather than let the exception propagate
        # and block/fail the send.
        jpeg = None
        try:
            jpeg = await asyncio.wait_for(_prewarm(), timeout=snapshot_prewarm_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "unifi_protect: snapshot prewarm for camera=%s timed out after %.1fs, "
                "sending ring without a prewarmed thumbnail",
                frigate_camera,
                snapshot_prewarm_timeout_s,
            )
        if jpeg:
            store.store_thumbnail(conn, handle, jpeg)

    collapse_id = f"ring:{frigate_camera}"
    card_key = f"{_CARD_KEY_PREFIX}{frigate_camera}"
    presets = lcd_presets or {}
    anims = animations or []
    sent = 0
    for device in eligible:
        snoozed = store.active_snoozes(conn, device.apns_token, now=now)
        if not _should_send(device, frigate_camera=frigate_camera, snoozed_scopes=snoozed):
            continue
        lcd_slots: list[dict[str, Any]] | None = None
        if has_lcd:
            slot_ids = device.doorbell_slots or DEFAULT_LCD_SLOT_IDS
            lcd_slots = resolve_lcd_slots(tuple(slot_ids), presets=presets, animations=anims)
        payload = build_ring_payload(
            frigate_camera=frigate_camera,
            media=media,
            protect_event_id=protect_event_id,
            device_timezone=device.timezone,
            now=now,
            has_lcd=has_lcd,
            lcd_slots=lcd_slots,
            custom_reply_max_chars=custom_reply_max_chars,
            custom_reply_duration_s=custom_reply_duration_s,
        )
        result: TransportResult = await transport.send_situation(
            device,
            payload=payload,
            collapse_id=collapse_id,
        )
        store.record_card_send(
            conn,
            apns_token=device.apns_token,
            card_key=card_key,
            mutation=_MUTATION,
            sent_at=now,
            ok=result.ok,
            error=result.error,
        )
        if result.ok:
            sent += 1
        elif result.unregistered:
            logger.info(
                "unifi_protect: pruning device %s after ring send (%s)",
                device.device_id,
                result.error,
            )
            store.delete_device(conn, device.apns_token)
        else:
            logger.warning(
                "unifi_protect: ring send failed for device %s: %s",
                device.device_id,
                result.error,
            )

    if sent > 0 or eligible:
        # Arm the dedup window on the *attempt*, not only on a fully
        # successful fan-out -- a partial/total transport failure must not
        # spam retried "duplicate" pushes into the same short window either.
        _last_ring_sent_at[frigate_camera] = now

    return RingOutcome(frigate_camera=frigate_camera, sent=sent)
