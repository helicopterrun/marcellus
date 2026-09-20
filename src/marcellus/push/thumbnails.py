"""Pre-warming the snapshot the NSE will ask for.

Plan §4 lever 1, the largest controllable win in the latency chain: when a
situation matches, pull the frame from Frigate *now*, shrink it, and park it
under the push's handle. By the time APNs has carried the alert to the phone
(200ms-2s, out of our hands) and the extension wakes up, the fetch it makes is
against a warm local cache instead of a cold round-trip to Frigate inside a
~30s budget it does not control.

Two properties this module has to keep:

* **Small.** ~320px longest edge at q60 lands around 10-20KB. The NSE runs
  under a very tight memory ceiling (transport spec §3) and the phone may be
  on a cold radio; a full 1280x720 snapshot off this deployment is ~180KB and
  buys nothing at notification size.
* **Never load-bearing.** Every failure path returns None. A snapshot that
  can't be fetched, decoded, or resized costs the notification its image, not
  its existence (handoff item 12).

Note on Frigate: `snapshot.jpg` ignores `h=`/`quality=` on the deployed
version (verified live 2026-08-05 -- both return the full 1280x720, 181KB
frame), so the resize happens here rather than upstream.
"""

from __future__ import annotations

import asyncio
import io
import logging

import httpx

logger = logging.getLogger(__name__)

#: Longest edge, px. A notification thumbnail is displayed at a few hundred
#: points at most; past this the bytes are paid for and never seen.
DEFAULT_MAX_EDGE = 320
DEFAULT_QUALITY = 60


def resize_jpeg(raw: bytes, *, max_edge: int = DEFAULT_MAX_EDGE, quality: int = DEFAULT_QUALITY
                ) -> bytes | None:
    """Shrink a JPEG to `max_edge` on its longest side. None if undecodable.

    Uses JPEG's DCT scaling (`Image.draft`) to decode straight to roughly the
    target size instead of decoding 1280x720 and throwing most of it away --
    the same trick the scrub repair path uses, and it matters more here
    because this runs on the interrupt path.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.draft("RGB", (max_edge, max_edge))
            rgb = im.convert("RGB")
            longest = max(rgb.width, rgb.height)
            if longest > max_edge:
                scale = max_edge / longest
                rgb = rgb.resize(
                    (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - a bad frame must not cost the push
        logger.debug("push: could not resize snapshot", exc_info=True)
        return None


async def fetch_thumbnail(
    client: httpx.AsyncClient,
    *,
    frigate_base_url: str,
    camera: str,
    event_id: str,
    max_edge: int = DEFAULT_MAX_EDGE,
    quality: int = DEFAULT_QUALITY,
    timeout: float = 5.0,
) -> bytes | None:
    """The pre-warmed thumbnail for one match, or None if anything went wrong.

    Tries the event's own snapshot first (it is the frame the alert is
    actually about) and falls back to the camera's latest frame, which is what
    exists when a review item is seconds old and Frigate hasn't written a
    snapshot for the tracked object yet. Both are best-effort.
    """
    base = frigate_base_url.rstrip("/")
    candidates = []
    if event_id:
        candidates.append(f"{base}/api/events/{event_id}/snapshot.jpg")
    if camera:
        candidates.append(f"{base}/api/{camera}/latest.jpg")

    for url in candidates:
        try:
            resp = await client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            logger.debug("push: thumbnail fetch %s failed: %s", url, exc)
            continue
        if resp.status_code != 200 or not resp.content:
            continue
        # Pillow is CPU-bound; off the loop so a decode can't add latency to
        # whatever else the sidecar is serving (the proxy, live view, scrub).
        small = await asyncio.to_thread(
            resize_jpeg, resp.content, max_edge=max_edge, quality=quality
        )
        if small:
            return small
    return None
