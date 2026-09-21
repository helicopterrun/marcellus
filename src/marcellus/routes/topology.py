"""Camera topology (M2, docs/encounters.md "Camera topology"): `/v1/topology`
(the full adjacency graph plus learned/config/default transition stats) and
`/v1/cameras/{camera}/neighbours` (one camera's directed edges). Read-only,
same auth as the rest of `/v1/encounters*`/`/v1/observations`.

`/v1/encounters/adjacency` (`routes/encounters.py`) stays byte-identical --
this module builds its own richer response on top of the same `Adjacency`
object rather than changing that route.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from marcellus import db
from marcellus.encounters.transitions import TransitionStats, load_transitions
from marcellus.errors import error_detail
from marcellus.models.wire import CameraNeighboursResponse, TopologyResponse
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes.encounters import _adjacency_for

v1_router = APIRouter(prefix="/v1", tags=["v1"])


def _stats_out(stats: TransitionStats) -> dict[str, Any]:
    return {
        "p10": stats.p10,
        "p50": stats.p50,
        "p90": stats.p90,
        "samples": stats.samples,
        "source": stats.source,
    }


def _edge_transitions(
    transitions: dict[tuple[str, str, str], TransitionStats], cam_a: str, cam_b: str
) -> dict[str, dict[str, Any]]:
    """`{"a>b": {family: stats}, "b>a": {family: stats}}` for one undirected
    edge, from every `camera_transitions` row touching either direction."""
    forward: dict[str, Any] = {}
    backward: dict[str, Any] = {}
    for (a, b, family), stats in transitions.items():
        if a == cam_a and b == cam_b:
            forward[family] = _stats_out(stats)
        elif a == cam_b and b == cam_a:
            backward[family] = _stats_out(stats)
    return {f"{cam_a}>{cam_b}": forward, f"{cam_b}>{cam_a}": backward}


@v1_router.get("/topology", response_model=TopologyResponse)
async def topology(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    adjacency = _adjacency_for(request)
    graph = adjacency.to_json()

    def _load(conn: Any) -> dict[tuple[str, str, str], TransitionStats]:
        return load_transitions(conn)

    transitions = await db.with_sidecar(settings.sidecar.db_path, _load)

    edges = [
        dict(
            edge,
            transitions=_edge_transitions(transitions, edge["a"], edge["b"]),
        )
        for edge in graph["edges"]
    ]
    return {"cameras": graph["cameras"], "edges": edges}


@v1_router.get("/cameras/{camera}/neighbours", response_model=CameraNeighboursResponse)
async def camera_neighbours(camera: str, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    adjacency = _adjacency_for(request)
    graph = adjacency.to_json()
    if camera not in graph["cameras"]:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "unknown camera"))

    def _load(conn: Any) -> dict[tuple[str, str, str], TransitionStats]:
        return load_transitions(conn)

    transitions = await db.with_sidecar(settings.sidecar.db_path, _load)

    zones_by_pair = {
        frozenset((e["a"], e["b"])): e["zones"] for e in graph["edges"]
    }
    neighbours = []
    for other in sorted(adjacency.neighbours(camera)):
        zones = zones_by_pair.get(frozenset((camera, other)), [])
        cam_transitions = {
            family: _stats_out(stats)
            for (a, b, family), stats in transitions.items()
            if a == camera and b == other
        }
        neighbours.append({"camera": other, "zones": zones, "transitions": cam_transitions})
    return {"camera": camera, "neighbours": neighbours}
