"""Merge proposals: unnamed clusters whose centroids have drifted together.

`match_or_cluster` assigns an embedding once and never revisits, so two
unnamed clusters whose running centroids later converge are never merged (real
case: #647 and #648 at distance 0.399, with cluster_threshold 0.55). This
module finds such groups. It is STRICTLY READ-ONLY: it never writes, merges,
or schedules anything; a person confirms each group through the existing merge
route. Named clusters are never candidates, however close.

Grouping is complete linkage, not chaining: two groups combine only if EVERY
cross pair is within the threshold (chaining produced groups of 12+ in real
data).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timezone, tzinfo
from itertools import combinations
from typing import Any

from marcellus.faces import enrich, people
from marcellus.faces.handles import handle_for


@dataclass(frozen=True)
class ProposalMember:
    id: int
    handle: str
    sightings: int
    days: int
    last_seen: float | None
    avatar_path: str | None


@dataclass(frozen=True)
class MergeProposal:
    members: tuple[ProposalMember, ...]
    max_distance: float
    #: Two members have sightings on the same camera within `visit_gap_s`.
    same_visit: bool
    #: The member with the most sightings (ties: the oldest id).
    survivor_id: int

    def wire(self) -> dict[str, Any]:
        return {
            "survivor": self.survivor_id,
            "max_distance": self.max_distance,
            "same_visit": self.same_visit,
            "members": [m.__dict__ for m in self.members],
        }


def _pair_distances(
    ids: list[int], blobs: list[bytes], threshold: float
) -> dict[tuple[int, int], float]:
    """Centroid cosine distance for every pair at or under `threshold`, keyed
    by (smaller id, larger id). numpy when available, else pure Python."""
    close: dict[tuple[int, int], float] = {}
    if len(ids) < 2:
        return close
    try:
        import numpy as np
    except ImportError:
        cents = [enrich.l2_normalize(enrich.unpack_embedding(b)) for b in blobs]
        for i, j in combinations(range(len(ids)), 2):
            if len(cents[i]) != len(cents[j]):
                continue
            d = enrich.cosine_distance(cents[i], cents[j])
            if d <= threshold:
                close[(min(ids[i], ids[j]), max(ids[i], ids[j]))] = d
        return close
    dims = {len(b) for b in blobs}
    if len(dims) != 1:
        return close
    m = np.stack([np.frombuffer(b, dtype="<f4") for b in blobs]).astype(np.float64)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m /= norms
    dist = 1.0 - m @ m.T
    for i, j in zip(*np.where(np.triu(dist <= threshold, k=1)), strict=True):
        a, b = ids[int(i)], ids[int(j)]
        close[(min(a, b), max(a, b))] = float(dist[i, j])
    return close


def _same_visit(group: list[int], seen: dict[int, list[tuple[str, float]]], gap_s: float) -> bool:
    """Any two members with sightings on the same camera within `gap_s`."""
    by_camera: dict[str, list[tuple[float, int]]] = {}
    for cid in group:
        for camera, ts in seen.get(cid, []):
            by_camera.setdefault(camera, []).append((ts, cid))
    for items in by_camera.values():
        items.sort()
        # If some cross-member pair is within the gap, so is an adjacent one.
        for (t1, c1), (t2, c2) in zip(items, items[1:], strict=False):
            if c1 != c2 and t2 - t1 <= gap_s:
                return True
    return False


def propose_merges(
    conn: sqlite3.Connection,
    *,
    threshold: float,
    visit_gap_s: float = 900.0,
    tz: tzinfo = timezone.utc,
) -> list[MergeProposal]:
    """Groups of unnamed clusters that look like one person, `same_visit` first
    then smallest `max_distance`. Reads only; safe on a read-only connection."""
    everyone = people.load_people(conn, tz)
    candidates = {cid: p for cid, p in everyone.items() if p.name is None}
    if len(candidates) < 2:
        return []
    ids = sorted(candidates)
    blobs = {
        int(r["cluster_id"]): r["centroid"]
        for r in conn.execute("SELECT cluster_id, centroid FROM face_clusters")
        if int(r["cluster_id"]) in candidates
    }
    ids = [i for i in ids if i in blobs]
    close = _pair_distances(ids, [blobs[i] for i in ids], threshold)
    if not close:
        return []

    group_of: dict[int, list[int]] = {i: [i] for i in ids}
    for (a, b), _d in sorted(close.items(), key=lambda kv: (kv[1], kv[0])):
        ga, gb = group_of[a], group_of[b]
        if ga is gb:
            continue
        if all((min(x, y), max(x, y)) in close for x in ga for y in gb):
            merged = ga + gb
            for cid in merged:
                group_of[cid] = merged

    groups = {id(g): g for g in group_of.values() if len(g) > 1}
    involved = {cid for g in groups.values() for cid in g}
    seen: dict[int, list[tuple[str, float]]] = {}
    for r in conn.execute(
        "SELECT cluster_id, camera, event_start_ts FROM face_enrichments "
        "WHERE cluster_id IS NOT NULL AND excluded_at IS NULL"
    ):
        if int(r["cluster_id"]) in involved:
            seen.setdefault(int(r["cluster_id"]), []).append(
                (str(r["camera"]), float(r["event_start_ts"]))
            )

    out: list[MergeProposal] = []
    for g in groups.values():
        members = sorted(g)
        persons = [candidates[c] for c in members]
        survivor = max(persons, key=lambda p: (p.sightings, -p.id))
        out.append(
            MergeProposal(
                members=tuple(
                    ProposalMember(
                        id=p.id,
                        handle=handle_for(p.id),
                        sightings=p.sightings,
                        days=p.days,
                        last_seen=p.last_seen,
                        avatar_path=p.wire()["avatar_path"],
                    )
                    for p in persons
                ),
                max_distance=round(
                    max(close[(a, b)] for a, b in combinations(members, 2)), 3
                ),
                same_visit=_same_visit(members, seen, visit_gap_s),
                survivor_id=survivor.id,
            )
        )
    out.sort(key=lambda m: (not m.same_visit, m.max_distance, m.survivor_id))
    return out
