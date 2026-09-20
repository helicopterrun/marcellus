"""High-res cross-camera face capture (B2).

The capture camera (`gate-face` here) is a *supporting identification* camera:
its job is to show the FACE of a person some other front camera detected. When a
person event fires on a trigger camera, this module pulls the capture camera's
full-main-stream frame out of Frigate's recordings at that moment and parks it
under `face_capture.output_dir` for human review.

One HTTP GET does the whole job::

    GET {frigate.base_url}/api/{camera}/recordings/{unix_ts:.3f}/snapshot.jpg

returns the full main-stream frame -- 2560x1440 / ~330 KB / ~0.45s on gate-face,
verified live -- with a clean 404 when no recording covers the timestamp. No
ffmpeg, no -ss seek, no recordings-table lookup, no container->host path
mapping. Prior art: analysis/annotation_offset.py.

WHY THIS IS A TIMER JOB AND NOT AN MQTT HOOK
--------------------------------------------
That endpoint 404s until the recording segment covering the timestamp has been
COMMITTED, and segments commit at their END. Measured publish lag on this
deployment is 5.4-9.4s per camera; a live probe at now-5s 404s and at now-60s
returns 200. A "grab it the instant the event fires" implementation returns 404
every time -- and would fail silently. The work is inherently deferred by tens of
seconds, so it belongs in a oneshot behind a systemd timer
(contrib/marcellus-face-capture.*), not in an in-process asyncio task
competing with the scrub generator for the single uvicorn worker's threadpool.

Core deps only -- Pillow and httpx. Deliberately no cv2/numpy, so this runs
without any optional extra installed.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from marcellus import db
from marcellus.config import Settings

logger = logging.getLogger(__name__)

_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]")
_LAST_RUN = ".last-run.json"

# Statuses that mean "settled, do not reconsider this sample".
_TERMINAL = ("captured", "no_recording", "deduped", "skipped")


@dataclass(frozen=True)
class Candidate:
    """A trigger event that may deserve a capture."""

    event_id: str
    camera: str
    label: str
    start_time: float
    top_score: float | None


@dataclass(frozen=True)
class Sample:
    offset_ms: int
    frame_ts: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Pure helpers -- no I/O. This is the unit-test surface and where the design
# risk actually lives.
# --------------------------------------------------------------------------


def head_box(
    box: Sequence[float], *, head_fraction: float, pad: float
) -> tuple[float, float, float, float]:
    """Normalized person box ``[x, y, w, h]`` -> normalized ``(l, t, r, b)`` head crop.

    Takes the top `head_fraction` of the box, expands by `pad` (a fraction of
    the head box's own width/height) on all sides, and clamps to [0, 1].

    Returns the full frame ``(0, 0, 1, 1)`` for a degenerate or unusable box
    rather than raising: a nonsense box must cost the crop, not the run.
    Frigate does emit boxes that run past the frame edge, so this cannot assume
    well-formed input.
    """
    try:
        x, y, w, h = (float(v) for v in box[:4])
    except (TypeError, ValueError, IndexError):
        return (0.0, 0.0, 1.0, 1.0)
    if not (w > 0 and h > 0):
        return (0.0, 0.0, 1.0, 1.0)

    hh = h * max(0.0, min(1.0, head_fraction))
    if hh <= 0:
        return (0.0, 0.0, 1.0, 1.0)

    left, top = x, y
    right, bottom = x + w, y + hh
    px, py = w * pad, hh * pad
    left, top, right, bottom = left - px, top - py, right + px, bottom + py

    left, top = max(0.0, left), max(0.0, top)
    right, bottom = min(1.0, right), min(1.0, bottom)
    if right - left <= 0 or bottom - top <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    return (left, top, right, bottom)


def group_into_visits(
    candidates: Sequence[Candidate],
    *,
    dedup_window_s: float,
    max_visit_s: float,
    prior: tuple[str, float, float] | None = None,
) -> list[tuple[Candidate, str, bool]]:
    """Assign each candidate a ``visit_key`` and a capture/skip flag.

    Gap-chaining, not fixed buckets: a fixed 60s bucket splits a visit that
    straddles a boundary. `max_visit_s` caps a chain so a loiter does not chain
    forever -- the largest real cluster measured here was 26 events.

    `prior` is ``(visit_key, visit_start, last_ts)`` for the run's trailing
    visit, read back from the DB, so a chain survives a run boundary: run N
    captures event A, run N+1 sees event B 30s later but A is no longer a
    candidate.

    Returns ``[(candidate, visit_key, is_visit_head)]`` in ascending time.
    """
    out: list[tuple[Candidate, str, bool]] = []
    key: str | None = None
    visit_start = 0.0
    last_ts = 0.0
    if prior is not None:
        key, visit_start, last_ts = prior

    for c in sorted(candidates, key=lambda x: x.start_time):
        chains = (
            key is not None
            and (c.start_time - last_ts) <= dedup_window_s
            and (c.start_time - visit_start) <= max_visit_s
        )
        if chains:
            out.append((c, key or c.event_id, False))
        else:
            key = c.event_id
            visit_start = c.start_time
            out.append((c, key, True))
        last_ts = c.start_time
    return out


def samples_for(
    start_time: float, *, offsets_s: Sequence[float], annotation_offset_ms: int
) -> list[Sample]:
    """Sample timestamps for one trigger event.

    `annotation_offset_ms` is the TRIGGER camera's ``detect.annotation_offset``.
    Convention, per analysis/annotation_offset.py's ``_measure_event`` (which
    probes recordings at ``t_det + off_ms/1000``)::

        recording_time = detection_time + annotation_offset_ms / 1000
    """
    base = start_time + (annotation_offset_ms / 1000.0)
    seen: set[int] = set()
    out: list[Sample] = []
    for off in offsets_s:
        ms = int(round(float(off) * 1000.0))
        if ms in seen:
            continue
        seen.add(ms)
        out.append(Sample(offset_ms=ms, frame_ts=base + (ms / 1000.0)))
    return sorted(out, key=lambda s: s.offset_ms)


def relative_paths(
    trigger_event_id: str, offset_ms: int, frame_ts: float
) -> tuple[str, str]:
    """``(full, thumb)`` paths relative to ``output_dir``, date-sharded.

    Date shards keep ``ls`` usable, let prune drop a whole day, and make orphan
    cleanup a directory-level rule instead of a per-file DB lookup.

    The stem is whitelisted to ``[A-Za-z0-9._-]`` before it is joined. These ids
    come from Frigate's DB rather than a request, but building a filesystem path
    gets the same discipline either way.
    """
    day = datetime.fromtimestamp(frame_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    sign = "m" if offset_ms < 0 else "p"
    stem = _SAFE_STEM.sub("_", f"{trigger_event_id}_{sign}{abs(offset_ms):05d}")
    # Collapse dot runs and strip leading dots. Separators are already gone, so
    # this is not about traversal -- it is so a stem can never look like the
    # dot-prefixed control files that share this tree (.last-run.json,
    # .publish-* temporaries) or read as a hidden file.
    stem = re.sub(r"\.{2,}", ".", stem).lstrip(".") or "capture"
    return (f"{day}/{stem}.jpg", f"{day}/{stem}.thumb.jpg")


def render_preview(
    raw: bytes,
    *,
    crop: tuple[float, float, float, float] | None,
    max_edge: int,
    quality: int,
) -> tuple[bytes, int, int] | None:
    """``(jpeg_bytes, src_w, src_h)`` for the review thumbnail, or None.

    Uses ``Image.draft`` on the no-crop path (JPEG DCT scaling, the same trick
    as push/thumbnails and scrub/repair) so a 2560x1440 frame is not fully
    decoded just to make a 480px preview.

    CPU-bound: callers on the event loop must go through ``asyncio.to_thread``.
    Every failure returns None -- a frame that will not decode costs its
    thumbnail, not the capture.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is a core dep; be defensive anyway
        logger.warning("face-capture: Pillow unavailable, skipping preview")
        return None

    try:
        with Image.open(io.BytesIO(raw)) as im:
            src_w, src_h = im.size
            if crop is None:
                im.draft("RGB", (max_edge, max_edge))
            rgb = im.convert("RGB")

        if crop is not None:
            left, top, right, bottom = crop
            box = (
                int(left * src_w),
                int(top * src_h),
                int(right * src_w),
                int(bottom * src_h),
            )
            if box[2] - box[0] > 0 and box[3] - box[1] > 0:
                rgb = rgb.crop(box)

        w, h = rgb.size
        longest = max(w, h)
        if longest > max_edge:
            scale = max_edge / float(longest)
            rgb = rgb.resize((max(1, int(w * scale)), max(1, int(h * scale))))

        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=quality)
        return (buf.getvalue(), src_w, src_h)
    except Exception:  # noqa: BLE001 - a bad frame costs its thumbnail only
        logger.warning("face-capture: preview render failed", exc_info=True)
        return None


