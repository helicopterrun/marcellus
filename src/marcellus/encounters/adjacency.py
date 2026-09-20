"""Camera adjacency: which cameras' fields of view plausibly hand off to one
another, derived from Frigate's zone definitions (docs/encounters.md).

Rule: two cameras are adjacent when they define a zone with the *same name*
(`zones.load_camera_zones` output) -- the convention already used across this
codebase (e.g. `alley-wide` and `shed` both define a `back_walkway` zone
because it is physically the same ground). Config `encounters.adjacency`
adds edges Frigate's zone naming misses; `encounters.not_adjacent` removes
ones that are coincidentally named alike but not actually adjacent. Config
always wins over the zone-derived graph.

Footprint-overlap adjacency (comparing `camera_layout`/optics geometry
instead of zone names) is a documented follow-up, not implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Adjacency:
    """An undirected camera adjacency graph."""

    edges: frozenset[frozenset[str]]
    #: pair -> zone names shared by that pair (empty for a config-only edge).
    shared_zones: dict[frozenset[str], tuple[str, ...]] = field(default_factory=dict)

    def adjacent(self, a: str, b: str) -> bool:
        if a == b:
            return True
        return frozenset((a, b)) in self.edges

    def neighbours(self, cam: str) -> set[str]:
        out: set[str] = set()
        for edge in self.edges:
            if cam in edge:
                out |= edge - {cam}
        return out

    def to_json(self) -> dict[str, Any]:
        cameras: set[str] = set()
        for edge in self.edges:
            cameras |= edge
        edges_out = []
        for edge in sorted(self.edges, key=lambda e: tuple(sorted(e))):
            a, b = sorted(edge)
            zones = self.shared_zones.get(edge, ())
            edges_out.append(
                {
                    "a": a,
                    "b": b,
                    "zones": list(zones),
                    "source": "zones" if zones else "config",
                }
            )
        return {"cameras": sorted(cameras), "edges": edges_out}


def build_adjacency(
    zones_by_camera: dict[str, list[dict[str, Any]]],
    *,
    extra: list[list[str]],
    removed: list[list[str]],
) -> Adjacency:
    """Build an `Adjacency` from Frigate zone definitions plus config overrides.

    `zones_by_camera` is `zones.load_camera_zones`'s output: `{camera: [{name,
    ...}, ...]}`. `extra`/`removed` are `encounters.adjacency`/`not_adjacent`
    config, each a list of `[camera_a, camera_b]` pairs.
    """
    zone_names_by_camera: dict[str, set[str]] = {
        cam: {str(z["name"]) for z in zones} for cam, zones in zones_by_camera.items()
    }
    cameras = sorted(zone_names_by_camera)

    shared_zones: dict[frozenset[str], tuple[str, ...]] = {}
    for i, cam_a in enumerate(cameras):
        for cam_b in cameras[i + 1 :]:
            shared = zone_names_by_camera[cam_a] & zone_names_by_camera[cam_b]
            if shared:
                shared_zones[frozenset((cam_a, cam_b))] = tuple(sorted(shared))

    edges: set[frozenset[str]] = set(shared_zones.keys())
    for pair in extra:
        if len(pair) == 2 and pair[0] != pair[1]:
            edges.add(frozenset(pair))

    removed_edges = {frozenset(pair) for pair in removed if len(pair) == 2}
    edges -= removed_edges
    for edge in removed_edges:
        shared_zones.pop(edge, None)

    return Adjacency(edges=frozenset(edges), shared_zones=shared_zones)
