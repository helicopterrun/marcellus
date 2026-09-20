"""Unified settings page, organized like the app's SettingsView.

Absorbs the old /zones (routing policy, neighbors, export/import — its
section markup keeps the same element ids so zones.js runs unchanged) and
/devices (registration table + test sends); adds a per-camera rig-facts
summary with landmark-calibrate deep links, read-only faces config, and the
about/debug block. /zones and /devices redirect here.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from marcellus import __version__, db
from marcellus.push import policy_settings, store
from marcellus.routes import scrub as scrub_routes
from marcellus.zones import load_camera_zones

router = APIRouter(tags=["settings"])


def _camera_summary(settings: Any) -> list[dict[str, Any]]:
    """Rig facts per camera: placed/aimed state + optics, for the summary
    table. Names come from the live Frigate config, like /cameras."""
    active = policy_settings.get_active()
    layout = active.get("camera_layout", {}) or {}
    optics = active.get("camera_optics", {}) or {}
    try:
        cameras = sorted(load_camera_zones(settings.frigate.config_path).keys())
    except Exception:  # noqa: BLE001 — a missing Frigate config is an empty list, not a 500
        cameras = sorted(set(layout) | set(optics))
    rows = []
    for cam in cameras:
        entry = layout.get(cam) or {}
        opt = optics.get(cam) or {}
        rows.append(
            {
                "camera": cam,
                "placed": "x" in entry and "y" in entry,
                "azimuth": entry.get("azimuth"),
                "fov": entry.get("fov"),
                "hfov": opt.get("hfov"),
                "mount_ft": opt.get("mount_ft"),
                "tilt_deg": opt.get("tilt_deg"),
            }
        )
    return rows


def _service_rows(settings: Any) -> list[dict[str, str]]:
    """Read-only summary of the YAML-only feature switches, so the settings
    page reflects the whole configuration surface (edits happen in the
    sidecar's config file; the guide link explains each one)."""

    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

    return [
        {"name": "Scrub cache", "value": onoff(settings.scrub.enabled), "guide": "/guide/scrub"},
        {
            "name": "Push",
            "value": f"{onoff(settings.push.enabled)} · {settings.push.transport}",
            "guide": "/guide/push-notifications",
        },
        {
            "name": "Face capture",
            "value": onoff(settings.face_capture.enabled),
            "guide": "/guide/faces-pipeline",
        },
        {
            "name": "Face enrichment",
            "value": onoff(settings.face_enrich.enabled),
            "guide": "/guide/identities",
        },
        {"name": "Watchdog", "value": onoff(settings.watchdog.enabled), "guide": "/guide/settings"},
        {"name": "Proxy", "value": onoff(settings.proxy.enabled), "guide": "/guide/settings"},
        {"name": "Log level", "value": settings.log_level, "guide": "/guide/first-run"},
    ]


#: Shared display vocabulary (mirrors zones.js / the app).
_PLACES = ("street", "yard", "doors", "private", "off_limits")
_PLACE_LABELS = {
    "street": "Public", "yard": "Semi-private", "doors": "Entry / exit",
    "private": "Private", "off_limits": "Restricted",
}
_LEVEL_LABELS = {"log": "Log", "quiet": "Glance", "notify": "Notify", "urgent": "Alarm"}
_SUBJECT_LABELS = {
    "stranger": "Unknown person", "known": "Known person",
    "animal": "Animal", "thing": "Vehicle / thing",
}


def _ladder_matrix() -> dict[str, Any]:
    """The live attention-ladder table for display: subject rows x place
    columns -> level chips, plus any per-zone overrides in effect."""
    from marcellus.push import ladder_policy as policy

    rows = []
    for subject, cells in policy.TABLE.items():
        rows.append(
            {
                "subject": _SUBJECT_LABELS.get(subject, subject),
                "cells": [
                    {"level": cells.get(p, "log"), "label": _LEVEL_LABELS[cells.get(p, "log")]}
                    for p in _PLACES
                ],
            }
        )
    overrides = [
        {"zone": zone, "subject": _SUBJECT_LABELS.get(subj, subj), "label": _LEVEL_LABELS[lvl]}
        for zone, per_subject in policy.ZONE_OVERRIDES.items()
        for subj, lvl in per_subject.items()
    ]
    return {
        "places": [_PLACE_LABELS[p] for p in _PLACES],
        "rows": rows,
        "overrides": overrides,
    }


def _notification_examples() -> list[dict[str, str]]:
    """Rendered example notifications, produced by the real copy composer
    (`delivery_wire._copy`) and the real ladder — so what's shown here is
    exactly what the phone would say for these situations."""
    from marcellus.push import ladder
    from marcellus.push.delivery_wire import _copy, _glyph_for

    scenarios = [
        # (subject, label, camera, zone, place, elapsed_s, identity, story)
        ("stranger", "person", "doorbell", "front_door", "doors", 40, "", ""),
        ("known", "person", "gate-face", "walkway", "doors", 0, "Sam", ""),
        ("animal", "dog", "alley-wide", "back_yard", "yard", 130, "", ""),
        ("thing", "car", "street", "driveway", "yard", 0, "", ""),
    ]
    emoji = {"person": "🧍", "dog": "🐕", "car": "🚗", "package": "📦"}
    out = []
    for subject, label, camera, zone, place, elapsed, identity, story in scenarios:
        primary, secondary = _copy(
            subject, label, camera, zone, elapsed, identity=identity, story=story
        )
        level, _ = ladder.base_level(subject, place, zone)
        out.append(
            {
                "glyph": _glyph_for(subject, label),
                "emoji": emoji.get(label, "〰️"),
                "primary": primary,
                "secondary": secondary,
                "level": level,
                "level_label": _LEVEL_LABELS[level],
                "situation": f"{_SUBJECT_LABELS[subject]} · {_PLACE_LABELS[place]}",
            }
        )
    return out


@router.get("/zones")
@router.get("/devices")
async def _settings_redirect() -> RedirectResponse:
    """/zones and /devices were absorbed into /settings (see module
    docstring) -- redirect rather than 404 for anyone with an old link or
    bookmark. Without a route here, these paths fall through to
    `proxy_routes`'s catch-all and return Frigate's SPA with HTTP 200.
    """
    return RedirectResponse(url="/settings")


@router.get("/settings", response_class=HTMLResponse)
async def settings_view(request: Request) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    devices = await db.with_sidecar(settings.sidecar.db_path, store.list_devices)

    caps = await scrub_routes.capabilities(request)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "devices": devices,
            "push_enabled": settings.push.enabled,
            "transport": settings.push.transport,
            "camera_rows": _camera_summary(settings),
            "service_rows": _service_rows(settings),
            "ladder": _ladder_matrix(),
            "notif_examples": _notification_examples(),
            "version": __version__,
            "capabilities_json": json.dumps(caps, indent=2, sort_keys=True),
            "cap_sections": [
                (k, json.dumps(v, indent=2, sort_keys=True)) for k, v in sorted(caps.items())
            ],
            "counts": {},
        },
    )