def aspect_ok(detect_aspect_v: float | None, frame_w: int, frame_h: int) -> bool:
    """Is the fetched frame the same aspect as the capture camera's detect stream?

    Normalized box coordinates only transfer between two images of the same
    aspect. An anamorphic crop that "works" is exactly the class of silent
    geometry bug this codebase keeps memorializing, so a mismatch means we
    refuse to crop rather than crop wrongly.
    """
    if not detect_aspect_v or frame_h <= 0:
        return False
    return abs(detect_aspect_v - (frame_w / float(frame_h))) < 0.02


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------


def _frigate_cameras(config_path: str | Path) -> dict[str, Any]:
    try:
        import yaml

        with open(config_path) as fh:
            return (yaml.safe_load(fh) or {}).get("cameras") or {}
    except Exception:  # noqa: BLE001 - a missing/parse-broken config is a warn, not a crash
        logger.warning("face-capture: could not read %s", config_path, exc_info=True)
        return {}


def annotation_offset_ms(config_path: str | Path, camera: str) -> int:
    """``cameras.<camera>.detect.annotation_offset`` in ms, or 0."""
    cam = (_frigate_cameras(config_path) or {}).get(camera) or {}
    try:
        return int(((cam.get("detect") or {}).get("annotation_offset")) or 0)
    except (TypeError, ValueError):
        return 0


