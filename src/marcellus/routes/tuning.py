"""`GET`/`PUT /v1/tuning` -- the runtime tuning-override document (Part A5 of
docs/settings-dial): effective config + user overrides for every knob the
`tuning` registry covers, behind the normal auth middleware like every other
`/v1` route.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from marcellus import tuning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["tuning"])

_ERR_INVALID = "invalid_tuning"
_ERR_STALE_REV = "stale_rev"

_SECTION_TITLES: dict[str, str] = {
    "": "General",
    "sidecar": "Sidecar",
    "frigate": "Frigate",
    "push": "Push",
    "encounters": "Encounters",
    "scrub": "Scrub cache",
    "face_capture": "Face capture",
    "face_enrich": "Face enrichment",
    "watchdog": "Watchdog",
    "proxy": "Proxy",
}


def _sections() -> list[dict[str, str]]:
    return [{"name": name, "title": title} for name, title in _SECTION_TITLES.items()]


@router.get("/tuning")
async def get_tuning(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    path = tuning.overrides_path(settings)
    overrides = tuning.read_overrides(path)
    return {
        "rev": tuning.read_rev(path),
        "knobs": tuning.effective(settings, overrides),
        "pending_restart": tuning.pending_restart(settings, overrides),
        "sections": _sections(),
        "overrides": overrides,
    }


@router.put("/tuning")
async def put_tuning(request: Request) -> dict[str, Any]:
    """Full replacement of the override set. A key with value `null`, or a
    key simply absent from the new set, removes that override -- the field
    reverts to its base (yaml/env/default) value from the startup snapshot.
    """
    settings = request.app.state.settings
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID, "detail": ["body must be a JSON object"]},
        )

    path = tuning.overrides_path(settings)
    client_rev = body.get("rev")
    current_rev = tuning.read_rev(path)
    if isinstance(client_rev, int) and client_rev != current_rev:
        raise HTTPException(
            status_code=409,
            detail={
                "error": _ERR_STALE_REV,
                "detail": "Tuning changed elsewhere — reload before saving.",
            },
        )

    raw_overrides = body.get("overrides")
    if not isinstance(raw_overrides, dict):
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID, "detail": ["overrides must be a JSON object"]},
        )
    new_overrides = {k: v for k, v in raw_overrides.items() if v is not None}

    errors = tuning.validate(new_overrides, settings)
    if errors:
        raise HTTPException(status_code=400, detail={"error": _ERR_INVALID, "detail": errors})

    old_overrides = tuning.read_overrides(path)
    snapshot = tuning.get_startup_snapshot() or {}
    removed_keys = set(old_overrides) - set(new_overrides)

    changed: list[tuple[str, Any, Any]] = []
    for key in removed_keys:
        knob = tuning.KNOBS_BY_KEY.get(key)
        if knob is None:
            continue
        old_value = tuning.get_field(settings, knob)
        base_value = tuning.snapshot_value(snapshot, knob)
        if old_value != base_value:
            changed.append((key, old_value, base_value))
        tuning.set_field(settings, knob, base_value)

    for key, value in new_overrides.items():
        knob = tuning.KNOBS_BY_KEY.get(key)
        if knob is None:
            continue
        old_value = tuning.get_field(settings, knob)
        if old_value != value:
            changed.append((key, old_value, value))

    tuning.apply_overrides(settings, new_overrides)

    for key, old_value, new_value in changed:
        logger.info("tuning: %s %s -> %s", key, old_value, new_value)

    new_rev = tuning.write_overrides(path, new_overrides)
    return {
        "rev": new_rev,
        "knobs": tuning.effective(settings, new_overrides),
        "pending_restart": tuning.pending_restart(settings, new_overrides),
        "sections": _sections(),
        "overrides": new_overrides,
    }
