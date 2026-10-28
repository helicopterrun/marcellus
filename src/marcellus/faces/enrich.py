"""Face enrichment (B3): embeddings, temporal aggregation, identity clusters.

For each ended `person` event on an enrolled camera (config `face_enrich`),
sample full-resolution frames out of Frigate's recordings — the same
`/api/{camera}/recordings/{ts:.3f}/snapshot.jpg` endpoint crosscam.py measured
and documented — detect faces with SCRFD, quality-score them (sharpness x size
x frontality from the 5-point landmarks), embed the best N with ArcFace,
aggregate into ONE embedding per event, and match it against identity clusters
in the sidecar DB. A NAMED cluster match writes the event's sub_label back to
Frigate; anything else folds into (or starts) an unnamed cluster, so recurring
strangers accumulate an identity that a human can later promote by naming.
The single best face is also saved as a padded crop under `crops_dir` — the
display copy, so no surface has to re-detect it.

Work discovery is crosscam's lookback pattern, not an MQTT hook: recordings
commit at segment END (measured lag 5.4-9.4s), so enrichment is inherently
deferred by `process_delay_s`, and a lookback query LEFT JOINed against
`face_enrichments` is self-healing across restarts with no cursor or queue.

Dependency split, load-bearing for CI: everything that decides — candidate
query, quality combine, aggregation, matching, clustering, reaping — is pure
Python (embeddings are list[float], packed with `array`), tested without the
`[enrich]` extra. Only `_Engine` touches insightface/onnxruntime/cv2, behind
a lazy import guard. Inference is CPU-only by design: the
OpenVINO/iGPU path produced a multi-day embeddings OOM cycle on this host.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import time
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from marcellus import db
from marcellus.config import Settings
from marcellus.frigate_api import FrigateClient

logger = logging.getLogger(__name__)

EMBED_DIM = 512


class EnrichUnavailable(RuntimeError):
    """insightface / onnxruntime / cv2 not installed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Embedding math — pure Python on list[float], no numpy.


