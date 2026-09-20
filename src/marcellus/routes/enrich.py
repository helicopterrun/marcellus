"""Identity clusters from face enrichment: review, name, merge, repair.

The page's data is `face_clusters` + `face_enrichments` (faces/enrich.py).
Naming a cluster is the promotion path — it becomes a KNOWN person, later
matches write the event's sub_label back to Frigate, and (by design) naming
also RETRO-writes the sub_label onto the cluster's past events still in
Frigate's retention. Naming, merging, and evicting a sighting all rebuild the
centroid exactly from the stored per-event embeddings rather than trusting the
running mean.

Thumbnails come from Frigate's event thumbnail endpoint, proxied server-side:
the sidecar's base_url origin is unauthenticated and server-to-server only, so
the browser can never be pointed at it directly. Full-size snapshots, by
contrast, go through the transparent proxy (routes/proxy.py) with the user's
own Frigate session — /api/events/{id}/snapshot.jpg straight from the page.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from marcellus.config import Settings
from marcellus.db import open_sidecar
from marcellus.errors import error_detail
from marcellus.faces import enrich
from marcellus.frigate_api import FrigateAPIError, FrigateClient, get_async_client
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enrich"])

# Sightings shown per cluster. Eight is enough to judge coherence at a glance
# and keeps the phone strip a single comfortable swipe.
_SIGHTINGS_PER_CLUSTER = 8

# Centroid cosine distance under which two clusters get a "looks like" merge
# hint. Deliberately looser than face_enrich.cluster_threshold (0.55): the
# hint is a human-confirmed suggestion, not an automatic join.
_SUGGEST_THRESHOLD = 0.6


def _clusters(conn: sqlite3.Connection, *, show_excluded: bool = False) -> list[dict[str, Any]]:
    """Clusters (named first, then by recency) with their best sightings.

    Excluded sightings (`excluded_at IS NOT NULL`) are hidden by default —
    they stay in the cluster's history but a person marked them "not this
    identity". `show_excluded=True` surfaces them (dimmed, "Include" action)
    for review/undo.
    """
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT cluster_id, name, observation_count, created_at, last_seen_at "
            "  FROM face_clusters ORDER BY (name IS NULL), last_seen_at DESC"
        )
    ]
    excluded_clause = "" if show_excluded else "AND excluded_at IS NULL "
    for r in rows:
        r["sightings"] = [
            dict(s)
            for s in conn.execute(
                "SELECT event_id, event_start_ts, distance, best_quality, excluded_at "
                "  FROM face_enrichments WHERE cluster_id = ? " + excluded_clause + ""
                " ORDER BY best_quality DESC LIMIT ?",
                (r["cluster_id"], _SIGHTINGS_PER_CLUSTER),
            )
        ]
        r["sample_event_id"] = next(
            (s["event_id"] for s in r["sightings"] if not s["excluded_at"]),
            r["sightings"][0]["event_id"] if r["sightings"] else None,
        )
    return rows


def _similar_pairs(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """cluster_id -> its closest other cluster under the suggest threshold.

    O(n²) over centroids is fine at this table's size (tens of clusters); the
    UI wants at most one hint per cluster, so keep only the best partner.
    """
    cents = [
        (
            int(r["cluster_id"]),
            r["name"],
            enrich.l2_normalize(enrich.unpack_embedding(r["centroid"])),
        )
        for r in conn.execute("SELECT cluster_id, name, centroid FROM face_clusters")
    ]
    best: dict[int, dict[str, Any]] = {}
    for i, (cid_a, _name_a, cent_a) in enumerate(cents):
        for cid_b, name_b, cent_b in cents[i + 1 :]:
            dist = enrich.cosine_distance(cent_a, cent_b)
            if dist > _SUGGEST_THRESHOLD:
                continue
            for cid, other_id, other_name in ((cid_a, cid_b, name_b), (cid_b, cid_a, _name_a)):
                cur = best.get(cid)
                if cur is None or dist < cur["distance"]:
                    best[cid] = {
                        "cluster_id": other_id,
                        "name": other_name,
                        "distance": round(dist, 3),
                    }
    return best


def _stats(conn: sqlite3.Connection, request: Request) -> dict[str, Any]:
    """Last-7-days pipeline counts + worker liveness, for the toolbar."""
    since = time.time() - 7 * 86400
    counts = {
        str(r["status"]): int(r["n"])
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM face_enrichments "
            " WHERE event_start_ts >= ? AND excluded_at IS NULL GROUP BY status",
            (since,),
        )
    }
    last_cycle = getattr(request.app.state, "face_enrich_last_cycle", None)
    return {
        "counts": counts,
        "processed": sum(counts.values()),
        "cycle_age_s": round(time.time() - last_cycle, 1) if last_cycle else None,
    }


@router.get("/enrich/clusters", response_class=HTMLResponse)
def clusters_view(request: Request, show_excluded: int = 0) -> Any:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        clusters = _clusters(conn, show_excluded=bool(show_excluded))
        similar = _similar_pairs(conn)
        stats = _stats(conn, request)
    finally:
        conn.close()
    return _templates(request).TemplateResponse(
        request,
        "enrich.html",
        {
            "clusters": clusters,
            "similar": similar,
            "stats": stats,
            "known_names": sorted({c["name"] for c in clusters if c["name"]}),
            "enabled": s.face_enrich.enabled,
            "cameras": s.face_enrich.cameras,
            "show_excluded": bool(show_excluded),
        },
    )


@router.get("/enrich/clusters.json")
def clusters_json(request: Request, show_excluded: int = 0) -> JSONResponse:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        return JSONResponse(
            {
                "clusters": _clusters(conn, show_excluded=bool(show_excluded)),
                "similar": {str(k): v for k, v in _similar_pairs(conn).items()},
                "stats": _stats(conn, request),
            }
        )
    finally:
        conn.close()


@router.get("/enrich/thumb/{event_id}")
async def cluster_thumb(event_id: str, request: Request) -> Response:
    """Proxy Frigate's event thumbnail for an enriched event."""
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM face_enrichments WHERE event_id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()
    # Only ids we enriched are proxied — the path param never becomes a free
    # fetch against the unauthenticated origin.
    if row is None:
        raise HTTPException(status_code=404, detail=error_detail("unknown_event", "unknown event"))
    client = get_async_client(request.app)
    url = f"{s.frigate.base_url}/api/events/{event_id}/thumbnail.jpg"
    try:
        r = await client.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", "frigate unreachable")
        ) from exc
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail=error_detail("not_found", "no thumbnail"))
    return Response(
        content=r.content,
        media_type="image/jpeg",
        # An event's thumbnail never changes once written.
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


def _retro_label(
    conn: sqlite3.Connection, settings: Settings, cluster_id: int, name: str
) -> int:
    """Write `name` as the sub_label onto the cluster's past events.

    Best-effort per event: an event that has aged out of Frigate's retention
    404s and is skipped — that must never fail the rename itself. Returns how
    many events were actually labeled.
    """
    event_ids = [
        str(r["event_id"])
        for r in conn.execute(
            "SELECT event_id FROM face_enrichments "
            "WHERE cluster_id = ? AND excluded_at IS NULL",
            (cluster_id,),
        )
    ]
    labeled = 0
    with FrigateClient(settings.frigate.base_url) as fc:
        for eid in event_ids:
            try:
                fc.set_sub_label(eid, name)
            except FrigateAPIError as exc:
                logger.info("enrich: retro-label skipped for %s: %s", eid, exc)
                continue
            conn.execute(
                "UPDATE face_enrichments SET sub_label_written = ? WHERE event_id = ?",
                (name, eid),
            )
            labeled += 1
    return labeled


class NamePayload(BaseModel):
    name: str


@router.post("/enrich/clusters/{cluster_id}/name")
def cluster_name(cluster_id: int, payload: NamePayload, request: Request) -> JSONResponse:
    name = payload.name.strip()
    if not name or len(name) > 100:
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_name", "name must be 1-100 chars")
        )
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        cur = conn.execute(
            "UPDATE face_clusters SET name = ? WHERE cluster_id = ?", (name, cluster_id)
        )
        if not cur.rowcount:
            raise HTTPException(
                status_code=404,
                detail=error_detail("cluster_not_found", "cluster not found"),
            )
        enrich.rebuild_centroid(conn, cluster_id)
        relabeled = _retro_label(conn, s, cluster_id, name)
        conn.commit()
        return JSONResponse({"ok": True, "relabeled": relabeled})
    finally:
        conn.close()


