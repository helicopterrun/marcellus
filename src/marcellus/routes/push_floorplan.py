"""`/v1/push/floorplan` -- the layout map's background image (upload,
fetch, delete) and its magic-byte sniffer.

Split out of `routes/push.py`; same `/v1/push` prefix and auth.
"""

from __future__ import annotations

import asyncio
import pathlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from marcellus.push import policy_settings

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_BAD_FLOORPLAN = "bad_floorplan"
_ERR_FLOORPLAN_NOT_FOUND = "floorplan_not_found"
_FLOORPLAN_MAX_BYTES = 10 * 1024 * 1024
_FLOORPLAN_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """(ext, width, height) from the image's own header bytes — the upload
    is trusted on its magic bytes, never its Content-Type. PNG/JPEG/WebP
    only; a tiny hand parse so Pillow doesn't become a dependency for one
    dimensions read."""
    # byteorder is explicit everywhere: the "big" default only exists on
    # Python 3.11+, and prod runs 3.10 (an omission here 500'd every PNG).
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return (
            "png",
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    "jpg",
                    int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"),
                )
            i += 2 + length
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        fmt = data[12:16]
        if fmt == b"VP8X":
            w = int.from_bytes(data[24:27], "little") + 1
            h = int.from_bytes(data[27:30], "little") + 1
            return ("webp", w, h)
        if fmt == b"VP8 ":
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return ("webp", w, h)
        if fmt == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            return ("webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    return None


def _floorplan_file(request: Request) -> pathlib.Path | None:
    active = policy_settings.get_active()
    fp = active.get("floorplan")
    if not isinstance(fp, dict):
        return None
    base = pathlib.Path(request.app.state.settings.push.floorplan_path)
    path = base.with_suffix("." + fp["ext"])
    return path if path.exists() else None


@router.post("/floorplan")
async def upload_floorplan(request: Request) -> dict[str, Any]:
    """Store the layout map's background image. Raw image bytes as the body
    (no multipart -- the page POSTs the File object directly), identified by
    magic bytes. Replaces any previous floorplan and resets the scale
    calibration line -- a new image means the old line's coordinates are
    meaningless."""
    data = await request.body()
    if len(data) > _FLOORPLAN_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": "image exceeds 10 MB"},
        )
    sniffed = _sniff_image(data)
    if sniffed is None:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": "not a PNG, JPEG, or WebP image"},
        )
    ext, w, h = sniffed
    if not (0 < w <= 20000 and 0 < h <= 20000):
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_BAD_FLOORPLAN, "message": f"unreasonable dimensions {w}x{h}"},
        )

    settings = request.app.state.settings
    base = pathlib.Path(settings.push.floorplan_path)

    def _store_image() -> None:
        base.parent.mkdir(parents=True, exist_ok=True)
        tmp = base.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(base.with_suffix("." + ext))
        for other in _FLOORPLAN_MEDIA_TYPES:
            if other != ext:
                base.with_suffix("." + other).unlink(missing_ok=True)

    await asyncio.to_thread(_store_image)

    from datetime import datetime, timezone

    fp = {
        "ext": ext, "w": w, "h": h,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calibration": None,
    }
    active = dict(policy_settings.get_active())
    active["floorplan"] = fp
    policy_settings.save_settings(settings.push.push_settings_path, active)
    policy_settings.apply_settings(active)
    return {"floorplan": fp}


@router.get("/floorplan")
async def get_floorplan(request: Request) -> Response:
    path = _floorplan_file(request)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_FLOORPLAN_NOT_FOUND, "message": "no floorplan uploaded"},
        )
    return Response(
        content=await asyncio.to_thread(path.read_bytes),
        media_type=_FLOORPLAN_MEDIA_TYPES[path.suffix.lstrip(".")],
        headers={"Cache-Control": "no-cache"},
    )


@router.delete("/floorplan")
async def delete_floorplan(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    base = pathlib.Path(settings.push.floorplan_path)
    for ext in _FLOORPLAN_MEDIA_TYPES:
        base.with_suffix("." + ext).unlink(missing_ok=True)
    active = dict(policy_settings.get_active())
    active["floorplan"] = None
    policy_settings.save_settings(settings.push.push_settings_path, active)
    policy_settings.apply_settings(active)
    return {"ok": True}