def pack_embedding(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack_embedding(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 0.0:
        return list(vec)
    return [x / norm for x in vec]


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity. Inputs are assumed L2-normalized."""
    return 1.0 - sum(x * y for x, y in zip(a, b, strict=False))


def aggregate(embeddings: list[list[float]], weights: list[float]) -> list[float]:
    """Quality-weighted mean of L2-normalized embeddings, re-normalized.

    Averaging the best frames' embeddings is where most of the accuracy over
    single-frame recognition comes from — pose and lighting noise is roughly
    zero-mean across frames while identity is not.
    """
    if not embeddings:
        raise ValueError("aggregate() needs at least one embedding")
    total_w = sum(weights)
    if total_w <= 0.0:
        weights = [1.0] * len(embeddings)
        total_w = float(len(embeddings))
    dim = len(embeddings[0])
    acc = [0.0] * dim
    for emb, w in zip(embeddings, weights, strict=True):
        for i in range(dim):
            acc[i] += emb[i] * w
    return l2_normalize([x / total_w for x in acc])


# ---------------------------------------------------------------------------
# Quality scoring.


def pose_frontality(kps: list[tuple[float, float]]) -> float:
    """0..1 frontality from SCRFD's 5 landmarks (le, re, nose, lm, rm).

    Two cheap, orientation-free signals, multiplied:
    - nose symmetry: how centered the nose x sits between the eyes. A profile
      pushes the nose past an eye (ratio -> 0 or 1).
    - eye level: eye-to-eye vertical offset vs eye distance; a heavy roll or a
      half-turned head tilts the eye line.
    """
    if len(kps) < 5:
        return 0.0
    (lex, ley), (rex, rey), (nx, _ny) = kps[0], kps[1], kps[2]
    eye_dx = rex - lex
    eye_span = math.hypot(eye_dx, rey - ley)
    if eye_span <= 1.0:
        return 0.0
    # Nose position along the eye axis, 0.5 = perfectly centered.
    ratio = (nx - lex) / eye_dx if abs(eye_dx) > 1e-6 else 0.0
    symmetry = max(0.0, 1.0 - abs(ratio - 0.5) * 2.0)
    level = max(0.0, 1.0 - abs(rey - ley) / eye_span * 2.0)
    return symmetry * level


def quality_score(
    *, sharpness: float, area_px: float, frontality: float, min_face_area_px: int
) -> float:
    """0..1 geometric mean of the three quality signals.

    Sharpness saturates at 500 Laplacian variance — beyond that more contrast
    is not more identity. Area saturates at 4x the floor: a face at the floor
    is usable, 2x the floor is comfortable, beyond 4x adds nothing.
    """
    s = min(1.0, max(0.0, sharpness) / 500.0)
    a = min(1.0, max(0.0, area_px) / (4.0 * max(1, min_face_area_px)))
    f = max(0.0, min(1.0, frontality))
    return float((s * a * f) ** (1.0 / 3.0))


# ---------------------------------------------------------------------------
# Best-face crop: pure Pillow (a core dep), so it is testable without [enrich].

_CROP_JPEG_QUALITY = 90
_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")


def crop_relative_path(event_id: str, frame_ts: float) -> str:
    """Date-sharded path under `crops_dir`, as crosscam lays out its captures.

    Event ids come from Frigate's DB, not a request, but a filesystem path
    gets the same whitelist either way.
    """
    day = datetime.fromtimestamp(frame_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    stem = re.sub(r"\.{2,}", ".", _SAFE_STEM.sub("_", event_id)).lstrip(".") or "face"
    return f"{day}/{stem}.jpg"


def crop_box(
    bbox: tuple[float, float, float, float], src_w: int, src_h: int, *, pad: float
) -> tuple[int, int, int, int] | None:
    """Square pixel box around a normalized face bbox, padded and kept in frame.

    The square is slid back inside the frame rather than clipped, so a face
    near an edge keeps its framing; only a square larger than the frame
    itself gets clipped. None for a degenerate box.
    """
    x1, y1, x2, y2 = bbox[0] * src_w, bbox[1] * src_h, bbox[2] * src_w, bbox[3] * src_h
    face = max(x2 - x1, y2 - y1)
    if face <= 1.0:
        return None
    side = face * (1.0 + 2.0 * max(0.0, pad))
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    left = min(max(0.0, cx - side / 2.0), max(0.0, src_w - side))
    top = min(max(0.0, cy - side / 2.0), max(0.0, src_h - side))
    right = min(float(src_w), left + side)
    bottom = min(float(src_h), top + side)
    return (int(left), int(top), int(round(right)), int(round(bottom)))


def render_face_crop(
    jpeg: bytes, bbox: tuple[float, float, float, float], *, pad: float, max_edge: int
) -> bytes | None:
    """JPEG bytes of the padded face crop, or None if the frame won't decode."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as im:
            box = crop_box(bbox, im.size[0], im.size[1], pad=pad)
            if box is None:
                return None
            face = im.convert("RGB").crop(box)
        if max(face.size) > max_edge:
            face.thumbnail((max_edge, max_edge))
        buf = io.BytesIO()
        face.save(buf, format="JPEG", quality=_CROP_JPEG_QUALITY)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a bad frame costs its crop, not the event
        logger.warning("face_enrich: crop render failed", exc_info=True)
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    """Publish a file atomically (mkstemp + os.replace), as crosscam does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".publish-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def store_crop(
    event_id: str, face: DetectedFace, jpeg: bytes, *, cfg: Any
) -> str | None:
    """Render and save the event's best-face crop; its relative path or None.

    Never raises: the crop is for display, and losing it must not turn an
    enriched event into an error that gets retried.
    """
    data = render_face_crop(jpeg, face.bbox, pad=cfg.crop_pad, max_edge=cfg.crop_max_edge)
    if data is None:
        return None
    rel = crop_relative_path(event_id, face.frame_ts)
    try:
        _write_atomic(Path(cfg.crops_dir) / rel, data)
    except OSError:
        logger.warning("face_enrich: could not write crop for %s", event_id, exc_info=True)
        return None
    return rel


def drop_crops(conn: sqlite3.Connection, crops_dir: Path, event_ids: list[str]) -> int:
    """Delete these events' crop files and clear their paths; returns files removed.

    Rows first, then files: a crash between the two leaves an orphan file,
    never a row pointing at nothing.
    """
    removed = 0
    for eid in event_ids:
        row = conn.execute(
            "SELECT best_crop_path FROM face_enrichments WHERE event_id = ?", (eid,)
        ).fetchone()
        if row is None or not row["best_crop_path"]:
            continue
        conn.execute(
            "UPDATE face_enrichments SET best_crop_path = NULL WHERE event_id = ?", (eid,)
        )
        with contextlib.suppress(OSError):
            (Path(crops_dir) / str(row["best_crop_path"])).unlink()
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Candidates: crosscam's lookback pattern over a joined connection.


@dataclass(frozen=True)
class Candidate:
    event_id: str
    camera: str
    start_time: float
    end_time: float
    attempts: int


def find_candidates(conn: sqlite3.Connection, *, cfg: Any, now: float) -> list[Candidate]:
    """Ended person events that are due and not already settled.

    Due means the event ENDED at least `process_delay_s` ago, so every frame
    of it lives in a committed recording segment. An absent face_enrichments
    row is "pending"; `status='error'` retries up to max_attempts.
    """
    if not cfg.cameras:
        return []
    lower = now - cfg.lookback_s
    upper = now - cfg.process_delay_s
    if upper <= lower:
        return []
    cam_q = ",".join("?" * len(cfg.cameras))
    sql = f"""
        SELECT e.id, e.camera, e.start_time, e.end_time,
               COALESCE(fe.attempts, 0) AS attempts
          FROM event e
          LEFT JOIN sidecar.face_enrichments fe ON fe.event_id = e.id
         WHERE e.camera IN ({cam_q})
           AND e.label = 'person'
           AND e.end_time IS NOT NULL
           AND e.end_time >= ?
           AND e.end_time <= ?
           AND (fe.event_id IS NULL
                OR (fe.status = 'error' AND fe.attempts < ?))
         ORDER BY e.end_time
         LIMIT ?
    """
    args = [*cfg.cameras, lower, upper, cfg.max_attempts, cfg.max_events_per_cycle]
    return [
        Candidate(
            event_id=str(r["id"]),
            camera=str(r["camera"]),
            start_time=float(r["start_time"]),
            end_time=float(r["end_time"]),
            attempts=int(r["attempts"]),
        )
        for r in conn.execute(sql, args)
    ]


def sample_times(start: float, end: float, *, max_frames: int, min_gap_s: float) -> list[float]:
    """Uniform timestamps across [start, end], honoring both bounds."""
    span = max(0.0, end - start)
    if max_frames <= 1 or span <= min_gap_s:
        return [start + span / 2.0]
    n = min(max_frames, int(span / min_gap_s) + 1)
    step = span / (n - 1)
    return [start + i * step for i in range(n)]


# ---------------------------------------------------------------------------
# Matching / clustering: incremental centroid assignment, no batch reclustering.


def match_or_cluster(
    conn: sqlite3.Connection, embedding: list[float], *, cfg: Any, seen_ts: float
) -> tuple[int, str | None, float]:
    """Assign one event embedding to a cluster; returns (cluster_id, name, distance).

    Named clusters are tried at the tighter `match_threshold` (a wrong
    sub_label is worse than a missed one); unnamed ones at `cluster_threshold`.
    No match starts a new unnamed cluster at distance 0. The running-mean
    centroid update is order-dependent and slightly lossy, which is fine here —
    exact centroids are rebuilt from stored event embeddings on name/merge.
    """
    best_named: tuple[int, str, float] | None = None
    best_unnamed: tuple[int, float] | None = None
    for row in conn.execute(
        "SELECT cluster_id, name, centroid, observation_count FROM face_clusters"
    ):
        dist = cosine_distance(embedding, l2_normalize(unpack_embedding(row["centroid"])))
        if row["name"] is not None:
            if best_named is None or dist < best_named[2]:
                best_named = (int(row["cluster_id"]), str(row["name"]), dist)
        elif best_unnamed is None or dist < best_unnamed[1]:
            best_unnamed = (int(row["cluster_id"]), dist)

    if best_named is not None and best_named[2] <= cfg.match_threshold:
        cid, name, dist = best_named
        _fold_into(conn, cid, embedding, seen_ts)
        return (cid, name, dist)
    if best_unnamed is not None and best_unnamed[1] <= cfg.cluster_threshold:
        cid, dist = best_unnamed
        _fold_into(conn, cid, embedding, seen_ts)
        return (cid, None, dist)
    cur = conn.execute(
        "INSERT INTO face_clusters "
        "(name, centroid, observation_count, created_at, last_seen_at) "
        "VALUES (NULL, ?, 1, ?, ?)",
        (pack_embedding(embedding), _now_iso(), seen_ts),
    )
    return (int(cur.lastrowid or 0), None, 0.0)


def _fold_into(
    conn: sqlite3.Connection, cluster_id: int, embedding: list[float], seen_ts: float
) -> None:
    row = conn.execute(
        "SELECT centroid, observation_count, last_seen_at FROM face_clusters "
        "WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    if row is None:
        return
    count = int(row["observation_count"])
    centroid = unpack_embedding(row["centroid"])
    merged = l2_normalize(
        [(c * count + e) / (count + 1) for c, e in zip(centroid, embedding, strict=False)]
    )
    conn.execute(
        "UPDATE face_clusters SET centroid = ?, observation_count = ?, "
        "last_seen_at = MAX(last_seen_at, ?) WHERE cluster_id = ?",
        (pack_embedding(merged), count + 1, seen_ts, cluster_id),
    )


def rebuild_centroid(conn: sqlite3.Connection, cluster_id: int) -> None:
    """Exact centroid from the stored event embeddings (used on name/merge)."""
    embs = [
        unpack_embedding(r["embedding"])
        for r in conn.execute(
            "SELECT embedding FROM face_enrichments "
            "WHERE cluster_id = ? AND embedding IS NOT NULL AND excluded_at IS NULL",
            (cluster_id,),
        )
    ]
    if not embs:
        return
    centroid = aggregate(embs, [1.0] * len(embs))
    conn.execute(
        "UPDATE face_clusters SET centroid = ?, observation_count = ? "
        "WHERE cluster_id = ?",
        (pack_embedding(centroid), len(embs), cluster_id),
    )


def reap_stale_clusters(conn: sqlite3.Connection, *, cfg: Any, now: float) -> int:
    """Delete UNNAMED clusters unseen for cluster_ttl_days, with their embeddings.

    This is the retention boundary that keeps the feature from becoming an
    unbounded stranger database: the enrichment rows stay (they mirror
    Frigate's event history) but lose their cluster assignment and embedding.
    """
    cutoff = now - cfg.cluster_ttl_days * 86400.0
    stale = [
        int(r["cluster_id"])
        for r in conn.execute(
            "SELECT cluster_id FROM face_clusters "
            "WHERE name IS NULL AND last_seen_at < ?",
            (cutoff,),
        )
    ]
    for cid in stale:
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = NULL, embedding = NULL "
            "WHERE cluster_id = ?",
            (cid,),
        )
        conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cid,))
    # Crops of clusterless sightings past the TTL: the reaped clusters' events
    # (a stale cluster's last sighting is older than the cutoff, so all of its
    # events are) plus sightings a person detached and nothing re-claimed.
    orphaned = [
        str(r["event_id"])
        for r in conn.execute(
            "SELECT event_id FROM face_enrichments WHERE cluster_id IS NULL "
            "AND best_crop_path IS NOT NULL AND event_start_ts < ?",
            (cutoff,),
        )
    ]
    drop_crops(conn, cfg.crops_dir, orphaned)
    return len(stale)


# ---------------------------------------------------------------------------
# The model engine — the ONLY part that touches the [enrich] extra.


@dataclass(frozen=True)
class DetectedFace:
    """One face in one frame, everything downstream needs as plain numbers."""

    frame_ts: float
    area_px: float
    quality: float
    embedding: list[float]
    # Face box normalized to the frame, (x1, y1, x2, y2) in 0..1.
    bbox: tuple[float, float, float, float]


class _Engine:
    """Lazy insightface FaceAnalysis wrapper, CPU-only, one per process."""

    def __init__(self, model_dir: str) -> None:
        try:
            import cv2  # noqa: F401
            import numpy  # noqa: F401
            from insightface.app import FaceAnalysis
        except ImportError as exc:
            raise EnrichUnavailable(
                "insightface / onnxruntime / cv2 not installed. "
                'Install with `pip install "marcellus[enrich]"`.'
            ) from exc
        self._app = FaceAnalysis(
            name="buffalo_l",
            root=model_dir,
            providers=["CPUExecutionProvider"],
            allowed_modules=["detection", "recognition"],
        )
        self._app.prepare(ctx_id=-1, det_size=(640, 640))

    def faces_in_jpeg(
        self, jpeg: bytes, *, frame_ts: float, min_face_area_px: int
    ) -> list[DetectedFace]:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return []
        out: list[DetectedFace] = []
        for face in self._app.get(img):
            x1, y1, x2, y2 = (float(v) for v in face.bbox)
            area = max(0.0, x2 - x1) * max(0.0, y2 - y1)
            if area < min_face_area_px:
                continue
            h, w = img.shape[:2]
            crop = img[
                max(0, int(y1)) : min(h, int(y2)), max(0, int(x1)) : min(w, int(x2))
            ]
            if crop.size == 0:
                continue
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            kps = [(float(p[0]), float(p[1])) for p in face.kps] if face.kps is not None else []
            quality = quality_score(
                sharpness=sharp,
                area_px=area,
                frontality=pose_frontality(kps),
                min_face_area_px=min_face_area_px,
            )
            emb = face.normed_embedding
            if emb is None:
                continue
            out.append(
                DetectedFace(
                    frame_ts=frame_ts,
                    area_px=area,
                    quality=quality,
                    embedding=[float(v) for v in emb],
                    bbox=(x1 / w, y1 / h, x2 / w, y2 / h),
                )
            )
        return out


_ENGINE: _Engine | None = None


def _engine(model_dir: str) -> _Engine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = _Engine(model_dir)
    return _ENGINE


def check_available() -> None:
    """Raise EnrichUnavailable (import check only — does not load models)."""
    try:
        import cv2  # noqa: F401
        import insightface  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        raise EnrichUnavailable(
            "insightface / onnxruntime / cv2 not installed. "
            'Install with `pip install "marcellus[enrich]"`.'
        ) from exc


# ---------------------------------------------------------------------------
# Per-event processing and the cycle entrypoint.


def _record(
    conn: sqlite3.Connection,
    cand: Candidate,
    *,
    status: str,
    cluster_id: int | None = None,
    distance: float | None = None,
    faces_found: int = 0,
    faces_used: int = 0,
    best_quality: float | None = None,
    best: DetectedFace | None = None,
    best_crop_path: str | None = None,
    embedding: list[float] | None = None,
    sub_label_written: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO face_enrichments "
        "(event_id, camera, event_start_ts, cluster_id, distance, faces_found, faces_used, "
        " best_quality, embedding, sub_label_written, status, attempts, detail, processed_at, "
        " best_frame_ts, best_bbox, best_crop_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_id) DO UPDATE SET "
        " cluster_id=excluded.cluster_id, distance=excluded.distance, "
        " faces_found=excluded.faces_found, faces_used=excluded.faces_used, "
        " best_quality=excluded.best_quality, embedding=excluded.embedding, "
        " sub_label_written=excluded.sub_label_written, status=excluded.status, "
        " attempts=excluded.attempts, detail=excluded.detail, "
        " processed_at=excluded.processed_at, best_frame_ts=excluded.best_frame_ts, "
        " best_bbox=excluded.best_bbox, best_crop_path=excluded.best_crop_path",
        (
            cand.event_id,
            cand.camera,
            cand.start_time,
            cluster_id,
            distance,
            faces_found,
            faces_used,
            best_quality,
            pack_embedding(embedding) if embedding is not None else None,
            sub_label_written,
            status,
            cand.attempts + 1,
            detail,
            _now_iso(),
            best.frame_ts if best is not None else None,
            json.dumps([round(v, 5) for v in best.bbox]) if best is not None else None,
            best_crop_path,
        ),
    )


def process_event(
    conn: sqlite3.Connection, client: FrigateClient, cand: Candidate, *, cfg: Any
) -> str:
    """Run the full pipeline for one event; returns the terminal status."""
    engine = _engine(str(cfg.model_dir))
    faces: list[DetectedFace] = []
    frames_ok = 0
    # Only the frame holding the best face so far is kept, not all ~40.
    best_jpeg: bytes | None = None
    for ts in sample_times(
        cand.start_time, cand.end_time, max_frames=cfg.max_frames, min_gap_s=cfg.min_sample_gap_s
    ):
        jpeg, _status = client.recording_snapshot(cand.camera, ts, timeout=cfg.http_timeout_s)
        if jpeg is None:
            continue  # routine: no committed segment covers this instant
        frames_ok += 1
        found = engine.faces_in_jpeg(jpeg, frame_ts=ts, min_face_area_px=cfg.min_face_area_px)
        if found and (not faces or max(f.quality for f in found) > max(f.quality for f in faces)):
            best_jpeg = jpeg
        faces.extend(found)

    if frames_ok == 0:
        _record(conn, cand, status="no_frames")
        return "no_frames"
    usable = sorted(
        (f for f in faces if f.quality >= cfg.min_quality), key=lambda f: -f.quality
    )[: cfg.best_n]
    if not usable:
        _record(conn, cand, status="no_faces", faces_found=len(faces))
        return "no_faces"
    # usable[0] is the first face at the top quality, which is the face whose
    # frame best_jpeg holds (both resolve ties to the earliest).
    best = usable[0]
    crop_path = store_crop(cand.event_id, best, best_jpeg, cfg=cfg) if best_jpeg else None

    event_emb = aggregate([f.embedding for f in usable], [f.quality for f in usable])
    cluster_id, name, dist = match_or_cluster(conn, event_emb, cfg=cfg, seen_ts=cand.end_time)

    sub_label = None
    if name is not None:
        # Confidence from distance: 1.0 at dist 0, threshold maps to ~0.5.
        score = max(0.0, min(1.0, 1.0 - dist / (2.0 * cfg.match_threshold)))
        client.set_sub_label(cand.event_id, name, score=score)
        sub_label = name

    _record(
        conn,
        cand,
        status="enriched",
        cluster_id=cluster_id,
        distance=dist,
        faces_found=len(faces),
        faces_used=len(usable),
        best_quality=best.quality,
        best=best,
        best_crop_path=crop_path,
        embedding=event_emb,
        sub_label_written=sub_label,
    )
    return "enriched"


def run_cycle(settings: Settings, *, now: float | None = None) -> dict[str, Any]:
    """One worker cycle: find due events, process each, reap stale clusters.

    Sync by design — server.py runs it via asyncio.to_thread so inference
    never blocks the event loop. Per-event failures are recorded as
    status='error' and retried on later cycles; they never kill the cycle.
    """
    cfg = settings.face_enrich
    now = time.time() if now is None else now
    summary: dict[str, Any] = {"candidates": 0, "enriched": 0, "no_faces": 0,
                               "no_frames": 0, "errors": 0, "reaped": 0}
    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        candidates = find_candidates(conn, cfg=cfg, now=now)
        summary["candidates"] = len(candidates)
        if candidates:
            with FrigateClient(settings.frigate.base_url, timeout=cfg.http_timeout_s) as client:
                for cand in candidates:
                    try:
                        status = process_event(conn, client, cand, cfg=cfg)
                        summary[status] = summary.get(status, 0) + 1
                    except EnrichUnavailable:
                        raise  # config error, not per-event: surface to the loop
                    except Exception as exc:  # noqa: BLE001 - record and retry later
                        logger.exception("face_enrich: event %s failed", cand.event_id)
                        _record(conn, cand, status="error", detail=str(exc)[:300])
                        summary["errors"] += 1
                    conn.commit()
        summary["reaped"] = reap_stale_clusters(conn, cfg=cfg, now=now)
        conn.commit()
    finally:
        conn.close()
    if summary["candidates"]:
        logger.info("face_enrich cycle: %s", summary)
    return summary