def detect_aspect(config_path: str | Path, camera: str) -> float | None:
    """``detect.width / detect.height`` from Frigate's config, or None."""
    cam = (_frigate_cameras(config_path) or {}).get(camera) or {}
    det = cam.get("detect") or {}
    try:
        w, h = float(det["width"]), float(det["height"])
        return w / h if h else None
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def check_inputs(settings: Settings) -> list[str]:
    """Log the misconfigurations that would otherwise make this a silent no-op.

    Called at server startup AND at the top of every run. The output_dir probe
    is the important one: under ProtectSystem=strict with
    ReadWritePaths=/opt/marcellus, a path outside the install dir fails
    EROFS at *write* time, not at config load -- the feature would enable
    cleanly, log nothing and produce nothing.
    """
    cfg = settings.face_capture
    problems: list[str] = []
    if not cfg.enabled:
        return problems

    if not cfg.trigger_cameras:
        problems.append("face_capture.trigger_cameras is empty -- nothing will trigger")
    if not cfg.capture_camera:
        problems.append("face_capture.capture_camera is unset")

    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.output_dir / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        problems.append(
            f"face_capture.output_dir ({cfg.output_dir}) is not writable: {exc} -- "
            "the systemd unit runs ProtectSystem=strict with "
            "ReadWritePaths=/opt/marcellus, so it must live under that path"
        )

    cams = _frigate_cameras(settings.frigate.config_path)
    if cams and cfg.capture_camera and cfg.capture_camera not in cams:
        problems.append(
            f"face_capture.capture_camera ({cfg.capture_camera}) is not in Frigate's config"
        )

    for p in problems:
        logger.error("face-capture: %s", p)
    return problems


def _prior_visit(conn: sqlite3.Connection, since: float) -> tuple[str, float, float] | None:
    """The most recent visit still inside the chaining horizon, for continuity."""
    row = conn.execute(
        "SELECT visit_key, MIN(trigger_start_ts) AS visit_start, "
        "       MAX(trigger_start_ts) AS last_ts "
        "  FROM sidecar.face_captures WHERE trigger_start_ts >= ? "
        " GROUP BY visit_key ORDER BY last_ts DESC LIMIT 1",
        (since,),
    ).fetchone()
    if not row or row["visit_key"] is None:
        return None
    return (str(row["visit_key"]), float(row["visit_start"]), float(row["last_ts"]))


