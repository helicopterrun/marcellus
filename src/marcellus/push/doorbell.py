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

import logging
import time
from dataclasses import dataclass
from typing import Any

from marcellus.push import store
from marcellus.push.models import Device
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


def is_dedup_window_active(
    camera: str, *, now: float, window_s: float
) -> bool:
    last = _last_ring_sent_at.get(camera)
    return last is not None and (now - last) < window_s


def build_ring_payload(
    *,
    frigate_camera: str,
    media: str | None,
    protect_event_id: str,
    device_timezone: str = "",
    now: float | None = None,
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
    payload: dict[str, Any] = {
        "aps": aps,
        "doorbell": {
            "camera": frigate_camera,
            "protect_event_id": protect_event_id,
            "ts": round(now, 3),
        },
    }
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
) -> RingOutcome:
    """Send one ring to every eligible device. `conn` is an already-open
    sidecar DB connection (caller owns its lifecycle, same convention as
    `push/engine.py`'s handlers)."""
    now = time.time() if now is None else now

    frigate_camera = frigate_camera_for(protect_camera_id, cameras)
    if frigate_camera is None:
        logger.debug(
            "unifi_protect: ring for unmapped camera id=%s -- add it to "
            "unifi_protect.cameras", protect_camera_id,
        )
        return RingOutcome(frigate_camera=None, sent=0, skipped_unmapped=True)

    if is_dedup_window_active(frigate_camera, now=now, window_s=ring_dedup_seconds):
        logger.debug(
            "unifi_protect: dropping duplicate ring for camera=%s (dedup window %.0fs)",
            frigate_camera, ring_dedup_seconds,
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

    collapse_id = f"ring:{frigate_camera}"
    card_key = f"{_CARD_KEY_PREFIX}{frigate_camera}"
    sent = 0
    for device in eligible:
        snoozed = store.active_snoozes(conn, device.apns_token, now=now)
        if not _should_send(device, frigate_camera=frigate_camera, snoozed_scopes=snoozed):
            continue
        payload = build_ring_payload(
            frigate_camera=frigate_camera,
            media=media,
            protect_event_id=protect_event_id,
            device_timezone=device.timezone,
            now=now,
        )
        result: TransportResult = await transport.send_situation(
            device, payload=payload, collapse_id=collapse_id,
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
                device.device_id, result.error,
            )
            store.delete_device(conn, device.apns_token)
        else:
            logger.warning(
                "unifi_protect: ring send failed for device %s: %s",
                device.device_id, result.error,
            )

    if sent > 0 or eligible:
        # Arm the dedup window on the *attempt*, not only on a fully
        # successful fan-out -- a partial/total transport failure must not
        # spam retried "duplicate" pushes into the same short window either.
        _last_ring_sent_at[frigate_camera] = now

    return RingOutcome(frigate_camera=frigate_camera, sent=sent)
