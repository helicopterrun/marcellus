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
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from marcellus.config import Settings
from marcellus.db import open_sidecar, with_sidecar
from marcellus.errors import error_detail
from marcellus.faces import consolidate, enrich
from marcellus.faces.handles import handle_for
from marcellus.frigate_api import get_async_client
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes._deps import templates_of as _templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["enrich"])

# Sightings shown per cluster. Eight is enough to judge coherence at a glance
# and keeps the phone strip a single comfortable swipe. A single-cluster view
# (?cluster=N) shows every sighting: it is where wrong joins get cleaned out,
# and those are usually the low-quality ones a best-first cap would hide (on
# 2026-10-31 the largest cluster had 237).
_SIGHTINGS_PER_CLUSTER = 8

# Single-cluster sort orders. "odd" puts the sightings furthest from the
# cluster's current centroid first -- the likeliest wrong joins.
_SORTS = ("best", "odd")

# Clusters per page. On 2026-10-31 there were 465 clusters, 332 of them seen
# once; rendering them all made a page of ~3,700 tiles.
_PAGE_SIZE = 40

# Page views. "people" is everyone named plus every unknown seen at least
# twice -- the clusters worth naming. "once" is the long tail of single
# sightings, mostly passers-by. "all" is the JSON default (everything).
_VIEWS = ("people", "once", "all")

_SUGGEST_THRESHOLD = enrich.SUGGEST_THRESHOLD


def _view_where(view: str) -> str:
    if view == "people":
        return "WHERE name IS NOT NULL OR observation_count >= 2"
    if view == "once":
        return "WHERE name IS NULL AND observation_count < 2"
    return ""


def _view_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Cluster counts for the page's tabs."""
    row = conn.execute(
        "SELECT SUM(name IS NOT NULL) AS named, "
        "       SUM(name IS NULL AND observation_count >= 2) AS unknown, "
        "       SUM(name IS NULL AND observation_count < 2) AS once "
        "  FROM face_clusters"
    ).fetchone()
    named, unknown, once = (int(row[k] or 0) for k in ("named", "unknown", "once"))
    return {"named": named, "unknown": unknown, "people": named + unknown, "once": once}