class MergePayload(BaseModel):
    into: int  # surviving cluster_id


@router.post("/enrich/clusters/{cluster_id}/merge")
def cluster_merge(cluster_id: int, payload: MergePayload, request: Request) -> JSONResponse:
    if payload.into == cluster_id:
        raise HTTPException(
                status_code=400,
                detail=error_detail(
                    "invalid_merge", "cannot merge a cluster into itself"
                ),
            )
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        for cid in (cluster_id, payload.into):
            if not conn.execute(
                "SELECT 1 FROM face_clusters WHERE cluster_id = ?", (cid,)
            ).fetchone():
                raise HTTPException(
                    status_code=404,
                    detail=error_detail("cluster_not_found", f"cluster {cid} not found"),
                )
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = ? WHERE cluster_id = ?",
            (payload.into, cluster_id),
        )
        conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cluster_id,))
        enrich.rebuild_centroid(conn, payload.into)
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@router.post("/enrich/clusters/{cluster_id}/delete")
def cluster_delete(cluster_id: int, request: Request) -> JSONResponse:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = NULL, embedding = NULL "
            "WHERE cluster_id = ?",
            (cluster_id,),
        )
        cur = conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cluster_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(
                status_code=404,
                detail=error_detail("cluster_not_found", "cluster not found"),
            )
        return JSONResponse({"ok": True})
    finally:
        conn.close()


