"""`/v1/push/settings` -- the attention-ladder policy document, plus the
Frigate-config snapshot refresh that keeps its derived vocab (zones,
openings, cameras) honest on dev instances.

Split out of `routes/push.py`; same `/v1/push` prefix and auth.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from marcellus import db
from marcellus.push import card_store, policy_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_INVALID_SETTINGS = "invalid_settings"
_ERR_STALE_REV = "stale_settings_rev"
_ERR_CARD_NOT_FOUND = "card_not_found"
_ERR_BAD_SCOPE = "bad_scope"

# The settings document's revision guards against two tabs clobbering each
# other last-write-wins: a web client sends back the rev it loaded and gets a
# 409 if someone else saved in between. Clients that never send `rev` (the
# iOS app) keep the old behavior. The rev lives *in the settings file*
# (`policy_settings.read_rev`/`save_settings`) rather than process memory --
# an in-process counter reset to 1 on every restart, silently re-admitting
# any pre-deploy stale rev.


@router.get("/settings")
async def get_push_settings(request: Request) -> dict[str, Any]:
    """The attention-ladder policy (Elsinore Phase 4): the routing table,
    zone-class assignments, and Live Activity family toggles, plus enough
    about the live Frigate config (`available_zones`/`available_openings`)
    for the app to render its settings screens without a second call.

    Returns the *live, applied* policy (`policy_settings.get_active()`), not
    a fresh disk read -- they're the same thing once `startup`/a prior `PUT`
    has run, and this guarantees `GET` can never show something other than
    what the routing engine is actually evaluating against right now. On a
    fresh install with no settings file yet, this is what creates one (with
    defaults) rather than leaving `GET` and the on-disk state to silently
    disagree until the first `PUT`.
    """
    settings = request.app.state.settings
    active = policy_settings.get_active()
    settings_path = pathlib.Path(settings.push.push_settings_path)
    if not settings_path.exists():
        policy_settings.save_settings(settings_path, active)

    # Re-read friendly names on every GET (a cheap yaml load): editing
    # Frigate's config must show up here without a sidecar restart.
    policy_settings.load_zone_display_names(settings.frigate.config_path)

    from marcellus.zones import load_camera_zones

    available_cameras = sorted(load_camera_zones(settings.frigate.config_path).keys())
    derived_headings = {
        cam: vec
        for cam in available_cameras
        if (vec := policy_settings.derived_camera_heading(cam, active)) is not None
    }

    return {
        "settings": active,
        "rev": policy_settings.read_rev(settings_path),
        "available_cameras": available_cameras,
        "derived_headings": derived_headings,
        # Response key predates the settings-backed optics table; kept so
        # existing consumers (the app) need no change.
        "placement_deployments": {
            cam: dict(entry)
            for cam, entry in active.get("camera_optics", {}).items()
            if isinstance(entry, dict)
        },
        "available_zones": policy_settings.build_available_zones(settings.frigate.config_path),
        "available_openings": policy_settings.build_available_openings(
            settings.frigate.config_path
        ),
        "recognition_available": policy_settings.probe_recognition_available(
            settings.frigate.config_path
        ),
    }


@router.put("/settings")
async def put_push_settings(request: Request) -> dict[str, Any]:
    """Validate, persist, and immediately apply a new policy document.

    The body is the same shape `GET` returns under `settings` -- not the
    wrapper with `available_zones`/`available_openings`, which are derived,
    read-only, and never round-tripped back in. Unknown top-level fields are
    ignored (forward compat); an unknown subject/place/family key inside a
    known block, or an invalid level/place-class value, is a 400.
    """
    body = await request.json()
    settings = request.app.state.settings
    client_rev = body.pop("rev", None) if isinstance(body, dict) else None
    current_rev = policy_settings.read_rev(settings.push.push_settings_path)
    if isinstance(client_rev, int) and client_rev != current_rev:
        raise HTTPException(
            status_code=409,
            detail={
                "error": _ERR_STALE_REV,
                "detail": "Settings changed elsewhere — reload before saving.",
            },
        )
    errors = policy_settings.validate_settings(body)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID_SETTINGS, "detail": errors},
        )

    # la_only is sticky: normalize fills absent keys from *defaults*, and the
    # app's settings model round-trips through a fixed Codable type that drops
    # keys it doesn't know — so a client that omits la_only must not silently
    # reset it. Only an explicit boolean in the body changes it. Same for the
    # config-side-only keys (camera_neighbors/...) and the nullable ones
    # (secure_area/...) -- see `policy_settings.save_and_apply`.
    _merged, new_rev = policy_settings.save_and_apply(settings.push.push_settings_path, body)
    return {"ok": True, "rev": new_rev}


_ERR_CONFIG_REFRESH = "config_refresh_failed"


@router.post("/frigate-config/refresh")
async def refresh_frigate_config(request: Request) -> dict[str, Any]:
    """Re-sync the sidecar's Frigate-config copy from Frigate itself.

    A deployment whose `frigate.config_path` points at the live config file
    (prod) never needs this — camera/zone reads go to that file per request.
    A dev instance reads a *snapshot*, which goes stale the moment cameras
    or zones are renamed in Frigate; this fetches `/api/config/raw` from the
    authenticated origin (with the requester's own session cookie, so it
    grants nothing the caller doesn't already have) and rewrites the
    snapshot when it changed."""
    import httpx

    from marcellus.frigate_api import get_async_client

    settings = request.app.state.settings
    if not settings.frigate.config_refresh_enabled:
        # On prod `frigate.config_path` is Frigate's live config.yml -- an
        # overwrite here would clobber it. Only a deployment that has declared
        # its copy to be a sidecar-owned snapshot may refresh it.
        raise HTTPException(
            status_code=403,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": "frigate.config_refresh_enabled is off -- refusing to "
                "overwrite frigate.config_path",
            },
        )
    upstream = settings.frigate.proxy_base_url.rstrip("/") + "/api/config/raw"
    client = get_async_client(request.app)
    try:
        resp = await client.get(
            upstream,
            headers={"cookie": request.headers.get("cookie", "")},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": _ERR_CONFIG_REFRESH, "message": str(exc)},
        ) from exc
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": f"frigate answered HTTP {resp.status_code}",
            },
        )
    raw = resp.text
    try:
        import yaml

        parsed = yaml.safe_load(raw)
    except Exception:
        parsed = None
    if not (isinstance(parsed, dict) and isinstance(parsed.get("cameras"), dict)):
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_CONFIG_REFRESH,
                "message": "response did not look like a Frigate config",
            },
        )

    path = pathlib.Path(settings.frigate.config_path)

    def _rewrite_snapshot() -> bool:
        current = path.read_text() if path.exists() else None
        if raw == current:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if current is not None:
            # Keep the outgoing content: this endpoint is the only writer that
            # can destroy a config it didn't author.
            path.with_suffix(path.suffix + ".bak").write_text(current)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(raw)
        tmp.replace(path)
        return True

    changed = await asyncio.to_thread(_rewrite_snapshot)
    if changed:
        policy_settings.load_zone_display_names(path)

    from marcellus.zones import load_camera_zones

    return {"changed": changed, "cameras": sorted(load_camera_zones(path).keys())}


class SilenceRequest(BaseModel):
    card_key: str


class OverrideRequest(BaseModel):
    """`PUT /v1/push/overrides` body (alerts-slice1 §C). No `card_key` --
    this is a direct edit of the policy document by (kind, zone/subject/place),
    not a card lookup, so the required fields vary by `kind`."""

    kind: Literal["zone_override", "outcome_cell"]
    zone: str | None = None
    subject: str | None = None
    place: str | None = None
    level: str | None = None


def _scope_for_card(row: Any) -> dict[str, Any]:
    """Derive the silence scope from a `push_cards` row (§C): a card with a
    `zone_name` is scoped to that zone's per-subject override; otherwise it's
    scoped to the subject/place outcome cell."""
    if row["zone_name"]:
        return {
            "kind": "zone_override",
            "zone": row["zone_name"],
            "subject": row["subject_kind"],
            "camera": row["camera"],
            "place": row["place_class"],
        }
    return {
        "kind": "outcome_cell",
        "subject": row["subject_kind"],
        "camera": row["camera"],
        "place": row["place_class"],
    }


def _insert_silence(
    conn: Any,
    *,
    card_key: str,
    kind: str,
    zone: str,
    subject: str,
    place: str,
    previous: str | None,
    applied: str,
) -> None:
    conn.execute(
        "INSERT INTO push_silences (ts, card_key, kind, zone, subject, place, previous, applied) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            card_key,
            kind,
            zone,
            subject,
            place,
            previous,
            applied,
        ),
    )


@router.post("/silence")
async def silence_card(body: SilenceRequest, request: Request) -> dict[str, Any]:
    """Silence the cell (zone-override or outcome-table) that a given card
    routed through, down to `quiet` (alerts-slice1 §C). One-tap "quiet this"
    from the card detail screen -- the counterpart to the `/overrides`
    fine-grained editor."""
    settings = request.app.state.settings

    def _lookup(conn: Any) -> Any:
        return card_store.get_card_row(conn, body.card_key)

    row = await db.with_sidecar(settings.sidecar.db_path, _lookup)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_CARD_NOT_FOUND,
                "message": "no push card found for that card_key",
            },
        )

    scope = _scope_for_card(row)
    kind = scope["kind"]
    subject = scope["subject"]
    place = scope["place"]
    zone = scope.get("zone", "")
    active = policy_settings.get_active()

    if kind == "zone_override":
        previous = active.get("zone_overrides", {}).get(zone, {}).get(subject)
        zone_overrides = {z: dict(row_) for z, row_ in active.get("zone_overrides", {}).items()}
        zone_overrides.setdefault(zone, {})
        zone_overrides[zone][subject] = "quiet"
        full_body = {**active, "zone_overrides": zone_overrides}
    else:
        previous = active.get("outcomes", {}).get(subject, {}).get(place)
        # "quiet" is a *level* word; the outcomes table speaks a different
        # vocabulary (`OUTCOMES`) where its equivalent is "glance". Deep-copy
        # every subject's row (not just this cell) -- `normalize_settings`
        # merges a partial `outcomes` document over `default_settings()`, so
        # sending only `{subject: {place: ...}}` would reset every other
        # tuned cell (including this subject's other places) to defaults.
        outcomes = {s: dict(row_) for s, row_ in active.get("outcomes", {}).items()}
        outcomes.setdefault(subject, {})
        outcomes[subject][place] = policy_settings.LEVEL_TO_OUTCOME["quiet"]
        full_body = {**active, "outcomes": outcomes}

    errors = policy_settings.validate_settings(full_body)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID_SETTINGS, "detail": errors},
        )
    policy_settings.save_and_apply(settings.push.push_settings_path, full_body)

    def _record(conn: Any) -> None:
        _insert_silence(
            conn,
            card_key=body.card_key,
            kind=kind,
            zone=zone,
            subject=subject,
            place=place,
            previous=previous,
            applied="quiet",
        )
        conn.commit()

    await db.with_sidecar(settings.sidecar.db_path, _record)

    logger.info(
        "push: silence applied card_key=%s scope=%s previous=%s",
        body.card_key,
        scope,
        previous,
    )
    response_scope = dict(scope)
    if kind == "outcome_cell":
        response_scope.pop("zone", None)
    return {"scope": response_scope, "previous": previous, "applied": "quiet"}


@router.put("/overrides")
async def put_override(body: OverrideRequest, request: Request) -> dict[str, Any]:
    """Direct edit of one zone-override or outcome cell (alerts-slice1 §C),
    for the settings screen's fine-grained editor -- no card lookup, the
    caller names the cell directly.

    Resolution (no spec case covers this): there is no `card_key` here, so
    "404 unknown card" doesn't apply; instead, missing required fields for
    the given `kind` are a 422 (`zone`+`subject` for `zone_override`,
    `subject`+`place` for `outcome_cell`). `level=None` is only valid for
    `zone_override` (it removes the override); an `outcome_cell` always has a
    current level, so `null` there is rejected rather than silently choosing
    one to fall back to.
    """
    settings = request.app.state.settings
    kind = body.kind

    if kind == "zone_override":
        if not body.zone or not body.subject:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": _ERR_BAD_SCOPE,
                    "message": "zone_override requires zone and subject",
                },
            )
        if body.level is not None and body.level not in policy_settings.LEVELS:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": _ERR_BAD_SCOPE,
                    "message": f"level must be one of {policy_settings.LEVELS} or null",
                },
            )
        zone = body.zone
        subject = body.subject
        place = ""
    else:
        if not body.subject or not body.place:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": _ERR_BAD_SCOPE,
                    "message": "outcome_cell requires subject and place",
                },
            )
        if body.level is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": _ERR_BAD_SCOPE,
                    "message": "an outcomes cell cannot be null -- send the previous level",
                },
            )
        allowed = (*policy_settings.LEVELS, "off")
        if body.level not in allowed:
            raise HTTPException(
                status_code=422,
                detail={"error": _ERR_BAD_SCOPE, "message": f"level must be one of {allowed}"},
            )
        zone = ""
        subject = body.subject
        place = body.place

    active = policy_settings.get_active()
    if kind == "zone_override":
        previous = active.get("zone_overrides", {}).get(zone, {}).get(subject)
        zone_overrides = {z: dict(row_) for z, row_ in active.get("zone_overrides", {}).items()}
        if body.level is None:
            zone_overrides.get(zone, {}).pop(subject, None)
            if zone in zone_overrides and not zone_overrides[zone]:
                zone_overrides.pop(zone, None)
        else:
            zone_overrides.setdefault(zone, {})
            zone_overrides[zone][subject] = body.level
        full_body = {**active, "zone_overrides": zone_overrides}
        scope: dict[str, Any] = {"kind": kind, "zone": zone, "subject": subject}
    else:
        previous = active.get("outcomes", {}).get(subject, {}).get(place)
        # `body.level` is in the level vocabulary (LEVELS + "off"); the
        # outcomes table speaks OUTCOMES (off/log/glance/notify/alarm) --
        # translate at this one boundary, same as `/silence`. Already
        # checked non-None above; narrow explicitly for mypy.
        assert body.level is not None  # validated above
        level_value: str = body.level
        outcome = policy_settings.LEVEL_TO_OUTCOME.get(level_value, level_value)
        # Deep-copy every subject's row -- see the matching comment in
        # `/silence` above for why a bare `{subject: {place: ...}}` would
        # reset every other tuned outcome cell to defaults.
        outcomes = {s: dict(row_) for s, row_ in active.get("outcomes", {}).items()}
        outcomes.setdefault(subject, {})
        outcomes[subject][place] = outcome
        full_body = {**active, "outcomes": outcomes}
        scope = {"kind": kind, "subject": subject, "place": place}

    errors = policy_settings.validate_settings(full_body)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"error": _ERR_INVALID_SETTINGS, "detail": errors},
        )
    policy_settings.save_and_apply(settings.push.push_settings_path, full_body)

    # A pure removal (zone_override level=None) applies nothing to audit --
    # there's no new silence in effect, just one lifted.
    if body.level is not None:

        def _record(conn: Any) -> None:
            _insert_silence(
                conn,
                card_key="",
                kind=kind,
                zone=zone,
                subject=subject,
                place=place,
                previous=previous,
                applied=body.level or "",
            )
            conn.commit()

        await db.with_sidecar(settings.sidecar.db_path, _record)

    logger.info(
        "push: override applied scope=%s previous=%s applied=%s",
        scope,
        previous,
        body.level,
    )
    return {"scope": scope, "previous": previous, "applied": body.level}