def find_candidates(
    conn: sqlite3.Connection, *, cfg: Any, now: float
) -> list[Candidate]:
    """Trigger events that are due and not already settled.

    One cross-DB LEFT JOIN rather than a round-trip per event.

    The upper time bound is ``now - capture_delay_s - max(offsets_s)``, NOT
    ``now - capture_delay_s``: the latest sample sits max(offsets_s) after the
    event's start_time, so bounding on start_time alone would ask for a frame
    that much younger than capture_delay_s promises and reliably 404 the last
    sample of every visit.
    """
    if not cfg.trigger_cameras or not cfg.trigger_labels:
        return []
    max_off = max(list(cfg.offsets_s) or [0.0])
    lower = now - cfg.lookback_s
    upper = now - cfg.capture_delay_s - max(0.0, max_off)
    if upper <= lower:
        return []

    cam_q = ",".join("?" * len(cfg.trigger_cameras))
    lbl_q = ",".join("?" * len(cfg.trigger_labels))
    sql = f"""
        SELECT e.id, e.camera, e.label, e.start_time, e.data
          FROM event e
          LEFT JOIN (
               SELECT trigger_event_id,
                      SUM(CASE WHEN status <> 'error' THEN 1 ELSE 0 END) AS n_settled,
                      MAX(attempts) AS attempts
                 FROM sidecar.face_captures
                WHERE trigger_start_ts >= ?
                GROUP BY trigger_event_id
          ) fc ON fc.trigger_event_id = e.id
         WHERE e.camera IN ({cam_q})
           AND e.label IN ({lbl_q})
           AND e.start_time >= ?
           AND e.start_time <= ?
           AND (fc.trigger_event_id IS NULL
                OR (fc.n_settled = 0 AND fc.attempts < ?))
         ORDER BY e.start_time
    """
    args = [lower, *cfg.trigger_cameras, *cfg.trigger_labels, lower, upper, cfg.max_attempts]
    out: list[Candidate] = []
    for row in conn.execute(sql, args):
        score = None
        try:
            score = db.event_top_score(row)
        except Exception:  # noqa: BLE001 - score is advisory only
            score = None
        out.append(
            Candidate(
                event_id=str(row["id"]),
                camera=str(row["camera"]),
                label=str(row["label"]),
                start_time=float(row["start_time"]),
                top_score=score,
            )
        )
    return out


def crop_box_for(
    conn: sqlite3.Connection, *, camera: str, frame_ts: float, labels: Sequence[str]
) -> tuple[str, list[float]] | None:
    """``(event_id, normalized [x, y, w, h])`` from the CAPTURE camera's own event
    LIVE at `frame_ts`, or None.

    Only a live event counts. A box borrowed from an event 20s away crops the
    wrong part of the frame, which is worse than not cropping at all. Read
    through ``db.parse_event_data`` -- the box lives in the `data` JSON blob,
    never in a column.
    """
    if not labels:
        return None
    lbl_q = ",".join("?" * len(labels))
    row = conn.execute(
        f"SELECT id, data, start_time, end_time FROM event "
        f" WHERE camera = ? AND label IN ({lbl_q}) "
        f"   AND start_time <= ? AND (end_time IS NULL OR end_time >= ?) "
        f" ORDER BY start_time DESC LIMIT 1",
        [camera, *labels, frame_ts, frame_ts],
    ).fetchone()
    if not row:
        return None
    try:
        parsed = db.parse_event_data(row)
        box = parsed.get("data_box")
    except Exception:  # noqa: BLE001
        return None
    if not box or len(box) < 4:
        return None
    return (str(row["id"]), [float(v) for v in box[:4]])


def _write_atomic(path: Path, data: bytes) -> None:
    """Publish a file atomically (mkstemp + os.replace), as scrub does."""
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


