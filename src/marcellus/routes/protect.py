"""`GET /v1/protect/status` -- UniFi Protect device health for the app's
settings page (M-1: "Protect health & capability flag") -- plus M-2's
doorbell LCD replies and ring snapshot proxy.

Authenticated like every other sidecar-owned `/v1` route (no entry in
`auth.EXEMPT_PATHS`) -- unlike `/v1/capabilities`, this carries console
version and per-camera state, not just a feature flag. The M-2 routes below
follow the same convention: nothing here is exempt. The one place M-2 reuses
an unauthenticated credential is the ring's own `media` URL, which still
goes through `push/store.py`'s existing opaque-handle mechanism
(`mint_handle` + `GET /v1/push/thumbnail/{handle}`, `auth.EXEMPT_PREFIXES`)
-- this module just changes *what bytes* land under that handle when
`ring_snapshot == "protect"`, not who can fetch it.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from marcellus import db, frigate_api
from marcellus.push import store
from marcellus.push.thumbnails import fetch_thumbnail
from marcellus.push.unifi_protect import ProtectRingSubscriber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/protect", tags=["protect"])
doorbell_router = APIRouter(prefix="/v1/doorbell", tags=["doorbell"])

_ERR_DISABLED = "unifi_protect_disabled"
_ERR_UNKNOWN_CAMERA = "unknown_camera"
_ERR_NO_LCD = "no_lcd"
_ERR_BAD_OPTION = "bad_option"
_ERR_CONSOLE_FAILED = "console_failed"

_CUSTOM_TEXT_RE = re.compile(r"^[A-Z0-9 .,!?'&-]+$")


def _reverse_camera_map(settings: Any) -> dict[str, str]:
    """Frigate camera name -> Protect camera id, derived from
    `unifi_protect.cameras` (Protect id -> Frigate name). Built on every call
    rather than cached -- the map is tiny and config can change at runtime."""
    return {frigate: protect_id for protect_id, frigate in settings.unifi_protect.cameras.items()}


def _require_protect_id(settings: Any, camera: str, *, disabled_status: int = 404) -> str:
    if not settings.unifi_protect.enabled:
        raise HTTPException(
            status_code=disabled_status, detail={"message": "unifi_protect disabled"}
        )
    protect_id = _reverse_camera_map(settings).get(camera)
    if protect_id is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_UNKNOWN_CAMERA,
                "message": f"no Protect camera mapped to {camera!r}",
            },
        )
    return protect_id


def _require_has_lcd(request: Request, protect_id: str) -> None:
    subscriber: ProtectRingSubscriber | None = getattr(
        request.app.state, "protect_subscriber", None
    )
    cam_status = subscriber.cameras.get(protect_id) if subscriber is not None else None
    if cam_status is None or not cam_status.has_lcd:
        raise HTTPException(
            status_code=409,
            detail={"error": _ERR_NO_LCD, "message": "camera has no LCD"},
        )


@router.get("/status")
async def protect_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    if not settings.unifi_protect.enabled:
        return {"enabled": False}

    subscriber = getattr(request.app.state, "protect_subscriber", None)
    if subscriber is None:
        # Feature enabled but the lifespan hasn't started it yet (or this is
        # a bare `create_app` under a test runner) -- same "starting" shape
        # other /healthz checks use, just without a status body to build on.
        return {"enabled": True, "starting": True}

    body = subscriber.status()
    frigate_cameras = sorted(set(settings.unifi_protect.cameras.values()))

    def _query(conn: Any) -> dict[str, float]:
        return store.last_ring_at_per_camera(conn, frigate_cameras=frigate_cameras)

    last_ring_by_camera = await db.with_sidecar(settings.sidecar.db_path, _query)

    cameras = body.get("cameras", [])
    for cam in cameras:
        cam["last_ring_at"] = last_ring_by_camera.get(cam["frigate_camera"])

    body["enabled"] = True
    body["cameras"] = cameras
    return dict(body)


@doorbell_router.get("/{camera}/lcd/options")
async def lcd_options(camera: str, request: Request) -> dict[str, Any]:
    """The quick-reply/custom-image options for one camera's doorbell LCD
    (M-2 spec point 3)."""
    settings = request.app.state.settings
    protect_id = _require_protect_id(settings, camera)
    _require_has_lcd(request, protect_id)

    subscriber: ProtectRingSubscriber = request.app.state.protect_subscriber
    section = settings.unifi_protect
    options: list[dict[str, Any]] = []
    for preset_id, preset in section.lcd_presets.items():
        options.append(
            {
                "id": preset_id,
                "kind": "preset",
                "title": preset.title,
                "type": preset.type,
                "duration_s": preset.duration_s,
            }
        )
    for anim in subscriber.animations:
        options.append(
            {
                "id": anim["id"],
                "kind": "image",
                "title": anim["title"],
                "type": "IMAGE",
                "duration_s": section.image_duration_s,
            }
        )
    return {"camera": camera, "has_lcd": True, "options": options}


class LcdActionRequest(BaseModel):
    """Deliberately permissive -- both fields optional, no cross-field
    validator -- so the "both or neither" case is a plain route-level 400
    (matching every other input-rejection case on this route: bad
    custom_text charset/length, etc.) instead of pydantic's automatic 422."""

    option_id: str | None = None
    custom_text: str | None = None


