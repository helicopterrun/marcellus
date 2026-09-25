"""`GET /v1/protect/status` -- UniFi Protect device health for the app's
settings page (M-1: "Protect health & capability flag").

Authenticated like every other sidecar-owned `/v1` route (no entry in
`auth.EXEMPT_PATHS`) -- unlike `/v1/capabilities`, this carries console
version and per-camera state, not just a feature flag.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from marcellus import db
from marcellus.push import store

router = APIRouter(prefix="/v1/protect", tags=["protect"])


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