def scan(settings: Settings, *, now: float | None = None) -> dict[str, Any]:
    """One capture pass. Idempotent; safe to run on any cadence."""
    cfg = settings.face_capture
    now = time.time() if now is None else now
    summary: dict[str, Any] = {
        "enabled": bool(cfg.enabled),
        "candidates": 0,
        "captured": 0,
        "no_recording": 0,
        "error": 0,
        "deduped": 0,
        "bytes": 0,
        "pruned": 0,
    }
    if not cfg.enabled:
        return summary

    problems = check_inputs(settings)
    if problems:
        summary["problems"] = problems
        return summary

    from marcellus.frigate_api import FrigateAPIError, FrigateClient

    ann_ms: dict[str, int] = {}
    if cfg.apply_annotation_offset:
        for cam in cfg.trigger_cameras:
            ann_ms[cam] = annotation_offset_ms(settings.frigate.config_path, cam)
    cap_aspect = detect_aspect(settings.frigate.config_path, cfg.capture_camera)

    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    client = FrigateClient(settings.frigate.base_url, timeout=cfg.http_timeout_s)
    aspect_warned = False
    try:
        candidates = find_candidates(conn, cfg=cfg, now=now)
        summary["candidates"] = len(candidates)
        prior = _prior_visit(conn, now - cfg.lookback_s - cfg.dedup_window_s)
        grouped = group_into_visits(
            candidates,
            dedup_window_s=cfg.dedup_window_s,
            max_visit_s=cfg.max_visit_s,
            prior=prior,
        )

        taken = 0
        for cand, visit_key, is_head in grouped:
            if not is_head:
                _upsert(
                    conn, cand, visit_key, cfg.capture_camera, 0, cand.start_time,
                    status="deduped", detail=f"same visit as {visit_key}",
                )
                summary["deduped"] += 1
                continue
            if taken >= cfg.max_captures_per_run:
                break

            for smp in samples_for(
                cand.start_time,
                offsets_s=cfg.offsets_s,
                annotation_offset_ms=ann_ms.get(cand.camera, 0),
            ):
                if taken >= cfg.max_captures_per_run:
                    break
                taken += 1
                try:
                    raw, http_status = client.recording_snapshot(
                        cfg.capture_camera, smp.frame_ts, timeout=cfg.http_timeout_s
                    )
                except FrigateAPIError as exc:
                    logger.warning(
                        "face-capture: fetch failed for %s @ %.3f: %s",
                        cand.event_id, smp.frame_ts, exc,
                    )
                    _upsert(conn, cand, visit_key, cfg.capture_camera, smp.offset_ms,
                            smp.frame_ts, status="error", detail=str(exc)[:300],
                            bump_attempts=True)
                    summary["error"] += 1
                    continue

                if raw is None:
                    # Terminal: capture_delay_s has already elapsed, so no
                    # recording will ever appear for this instant.
                    logger.debug(
                        "face-capture: no recording for %s @ %.3f", cfg.capture_camera, smp.frame_ts
                    )
                    _upsert(conn, cand, visit_key, cfg.capture_camera, smp.offset_ms,
                            smp.frame_ts, status="no_recording", http_status=404)
                    summary["no_recording"] += 1
                    continue

                crop = None
                crop_event_id = None
                crop_box_json = None
                if cfg.crop_to_bbox:
                    got = crop_box_for(
                        conn, camera=cfg.capture_camera, frame_ts=smp.frame_ts,
                        labels=cfg.trigger_labels,
                    )
                    if got:
                        crop_event_id, box = got
                        crop_box_json = json.dumps(box)
                        crop = head_box(box, head_fraction=cfg.head_fraction, pad=cfg.crop_pad)

                rel_full, rel_thumb = relative_paths(cand.event_id, smp.offset_ms, smp.frame_ts)
                full_path = cfg.output_dir / rel_full
                thumb_path = cfg.output_dir / rel_thumb
                try:
                    _write_atomic(full_path, raw)
                except OSError as exc:
                    logger.exception("face-capture: write failed for %s", full_path)
                    _upsert(conn, cand, visit_key, cfg.capture_camera, smp.offset_ms,
                            smp.frame_ts, status="error", detail=str(exc)[:300],
                            bump_attempts=True)
                    summary["error"] += 1
                    continue

                # Aspect guard: normalized coords only transfer between images
                # of the same aspect. Refuse to crop rather than crop wrongly.
                pre = render_preview(raw, crop=None, max_edge=8, quality=50)
                fw, fh = (pre[1], pre[2]) if pre else (0, 0)
                if crop is not None and not aspect_ok(cap_aspect, fw, fh):
                    if not aspect_warned:
                        logger.error(
                            "face-capture: %s detect aspect %.4f != frame aspect %sx%s -- "
                            "not cropping (normalized boxes do not transfer across aspects)",
                            cfg.capture_camera, cap_aspect or -1, fw, fh,
                        )
                        aspect_warned = True
                    crop = None

                thumb = render_preview(
                    raw, crop=crop, max_edge=cfg.thumb_max_edge, quality=cfg.thumb_quality
                )
                if thumb:
                    try:
                        _write_atomic(thumb_path, thumb[0])
                    except OSError:
                        logger.warning("face-capture: thumb write failed for %s", thumb_path)

                _upsert(
                    conn, cand, visit_key, cfg.capture_camera, smp.offset_ms, smp.frame_ts,
                    status="captured", http_status=http_status,
                    full_path=rel_full, thumb_path=rel_thumb if thumb else None,
                    width=fw or None, height=fh or None, nbytes=len(raw),
                    crop_event_id=crop_event_id, crop_box=crop_box_json,
                )
                summary["captured"] += 1
                summary["bytes"] += len(raw)

        conn.commit()
    finally:
        conn.close()
        client.close()

    summary["pruned"] = prune(settings, now=now).get("rows", 0)
    _write_last_run(cfg.output_dir, summary, now)
    logger.info(
        "face-capture: %d candidate(s), %d captured, %d no_recording, "
        "%d error, %d deduped, %.1f MB",
        summary["candidates"], summary["captured"], summary["no_recording"],
        summary["error"], summary["deduped"], summary["bytes"] / 1e6,
    )
    return summary