def _normalize_custom_text(text: str, *, max_chars: int) -> str:
    normalized = " ".join(text.strip().split()).upper()
    if not normalized:
        raise HTTPException(
            status_code=400,
            detail={
                "error": _ERR_BAD_OPTION,
                "message": "custom_text is empty after normalization",
            },
        )
    if len(normalized) > max_chars:
        raise HTTPException(
            status_code=400,
            detail={
                "error": _ERR_BAD_OPTION,
                "message": f"custom_text too long (max {max_chars} chars)",
            },
        )
    if not _CUSTOM_TEXT_RE.match(normalized):
        raise HTTPException(
            status_code=400,
            detail={
                "error": _ERR_BAD_OPTION,
                "message": "custom_text has unsupported characters",
            },
        )
    return normalized


@doorbell_router.post("/{camera}/lcd")
async def lcd_action(camera: str, body: LcdActionRequest, request: Request) -> dict[str, Any]:
    """Send one LCD reply to the console (M-2 spec point 6)."""
    settings = request.app.state.settings
    protect_id = _require_protect_id(settings, camera, disabled_status=503)
    _require_has_lcd(request, protect_id)
    section = settings.unifi_protect
    subscriber: ProtectRingSubscriber = request.app.state.protect_subscriber
    now_ms = int(time.time() * 1000)

    if bool(body.option_id) == bool(body.custom_text):
        raise HTTPException(
            status_code=400,
            detail={
                "error": _ERR_BAD_OPTION,
                "message": "exactly one of option_id or custom_text is required",
            },
        )

    option_id: str | None = None
    if body.option_id is not None:
        option_id = body.option_id
        preset = section.lcd_presets.get(option_id)
        anim = next((a for a in subscriber.animations if a["id"] == option_id), None)
        if preset is not None:
            lcd_type = preset.type
            text = preset.text if preset.type == "CUSTOM_MESSAGE" else None
            reset_at = now_ms + preset.duration_s * 1000
        elif anim is not None:
            lcd_type = "IMAGE"
            text = anim["name"]
            reset_at = now_ms + section.image_duration_s * 1000
        else:
            raise HTTPException(
                status_code=400,
                detail={"error": _ERR_BAD_OPTION, "message": f"unknown option_id {option_id!r}"},
            )
    else:
        assert body.custom_text is not None
        normalized = _normalize_custom_text(
            body.custom_text, max_chars=section.custom_reply_max_chars
        )
        lcd_type = "CUSTOM_MESSAGE"
        text = normalized
        reset_at = now_ms + section.custom_reply_duration_s * 1000

    lcd_message: dict[str, Any] = {"type": lcd_type, "resetAt": reset_at}
    if text is not None:
        lcd_message["text"] = text

    ok = True
    status_code: int | None = None
    error: str | None = None
    try:
        await subscriber.set_lcd_message(protect_id, lcd_message)
    except httpx.HTTPStatusError as exc:
        ok = False
        status_code = exc.response.status_code
        error = str(exc)
    except httpx.HTTPError as exc:
        ok = False
        error = str(exc)

    def _log(conn: Any) -> None:
        store.record_doorbell_action(
            conn,
            camera=camera,
            option_id=option_id,
            type_=lcd_type,
            text=text,
            reset_at=reset_at,
            ok=ok,
            status_code=status_code,
            error=error,
        )

    await db.with_sidecar(settings.sidecar.db_path, _log)
    logger.info(
        "doorbell: lcd action camera=%s type=%s ok=%s status=%s",
        camera,
        lcd_type,
        ok,
        status_code,
    )

    if not ok:
        raise HTTPException(
            status_code=502,
            detail={"ok": False, "console_status": status_code, "error": _ERR_CONSOLE_FAILED},
        )

    return {"ok": True, "applied": {"type": lcd_type, "text": text, "reset_at": reset_at}}


@doorbell_router.get("/{camera}/snapshot")
async def doorbell_snapshot(camera: str, request: Request) -> Response:
    """Proxy the doorbell's own onboard snapshot, falling back to Frigate's
    `latest.jpg` on any console failure (M-2 spec point 7)."""
    settings = request.app.state.settings
    protect_id = _require_protect_id(settings, camera)
    subscriber: ProtectRingSubscriber | None = getattr(
        request.app.state, "protect_subscriber", None
    )
    jpeg: bytes | None = None
    if subscriber is not None:
        jpeg = await subscriber.fetch_snapshot(protect_id)
    if jpeg is None:
        jpeg = await fetch_thumbnail(
            frigate_api.get_async_client(request.app),
            frigate_base_url=settings.frigate.base_url,
            camera=camera,
            event_id="",
        )
    if jpeg is None:
        raise HTTPException(
            status_code=502,
            detail={"error": _ERR_CONSOLE_FAILED, "message": "no snapshot available"},
        )
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