def _clusters(
    conn: sqlite3.Connection,
    *,
    show_excluded: bool = False,
    view: str = "all",
    cluster_id: int | None = None,
    limit: int | None = None,
    sort: str = "best",
) -> list[dict[str, Any]]:
    """Clusters with their best sightings: named first (most recently seen
    first), then unknowns by sighting count, so the people seen most -- the
    ones worth naming -- lead.

    Excluded sightings (`excluded_at IS NOT NULL`) are hidden by default —
    they stay in the cluster's history but a person marked them "not this
    identity". `show_excluded=True` surfaces them (dimmed, "Include" action)
    for review/undo. `cluster_id` selects one cluster (any view) with all of
    its sightings, each with `fit` (cosine distance to the current centroid);
    `sort="odd"` orders them worst fit first.
    """
    if cluster_id is not None:
        where, args = "WHERE cluster_id = ?", [cluster_id]
    else:
        where, args = _view_where(view), []
    sql = (
        "SELECT cluster_id, name, observation_count, created_at, last_seen_at, centroid "
        f"  FROM face_clusters {where} "
        " ORDER BY (name IS NULL), "
        "          CASE WHEN name IS NULL THEN observation_count END DESC, "
        "          last_seen_at DESC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    single = cluster_id is not None
    excluded_clause = "" if show_excluded else "AND excluded_at IS NULL "
    for r in rows:
        centroid = r.pop("centroid")
        r["sightings"] = [
            dict(s)
            for s in conn.execute(
                "SELECT event_id, event_start_ts, distance, best_quality, excluded_at"
                + (", embedding" if single else "")
                + "  FROM face_enrichments WHERE cluster_id = ? " + excluded_clause + ""
                " ORDER BY best_quality DESC LIMIT ?",
                (r["cluster_id"], -1 if single else _SIGHTINGS_PER_CLUSTER),
            )
        ]
        if single:
            _attach_fit(r["sightings"], centroid)
            if sort == "odd":
                r["sightings"].sort(
                    key=lambda s: -1.0 if s["fit"] is None else s["fit"], reverse=True
                )
        for sight in r["sightings"]:
            # Rendered crops only, best first; empty for events enriched
            # before stacks existed (the template falls back to the thumbnail).
            sight["faces"] = [
                dict(f)
                for f in conn.execute(
                    "SELECT rank, quality, used FROM face_crops "
                    "WHERE event_id = ? AND crop_path IS NOT NULL ORDER BY rank",
                    (sight["event_id"],),
                )
            ]
        r["sample_event_id"] = next(
            (s["event_id"] for s in r["sightings"] if not s["excluded_at"]),
            r["sightings"][0]["event_id"] if r["sightings"] else None,
        )
        r["avatar"] = _avatar_url(r["sightings"])
    return rows


def _attach_fit(sightings: list[dict[str, Any]], centroid: bytes) -> None:
    """Set each sighting's `fit`: cosine distance from its embedding to the
    cluster's current centroid (None when the embedding is gone)."""
    cent = enrich.l2_normalize(enrich.unpack_embedding(centroid))
    for s in sightings:
        blob = s.pop("embedding")
        if not blob:
            s["fit"] = None
            continue
        emb = enrich.l2_normalize(enrich.unpack_embedding(blob))
        s["fit"] = round(enrich.cosine_distance(emb, cent), 3)


def _avatar_url(sightings: list[dict[str, Any]]) -> str | None:
    """Best face crop among the sightings, else the first one's Frigate thumbnail."""
    live = [s for s in sightings if not s.get("excluded_at")] or sightings
    for s in live:
        if s.get("faces"):
            return f"/enrich/face/{s['event_id']}/{s['faces'][0]['rank']}.jpg"
    return f"/enrich/thumb/{live[0]['event_id']}" if live else None


def _cluster_index(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every cluster with an avatar, for the merge picker (no sightings)."""
    out = []
    for r in conn.execute(
        "SELECT c.cluster_id, c.name, c.observation_count, c.last_seen_at, "
        "       (SELECT e.event_id FROM face_enrichments e "
        "         WHERE e.cluster_id = c.cluster_id AND e.excluded_at IS NULL "
        "         ORDER BY e.best_quality DESC LIMIT 1) AS sample_event_id, "
        "       (SELECT e.event_id FROM face_enrichments e JOIN face_crops f "
        "            ON f.event_id = e.event_id AND f.rank = 0 AND f.crop_path IS NOT NULL "
        "         WHERE e.cluster_id = c.cluster_id AND e.excluded_at IS NULL "
        "         ORDER BY e.best_quality DESC LIMIT 1) AS face_event_id "
        "  FROM face_clusters c "
        " ORDER BY (c.name IS NULL), c.observation_count DESC, c.last_seen_at DESC"
    ):
        d = dict(r)
        d["handle"] = handle_for(int(d["cluster_id"]))
        face_eid = d.pop("face_event_id")
        if face_eid:
            d["avatar"] = f"/enrich/face/{face_eid}/0.jpg"
        elif d["sample_event_id"]:
            d["avatar"] = f"/enrich/thumb/{d['sample_event_id']}"
        else:
            d["avatar"] = None
        out.append(d)
    return out


def _similar_pairs(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """cluster_id -> its closest other cluster under the suggest threshold.

    All pairs, so O(n²): one matrix product with numpy (installed with the
    `enrich` extra, which any instance producing clusters has). The pure-Python
    fallback took 7.5s on the page at 482 clusters (2026-10-31); numpy takes
    milliseconds. The UI wants at most one hint per cluster, so keep only the
    best partner.
    """
    rows = [
        (int(r["cluster_id"]), r["name"], r["centroid"])
        for r in conn.execute("SELECT cluster_id, name, centroid FROM face_clusters")
    ]
    try:
        import numpy as np
    except ImportError:
        return _similar_pairs_py(rows)
    if len(rows) < 2:
        return {}
    m = np.stack([np.frombuffer(blob, dtype="<f4") for _, _, blob in rows]).astype(np.float64)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m /= norms
    dist = 1.0 - m @ m.T
    np.fill_diagonal(dist, np.inf)
    nearest = dist.argmin(axis=1)
    best: dict[int, dict[str, Any]] = {}
    for i, j in enumerate(nearest):
        d = float(dist[i, j])
        if d <= _SUGGEST_THRESHOLD:
            best[rows[i][0]] = {
                "cluster_id": rows[j][0],
                "name": rows[j][1],
                "handle": handle_for(rows[j][0]),
                "distance": round(d, 3),
            }
    return best


def _similar_pairs_py(rows: list[tuple[int, Any, bytes]]) -> dict[int, dict[str, Any]]:
    """`_similar_pairs` without numpy."""
    cents = [
        (cid, name, enrich.l2_normalize(enrich.unpack_embedding(blob)))
        for cid, name, blob in rows
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
                        "handle": handle_for(other_id),
                        "distance": round(dist, 3),
                    }
    return best


def _attach_partner_avatars(conn: sqlite3.Connection, similar: dict[int, dict[str, Any]]) -> None:
    """Give each "looks like" partner an `avatar` URL: ONE query for every
    distinct partner (a partner is often not on the current page), preferring
    its best live sighting that has a rendered crop, else that sighting's
    Frigate thumbnail -- the same choice `_cluster_index` makes."""
    partners = sorted({int(v["cluster_id"]) for v in similar.values()})
    best: dict[int, str] = {}
    has_crop: set[int] = set()
    for i in range(0, len(partners), 500):
        chunk = partners[i : i + 500]
        for r in conn.execute(
            "SELECT e.cluster_id, e.event_id, f.rank AS rank "
            "  FROM face_enrichments e LEFT JOIN face_crops f "
            "    ON f.event_id = e.event_id AND f.rank = 0 AND f.crop_path IS NOT NULL "
            " WHERE e.excluded_at IS NULL AND e.cluster_id IN ("
            + ",".join("?" * len(chunk))
            + ") ORDER BY e.best_quality DESC",
            chunk,
        ):
            cid = int(r["cluster_id"])
            url = (
                f"/enrich/face/{r['event_id']}/0.jpg"
                if r["rank"] is not None
                else f"/enrich/thumb/{r['event_id']}"
            )
            # First row per cluster is its best sighting; a later one only
            # wins by having a crop where the best doesn't.
            if cid not in best or (cid not in has_crop and r["rank"] is not None):
                best[cid] = url
                if r["rank"] is not None:
                    has_crop.add(cid)
    for v in similar.values():
        v["avatar"] = best.get(int(v["cluster_id"]))


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
def clusters_view(
    request: Request,
    show_excluded: int = 0,
    view: str = "people",
    cluster: int | None = None,
    page: int = Query(1, ge=1),
    sort: str = "best",
) -> Any:
    s = _settings(request)
    if view not in _VIEWS:
        view = "people"
    if sort not in _SORTS:
        sort = "best"
    conn = open_sidecar(s.sidecar.db_path)
    try:
        clusters = _clusters(
            conn,
            show_excluded=bool(show_excluded),
            view=view,
            cluster_id=cluster,
            limit=None if cluster is not None else _PAGE_SIZE * page,
            sort=sort,
        )
        similar = _similar_pairs(conn)
        _attach_partner_avatars(conn, similar)
        stats = _stats(conn, request)
        backfill = enrich.backfill_status(conn)
        counts = _view_counts(conn)
        known_names = sorted(
            str(r["name"])
            for r in conn.execute("SELECT name FROM face_clusters WHERE name IS NOT NULL")
        )
    finally:
        conn.close()
    total = counts.get(view, counts["people"] + counts["once"])
    return _templates(request).TemplateResponse(
        request,
        "enrich.html",
        {
            "clusters": clusters,
            "similar": similar,
            "stats": stats,
            "backfill": backfill,
            "counts": counts,
            "view": view,
            "single": cluster,
            "sort": sort,
            "page": page,
            "has_more": cluster is None and total > len(clusters),
            "known_names": known_names,
            "enabled": s.face_enrich.enabled,
            "cameras": s.face_enrich.cameras,
            "join_threshold": s.face_enrich.cluster_threshold,
            "show_excluded": bool(show_excluded),
        },
    )


@router.get("/enrich/clusters/index.json")
def clusters_index(request: Request) -> JSONResponse:
    """Every cluster, avatar + counts only: the merge picker searches this."""
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        return JSONResponse({"clusters": _cluster_index(conn)})
    finally:
        conn.close()


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


@router.get("/enrich/clusters/{cluster_id}/faces.json")
def cluster_faces(
    cluster_id: int, request: Request, limit: int = Query(6, ge=1, le=24)
) -> JSONResponse:
    """Up to `limit` best face crops of one identity's LIVE sightings (best
    quality first), for the merge confirmation: enough evidence to judge by."""
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        if not conn.execute(
            "SELECT 1 FROM face_clusters WHERE cluster_id = ?", (cluster_id,)
        ).fetchone():
            raise HTTPException(
                status_code=404,
                detail=error_detail("cluster_not_found", "cluster not found"),
            )
        faces = [
            {
                "event_id": r["event_id"],
                "rank": int(r["rank"]),
                "url": f"/enrich/face/{r['event_id']}/{int(r['rank'])}.jpg",
                "quality": r["best_quality"],
            }
            for r in conn.execute(
                "SELECT e.event_id, e.best_quality, "
                "       (SELECT MIN(f.rank) FROM face_crops f "
                "         WHERE f.event_id = e.event_id AND f.crop_path IS NOT NULL) AS rank "
                "  FROM face_enrichments e "
                " WHERE e.cluster_id = ? AND e.excluded_at IS NULL "
                "   AND EXISTS (SELECT 1 FROM face_crops f WHERE f.event_id = e.event_id "
                "                AND f.crop_path IS NOT NULL) "
                " ORDER BY e.best_quality DESC, e.event_start_ts DESC LIMIT ?",
                (cluster_id, limit),
            )
        ]
        return JSONResponse({"cluster_id": cluster_id, "faces": faces})
    finally:
        conn.close()


@router.get("/enrich/merge-proposals.json")
async def merge_proposals(request: Request) -> JSONResponse:
    """Read-only: groups of unnamed identities that look like one person
    (faces/consolidate.py). Computed on a worker thread; nothing is written."""
    s = _settings(request)
    threshold = s.face_enrich.consolidate_threshold

    def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [p.wire() for p in consolidate.propose_merges(conn, threshold=threshold)]

    proposals = await with_sidecar(s.sidecar.db_path, _do)
    return JSONResponse({"threshold": threshold, "proposals": proposals})


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


def _crop_response(s: Settings, sql: str, args: tuple[Any, ...]) -> FileResponse:
    """FileResponse for the crop path `sql` selects (as `rel`), or 404."""
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(sql, args).fetchone()
    finally:
        conn.close()
    missing = HTTPException(status_code=404, detail=error_detail("not_found", "no face crop"))
    if row is None or not row["rel"]:
        raise missing
    root = Path(s.face_enrich.crops_dir).resolve()
    try:
        path = (root / str(row["rel"])).resolve()
    except OSError as exc:
        raise missing from exc
    # A hand-edited row must not become an arbitrary file read.
    if root not in path.parents or not path.is_file():
        raise missing
    # Not immutable: an event retried after an error re-renders its crops.
    return FileResponse(
        path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )


@router.get("/enrich/face/{event_id}.jpg")
def event_face(event_id: str, request: Request) -> FileResponse:
    """The event's best-face crop (faces/enrich.py store_crops, rank 0)."""
    return _crop_response(
        _settings(request),
        "SELECT best_crop_path AS rel FROM face_enrichments WHERE event_id = ?",
        (event_id,),
    )


@router.get("/enrich/face/{event_id}/{rank}.jpg")
def event_face_rank(event_id: str, rank: int, request: Request) -> FileResponse:
    """One face of the event's stack, rank 0 = best."""
    return _crop_response(
        _settings(request),
        "SELECT crop_path AS rel FROM face_crops WHERE event_id = ? AND rank = ?",
        (event_id, rank),
    )


class BackfillPayload(BaseModel):
    run: bool


@router.get("/enrich/backfill.json")
def backfill_get(request: Request) -> JSONResponse:
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        return JSONResponse(enrich.backfill_status(conn))
    finally:
        conn.close()


@router.post("/enrich/backfill")
def backfill_set(payload: BackfillPayload, request: Request) -> JSONResponse:
    """Queue (run=true) or un-queue (run=false) the face-stack backfill.

    Only queues; the enrichment worker does the work in its idle cycles.
    """
    conn = open_sidecar(_settings(request).sidecar.db_path)
    try:
        if payload.run:
            enrich.queue_backfill(conn)
        else:
            enrich.pause_backfill(conn)
        conn.commit()
        return JSONResponse(enrich.backfill_status(conn))
    finally:
        conn.close()


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
        if not enrich.rename_cluster(conn, cluster_id, name):
            raise HTTPException(
                status_code=404,
                detail=error_detail("cluster_not_found", "cluster not found"),
            )
        conn.commit()
        relabeled = enrich.retro_label(conn, s, cluster_id, name)
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
        result = enrich.merge_clusters(conn, cluster_id, payload.into)
        conn.commit()
        if result.carried_name:
            enrich.retro_label(conn, s, payload.into, result.carried_name)
            conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@router.post("/enrich/clusters/{cluster_id}/delete")
def cluster_delete(cluster_id: int, request: Request) -> JSONResponse:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        # Deleting an identity forgets its faces too, not just its embeddings.
        deleted = enrich.delete_cluster(conn, s.face_enrich.crops_dir, cluster_id)
        conn.commit()
        if not deleted:
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
        found, cluster_id = enrich.set_sighting_excluded(conn, event_id, payload.excluded)
        if not found:
            raise HTTPException(status_code=404, detail="sighting not found")
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