def _upsert(
    conn: sqlite3.Connection,
    cand: Candidate,
    visit_key: str,
    capture_camera: str,
    offset_ms: int,
    frame_ts: float,
    *,
    status: str,
    http_status: int | None = None,
    detail: str | None = None,
    full_path: str | None = None,
    thumb_path: str | None = None,
    width: int | None = None,
    height: int | None = None,
    nbytes: int | None = None,
    crop_event_id: str | None = None,
    crop_box: str | None = None,
    bump_attempts: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO sidecar.face_captures (
            trigger_event_id, trigger_camera, trigger_label, trigger_start_ts,
            trigger_score, visit_key, capture_camera, offset_ms, frame_ts,
            status, attempts, http_status, detail, full_path, thumb_path,
            width, height, bytes, crop_event_id, crop_box, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(trigger_event_id, offset_ms) DO UPDATE SET
            status = excluded.status,
            attempts = face_captures.attempts + ?,
            http_status = excluded.http_status,
            detail = excluded.detail,
            full_path = COALESCE(excluded.full_path, face_captures.full_path),
            thumb_path = COALESCE(excluded.thumb_path, face_captures.thumb_path),
            width = COALESCE(excluded.width, face_captures.width),
            height = COALESCE(excluded.height, face_captures.height),
            bytes = COALESCE(excluded.bytes, face_captures.bytes),
            crop_event_id = COALESCE(excluded.crop_event_id, face_captures.crop_event_id),
            crop_box = COALESCE(excluded.crop_box, face_captures.crop_box)
        """,
        (
            cand.event_id, cand.camera, cand.label, cand.start_time, cand.top_score,
            visit_key, capture_camera, offset_ms, frame_ts, status,
            1 if bump_attempts else 0, http_status, detail, full_path, thumb_path,
            width, height, nbytes, crop_event_id, crop_box, _now_iso(),
            1 if bump_attempts else 0,
        ),
    )


def _write_last_run(output_dir: Path, summary: dict[str, Any], now: float) -> None:
    """Heartbeat for a job that runs in ANOTHER process.

    The scrub loop publishes app.state.scrub_last_cycle; a systemd-timer oneshot
    cannot, so liveness travels through this file instead and is surfaced on the
    status page.
    """
    try:
        payload = dict(summary)
        payload["ran_at"] = now
        _write_atomic(output_dir / _LAST_RUN, json.dumps(payload, indent=2).encode())
    except OSError:
        logger.warning("face-capture: could not write %s", _LAST_RUN)


def prune(settings: Settings, *, now: float | None = None) -> dict[str, int]:
    """Drop rows and files past `retention_days`.

    Rows first, then the paths those rows named, then whole date directories
    older than the cutoff -- which also reaps orphans from a write that
    succeeded before a commit that did not. Files are never discovered by
    walking: the table is authoritative, the tree is not.
    """
    cfg = settings.face_capture
    now = time.time() if now is None else now
    cutoff = now - (cfg.retention_days * 86400)
    out = {"rows": 0, "files": 0, "dirs": 0}

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute(
            "SELECT full_path, thumb_path FROM face_captures WHERE trigger_start_ts < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            for rel in (r["full_path"], r["thumb_path"]):
                if not rel:
                    continue
                try:
                    (cfg.output_dir / rel).unlink()
                    out["files"] += 1
                except OSError:
                    pass
        cur = conn.execute(
            "DELETE FROM face_captures WHERE trigger_start_ts < ?", (cutoff,)
        )
        out["rows"] = cur.rowcount or 0
        conn.commit()
    finally:
        conn.close()

    cutoff_day = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        for child in sorted(cfg.output_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name < cutoff_day:
                shutil.rmtree(child, ignore_errors=True)
                out["dirs"] += 1
    except OSError:
        pass
    return out


def read_last_run(settings: Settings) -> dict[str, Any] | None:
    try:
        return cast(
            "dict[str, Any]",
            json.loads((settings.face_capture.output_dir / _LAST_RUN).read_text()),
        )
    except (OSError, ValueError):
        return None