class ExcludePayload(BaseModel):
    excluded: bool


@router.patch("/enrich/events/{event_id}")
def sighting_exclude(
    event_id: str, payload: ExcludePayload, request: Request
) -> JSONResponse:
    """Soft-exclude/include one sighting: keeps its cluster assignment but
    marks it out of centroid/sub_label/stats aggregation, with an Undo path.

    Unlike `/remove`, this never detaches the row or deletes an emptied
    cluster -- excluding every sighting in a cluster just leaves it looking
    momentarily empty until one is re-included.
    """
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT cluster_id FROM face_enrichments WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="sighting not found")
        excluded_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
            if payload.excluded
            else None
        )
        conn.execute(
            "UPDATE face_enrichments SET excluded_at = ? WHERE event_id = ?",
            (excluded_at, event_id),
        )
        cluster_id = row["cluster_id"]
        if cluster_id is not None:
            enrich.rebuild_centroid(conn, int(cluster_id))
        conn.commit()
        return JSONResponse(
            {
                "event_id": event_id,
                "excluded": payload.excluded,
                "cluster_id": cluster_id,
            }
        )
    finally:
        conn.close()


@router.post("/enrich/events/{event_id}/remove")
def sighting_remove(event_id: str, request: Request) -> JSONResponse:
    """Evict one sighting from its cluster — the repair tool for a bad join.

    The enrichment row keeps its status (it mirrors Frigate's event history)
    but loses its cluster assignment and embedding, so the centroid rebuild
    forgets it. A cluster that just lost its last sighting is deleted rather
    than left as an empty shell with a stale centroid.
    """
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT cluster_id FROM face_enrichments WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None or row["cluster_id"] is None:
            raise HTTPException(
                status_code=404,
                detail=error_detail("not_found", "sighting not in a cluster"),
            )
        cluster_id = int(row["cluster_id"])
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = NULL, embedding = NULL "
            "WHERE event_id = ?",
            (event_id,),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM face_enrichments WHERE cluster_id = ?", (cluster_id,)
        ).fetchone()
        cluster_deleted = False
        if int(remaining["n"]) == 0:
            conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cluster_id,))
            cluster_deleted = True
        else:
            enrich.rebuild_centroid(conn, cluster_id)
        conn.commit()
        return JSONResponse({"ok": True, "cluster_deleted": cluster_deleted})
    finally:
        conn.close()
