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
from marcellus.routes.tuning import _sections as _tuning_sections
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


#: Shared display vocabulary (mirrors policy.js / the app), ladder v3.
_PLACES = ("street", "yard", "doors", "off_limits")
_PLACE_LABELS = {
    "street": "Public", "yard": "Grounds", "doors": "Entrances",
    "off_limits": "Restricted",
}
# Ladder words (settings `ladder` values) and engine levels both resolve to
# the four user-facing words. `suppressed` is what `ladder.base_level`
# answers for an Ignore cell of a non-person subject; legacy `notify` reads
# as Quiet (it is what an old client's Quiet cell looks like).
_LEVEL_LABELS = {
    "ignore": "Ignore", "silent": "Quiet", "glance": "Glance", "urgent": "Urgent",
    "log": "Ignore", "quiet": "Glance", "notify": "Quiet", "suppressed": "Ignore",
    "off": "Ignore", "alarm": "Urgent",
}
_SUBJECTS = ("person", "vehicle", "animal", "thing", "package", "bin", "opening")
_SUBJECT_LABELS = {
    "person": "Person", "vehicle": "Vehicle", "animal": "Animal", "thing": "Thing",
    "package": "Package", "bin": "Bin", "opening": "Opening",
    # Subjects of the engine's own scenario vocabulary (notification examples).
    "stranger": "Unknown person", "known": "Known person",
}
_LEGACY_OUTCOME_TO_LADDER = {
    "off": "ignore", "log": "ignore", "notify": "silent", "glance": "glance",
    "alarm": "urgent",
}
_LADDER_ORDER = ("ignore", "silent", "glance", "urgent")


def _level_label(value: Any) -> str:
    """Display word for any level vocabulary; unknown values show raw."""
    return _LEVEL_LABELS.get(value, str(value)) if isinstance(value, str) else str(value)


def _ladder_from_outcomes(outcomes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Rollout fallback: derive a v3 `ladder` from legacy `outcomes` (§2.3).
    `private` merges into `yard`, louder wins."""
    out: dict[str, dict[str, Any]] = {}
    for subject, row in outcomes.items():
        if not isinstance(row, dict):
            continue
        cells: dict[str, Any] = {}
        for place, val in row.items():
            mapped = _LEGACY_OUTCOME_TO_LADDER.get(val, val) if isinstance(val, str) else val
            target = "yard" if place == "private" else place
            prev = cells.get(target)
            if prev in _LADDER_ORDER and mapped in _LADDER_ORDER:
                mapped = max(prev, mapped, key=_LADDER_ORDER.index)
            cells[target] = mapped
        out[subject] = cells
    return out


def _active_ladder() -> dict[str, dict[str, Any]]:
    active = policy_settings.get_active()
    ladder = active.get("ladder")
    if isinstance(ladder, dict) and ladder:
        return {k: v for k, v in ladder.items() if isinstance(v, dict)}
    outcomes = active.get("outcomes")
    return _ladder_from_outcomes(outcomes) if isinstance(outcomes, dict) else {}


def _ladder_matrix() -> dict[str, Any]:
    """The live attention-ladder table for display: subject rows x place
    columns -> level chips, plus any per-zone overrides in effect."""
    ladder = _active_ladder()
    subjects = [s for s in _SUBJECTS if s in ladder] + [
        s for s in ladder if s not in _SUBJECTS
    ]
    rows = []
    for subject in subjects:
        cells = ladder[subject]
        rows.append(
            {
                "subject": _SUBJECT_LABELS.get(subject, subject),
                "cells": [
                    {
                        "level": str(cells.get(p, "ignore")),
                        "label": _level_label(cells.get(p, "ignore")),
                    }
                    for p in _PLACES
                ],
            }
        )
    zone_overrides = policy_settings.get_active().get("zone_overrides") or {}
    overrides = [
        {
            "zone": zone,
            "subject": _SUBJECT_LABELS.get(subj, subj),
            "label": _level_label(lvl),
        }
        for zone, per_subject in zone_overrides.items()
        if isinstance(per_subject, dict)
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
                "level_label": _level_label(level),
                "situation": f"{_SUBJECT_LABELS[subject]} · {_PLACE_LABELS.get(place, place)}",
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
            "tuning_sections": _tuning_sections(),
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
