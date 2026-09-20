"""`/v1/push/map/*` -- floorplan-projected views of the world model:
zone polygons, camera footprints, live fused positions, single-event
trails, and the landmark calibration solver.

Split out of `routes/push.py`; same `/v1/push` prefix and auth.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from marcellus.errors import error_detail
from marcellus.models.wire import MapLiveResponse, MapTrackResponse
from marcellus.push import policy_settings

router = APIRouter(prefix="/v1/push", tags=["push"])

@router.get("/map/zones")
async def map_zones(request: Request) -> dict[str, Any]:
    """Frigate zone polygons projected onto the floorplan map.

    One entry per (camera, zone): a zone watched by several cameras yields
    several overlapping polygons — overlap is honest coverage evidence, so
    the UI draws all of them translucently rather than merging. Cameras
    without layout/optics/scale and full-frame gate zones are omitted;
    `clipped` marks polygons cut at the horizon/range limit (some source
    vertices didn't project).
    """
    import time as _time

    from marcellus.push import fusion, ground
    from marcellus.zones import is_full_frame, load_camera_zones

    settings = request.app.state.settings
    active = policy_settings.get_active()
    layout_table = active.get("camera_layout") or {}
    scale_ft = active.get("map_scale_ft")
    aspect = ground.map_aspect(active)
    zones: list[dict[str, Any]] = []
    if scale_ft and scale_ft > 0:
        for camera, zone_list in load_camera_zones(settings.frigate.config_path).items():
            layout = layout_table.get(camera)
            if not layout:
                continue
            for zone in zone_list:
                if is_full_frame(zone["coords"]):
                    continue
                pts = fusion.project_polygon(
                    zone["coords"], camera=camera, layout_entry=layout,
                    scale_ft=scale_ft, aspect_h_over_w=aspect,
                )
                if pts is None:
                    continue
                zones.append({
                    "camera": camera,
                    "name": zone["name"],
                    "color": zone["color"],
                    "objects": zone["objects"],
                    "points": [[round(x, 4), round(y, 4)] for x, y in pts],
                    "clipped": len(pts) != len(zone["coords"]),
                })
    return {"t": _time.time(), "aspect": aspect, "zones": zones}


@router.get("/map/footprints")
async def map_footprints(request: Request) -> dict[str, Any]:
    """Each placed camera's true projected ground footprint: the full
    image frame pushed through its optics onto the floorplan, densified
    and clipped at the horizon/range limit — the honest coverage view.
    Cameras without layout/optics/scale are omitted."""
    import time as _time

    from marcellus.push import fusion, ground

    active = policy_settings.get_active()
    layout_table = active.get("camera_layout") or {}
    scale_ft = active.get("map_scale_ft")
    aspect = ground.map_aspect(active)
    frame = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    footprints: list[dict[str, Any]] = []
    if scale_ft and scale_ft > 0:
        for camera in sorted(layout_table):
            layout = layout_table[camera]
            pts = fusion.project_polygon(
                frame, camera=camera, layout_entry=layout,
                scale_ft=scale_ft, aspect_h_over_w=aspect,
            )
            if pts is None:
                continue
            footprints.append({
                "camera": camera,
                "points": [[round(x, 4), round(y, 4)] for x, y in pts],
                "clipped": len(pts) != len(frame),
            })
    return {"t": _time.time(), "aspect": aspect, "footprints": footprints}


@router.get(
    "/map/live",
    response_model=MapLiveResponse,
    response_model_exclude_unset=True,
)
async def map_live(request: Request, debug: int = Query(default=0)) -> dict[str, Any]:
    """Current fused object positions on the floorplan map, for the /cameras
    Live overlay (polled ~1 Hz). Cross-camera sightings of the same object
    merge into one entry listing every contributing camera.

    Tracks are MQTT-fed in-memory state: if the broker/Frigate goes dark the
    engine just stops updating and this would otherwise keep serving the
    last-known (or empty) positions as if everything were fine forever. When
    the same `push_subscriber` that /healthz's `mqtt` check reads has been
    silent past `push.offline_silence_s`, the response carries `"stale":
    true` so the map UI can show a banner instead of a confidently wrong
    scene. Omitted entirely when the feed is fresh (additive, not a
    breaking-change key)."""
    import time as _time

    from marcellus.push import fusion, ground

    engine = getattr(request.app.state, "push_engine", None)
    active = policy_settings.get_active()
    scale_ft = active.get("map_scale_ft")
    now = _time.time()
    objects: list[dict[str, Any]] = []
    if engine is not None and scale_ft and scale_ft > 0:
        positions = fusion.track_world_positions(engine.tracks, active, now=now)
        aspect = ground.map_aspect(active)
        for c in fusion.cluster(positions, scale_ft=scale_ft, aspect_h_over_w=aspect):
            entry: dict[str, Any] = {
                "x": round(c.x, 4),
                "y": round(c.y, 4),
                "label": c.label,
                "stationary": c.stationary,
                "cameras": [m.camera for m in c.members],
                "track_ids": [m.track_id for m in c.members],
            }
            if debug:
                entry["members"] = [
                    {
                        "camera": m.camera, "track_id": m.track_id,
                        "x": round(m.x, 4), "y": round(m.y, 4),
                        "forward_ft": round(m.forward_ft, 1),
                        "age_s": round(m.age_s, 2),
                    }
                    for m in c.members
                ]
            objects.append(entry)
    result: dict[str, Any] = {"t": now, "objects": objects}
    subscriber = getattr(request.app.state, "push_subscriber", None)
    if subscriber is not None and subscriber.is_stale(now=now):
        result["stale"] = True
    return result


@router.get("/map/track", response_model=MapTrackResponse)
async def map_track(
    request: Request, camera: str = Query(...), event_id: str = Query(...),
) -> dict[str, Any]:
    """One event's trail projected onto the floorplan map, for the app's
    event mini-map. Live tracks come from the engine's track store; ended
    events fall back to the MQTT flight recorder (same source as
    /replay/capture-window). 404 `not_projectable` whenever the world model
    can't answer — the app renders nothing rather than a guess."""
    import time as _time

    from marcellus.push import ground
    from marcellus.routes.replay import _capture_paths, _capture_tracks

    active = policy_settings.get_active()
    scale_ft = active.get("map_scale_ft")
    layout = (active.get("camera_layout") or {}).get(camera)
    if (
        not scale_ft or scale_ft <= 0 or not layout
        or layout.get("azimuth") is None or ground.camera_ground(camera) is None
    ):
        raise HTTPException(
            status_code=404,
            detail=error_detail("not_projectable", "camera not calibrated for map projection"),
        )

    path_data: list[Any] | None = None
    engine = getattr(request.app.state, "push_engine", None)
    if engine is not None:
        state = engine.tracks.get(camera, event_id)
        if state is not None and state.path_data:
            path_data = list(state.path_data)
    if path_data is None:
        # Flight-recorder fallback: last 24 h, this camera only.
        rows = _capture_tracks(
            _capture_paths(request.app.state.settings),
            _time.time() - 24 * 3600.0, camera=camera,
        )
        for row in rows:
            if row["track_id"] == event_id:
                path_data = row["points"]
                break
    if not path_data:
        raise HTTPException(
            status_code=404,
            detail=error_detail("not_projectable", "camera not calibrated for map projection"),
        )

    aspect = ground.map_aspect(active)
    projected: list[list[float]] = []
    for pt in path_data:
        wp = ground.world_position(
            pt[0], pt[1], camera=camera, layout_entry=layout,
            scale_ft=scale_ft, aspect_h_over_w=aspect,
        )
        if wp is not None:
            projected.append([round(wp[0], 4), round(wp[1], 4)])
    if len(projected) < 2:
        raise HTTPException(
            status_code=404,
            detail=error_detail("not_projectable", "camera not calibrated for map projection"),
        )

    if len(projected) > 60:  # even decimation, endpoints preserved
        stride = (len(projected) - 1) / 59
        projected = [projected[round(k * stride)] for k in range(60)]

    secure = active.get("secure_area")
    distances = [
        d for p in projected
        if (d := ground.distance_to_secure_ft(
            p[0], p[1], secure, scale_ft=scale_ft, aspect_h_over_w=aspect,
        )) is not None
    ]
    speed = ground.speed_ft_s(path_data, camera)
    return {
        "points_map": projected,
        "camera": {"x": layout.get("x", 0.0), "y": layout.get("y", 0.0)},
        "secure_area": secure if isinstance(secure, dict) else None,
        "aspect": round(aspect, 4),
        "speed_ft_s": round(speed, 1) if speed is not None else None,
        "distance_ft_range": (
            [round(min(distances), 1), round(max(distances), 1)] if distances else None
        ),
    }


@router.post("/map/landmark-solve")
async def map_landmark_solve(request: Request) -> dict[str, Any]:
    """Solve one camera's HFOV/azimuth/tilt from landmark matches.

    Body: `{"camera": str, "matches": [{"u","v","mx","my"}, ...]}` — each
    match pairs a click in the camera frame with the same physical spot
    clicked on the calibrated floorplan. Pure preview: returns the solved
    values + per-match residuals; the /cameras page applies accepted
    numbers through the normal Save flow.
    """
    from marcellus.push import calibrate

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_body", "body must be an object")
        )
    camera = body.get("camera")
    matches = body.get("matches")
    if not isinstance(camera, str) or not isinstance(matches, list):
        raise HTTPException(
            status_code=400,
            detail=error_detail("invalid_body", "camera and matches required"),
        )
    if not (2 <= len(matches) <= 12):
        raise HTTPException(
            status_code=400,
            detail=error_detail("invalid_matches", "need 2-12 landmark matches"),
        )
    clean = []
    for m in matches:
        try:
            entry = {k: float(m[k]) for k in ("u", "v", "mx", "my")}
        except (TypeError, KeyError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "invalid_matches", "each match needs numeric u, v, mx, my"
                ),
            ) from None
        if not all(-0.5 <= v <= 1.5 for v in entry.values()):
            raise HTTPException(
                status_code=400,
                detail=error_detail("invalid_matches", "match coords out of range"),
            )
        clean.append(entry)
    try:
        return calibrate.solve_landmarks(camera, clean, policy_settings.get_active())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_matches", str(exc))
        ) from exc


