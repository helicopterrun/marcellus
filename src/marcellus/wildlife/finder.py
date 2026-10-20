"""Wildlife finder: animals in footage where Frigate tracked nothing.

Frigate's model decides what becomes an event. When it scores an animal below
threshold, the animal is gone: no event, no review item, no training
example. The ground-truth case was a grey squirrel on gate-walkway for 45s
on 2026-10-17. Its segments recorded motion 30-58 and 29-48 detector regions
each, but 0 objects. Motion and detection both ran; the model never scored it
high enough.

This job re-examines exactly those segments, ``recordings.motion > 0 AND
objects = 0``, with models built for animals:

1. **Decode** a few keyframes per 10s segment (keyframe-only, no full decode).
2. **Detect** with MegaDetector v6 (YOLOv10-compact). Recall-first, so the
   score floor is low (0.2) and false positives are expected.
3. **Learned scenery.** A box overlapping enough recent boxes the classifier
   called blank is skipped unclassified. Static scenery (a pumpkin, a pine
   trunk) was ~94% of detector boxes in the benchmark.
4. **Classify** the rest with SpeciesNet. Its blank/human/vehicle mass
   rejects false positives, and its animal mass rolls up to a Frigate-style
   label suggestion (taxonomy.py).
5. **Group** animal hits per camera into visits for review at /wildlife. A
   kept frame can be sent to Frigate+ from there.

Work discovery follows the face pipeline's lookback pattern. A LEFT JOIN
against ``wildlife_segments`` over a lookback window is the whole queue, so
the job is self-healing across restarts with no cursor. It runs as a
systemd oneshot (contrib/marcellus-wildlife.*) because one run can hold two
cores for minutes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from marcellus import db
from marcellus.config import Settings, WildlifeSection
from marcellus.scrub.mapping import map_recording_path
from marcellus.wildlife import taxonomy
from marcellus.wildlife.engine import Detection, Frame, iou, render_jpeg

logger = logging.getLogger(__name__)

_LAST_RUN = ".last-run.json"

# Frigate labels the scoreboard counts as "wild animal caught by Frigate".
# dog is left out: on this property it is overwhelmingly the household's own.
WILD_LABELS = (
    "cat", "deer", "fox", "rabbit", "raccoon", "skunk", "squirrel", "rodent", "possum",
)

_SHOWINFO = re.compile(rb"pts_time:\s*(-?[0-9]+(?:\.[0-9]+)?).*?\ss:(\d+)x(\d+)")


class Models(Protocol):
    """What the scan needs from the model engine (a fake in tests)."""

    def detect(self, frame: Frame, *, size: int, min_score: float) -> list[Detection]: ...

    def classify(
        self, frame: Frame, boxes: list[Detection]
    ) -> list[list[tuple[str, float]]]: ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def local_hour(ts: float, tz_name: str) -> int:
    """Hour of day at `ts` in `tz_name`, or in the host clock when it is empty."""
    if tz_name:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(ts, ZoneInfo(tz_name)).hour
    return datetime.fromtimestamp(ts).hour


def in_hours(hour: int, window: tuple[int, int] | None) -> bool:
    """`[start, end)` membership, wrapping across midnight when start > end."""
    if window is None:
        return True
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def pick_indices(n: int, count: int) -> list[int]:
    """`count` indices spread evenly over `n` frames, centred in each slice.

    10 keyframes, 3 wanted -> [1, 5, 8]: the middle of each third, not the
    segment's first and last frames, which are only ~1s from the neighbouring
    segments' picks anyway.
    """
    if n <= 0 or count <= 0:
        return []
    if count >= n:
        return list(range(n))
    out: list[int] = []
    for i in range(count):
        idx = min(n - 1, int((i + 0.5) * n / count))
        if idx not in out:
            out.append(idx)
    return out


def chains(start: float, end: float, ts: float, *, gap_s: float, max_visit_s: float) -> bool:
    """Does a hit at `ts` belong to the visit spanning [start, end]?"""
    near = (start - gap_s) <= ts <= (end + gap_s)
    return near and (max(end, ts) - min(start, ts)) <= max_visit_s


def relative_paths(box_id: int, frame_ts: float) -> tuple[str, str]:
    """`(crop, frame)` image paths under output_dir, date-sharded like
    face captures so prune can drop whole days."""
    day = datetime.fromtimestamp(frame_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return (f"{day}/{int(box_id)}.crop.jpg", f"{day}/{int(box_id)}.frame.jpg")


# --------------------------------------------------------------------------
# Frame decode
# --------------------------------------------------------------------------


class FrameError(RuntimeError):
    pass


def parse_showinfo(stderr: bytes) -> list[tuple[float, int, int]]:
    """`(pts_seconds, width, height)` per frame from ffmpeg's showinfo lines."""
    out = []
    for line in stderr.splitlines():
        if b"showinfo" not in line:
            continue
        m = _SHOWINFO.search(line)
        if m:
            out.append((float(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def extract_frames(
    path: Path, *, width: int, count: int, timeout_s: float = 60.0
) -> list[Frame]:
    """Up to `count` keyframes from one segment, scaled to `width`, as RGB.

    Keyframe-only decode (`-skip_frame nokey`) is the scrub generator's trick:
    every camera here has a keyframe every 1s (package, crows-nest every 5s),
    so this is ~10 cheap intra-frame decodes instead of 100-200 full ones.
    Raw RGB on a pipe skips a JPEG encode/decode round trip per frame.
    """
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-skip_frame", "nokey", "-vsync", "0", "-i", str(path),
        "-vf", f"scale={int(width)}:-2,showinfo",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FrameError(f"ffmpeg on {path}: {exc}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip()[-300:].replace("\n", " | ")
        raise FrameError(f"ffmpeg rc={proc.returncode} on {path}: {tail or 'no stderr'}")
    info = parse_showinfo(proc.stderr)
    if not info:
        raise FrameError(f"no frames decoded from {path}")
    _, w, h = info[0]
    size = w * h * 3
    n = min(len(info), len(proc.stdout) // size)
    return [
        Frame(pts=info[i][0], width=w, height=h, rgb=proc.stdout[i * size : (i + 1) * size])
        for i in pick_indices(n, count)
    ]


# --------------------------------------------------------------------------
# Work discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    camera: str
    path: str
    start_ts: float
    end_ts: float
    attempts: int


def find_segments(
    conn: sqlite3.Connection,
    *,
    cfg: WildlifeSection,
    now: float,
    lower: float | None = None,
    upper: float | None = None,
    cameras: Sequence[str] | None = None,
) -> list[Segment]:
    """Unscanned motion-without-object segments, oldest first, inside each
    camera's `hours` window.

    `objects = 0` is the whole point: a segment where Frigate tracked
    something is one where its model already fired. That includes a
    segment where it tracked a person while an animal was also present, which is
    a known blind spot accepted for a ~3x smaller workload.
    """
    cams = [c for c in (cameras or list(cfg.cameras)) if c in cfg.cameras]
    if not cams:
        return []
    lo = now - cfg.lookback_s if lower is None else lower
    hi = now - cfg.process_delay_s if upper is None else upper
    if hi <= lo:
        return []
    q = ",".join("?" * len(cams))
    sql = f"""
        SELECT r.camera, r.path, r.start_time, r.end_time,
               COALESCE(ws.attempts, 0) AS attempts
          FROM recordings r
          LEFT JOIN sidecar.wildlife_segments ws
                 ON ws.camera = r.camera AND ws.start_ts = r.start_time
         WHERE r.camera IN ({q})
           AND r.start_time >= ? AND r.end_time <= ?
           AND r.motion > 0 AND COALESCE(r.objects, 0) = 0
           AND (ws.camera IS NULL OR (ws.status = 'error' AND ws.attempts < ?))
         ORDER BY r.start_time
    """
    out = []
    for r in conn.execute(sql, [*cams, lo, hi, cfg.max_attempts]):
        cam = str(r["camera"])
        start = float(r["start_time"])
        if not in_hours(local_hour(start, cfg.timezone), cfg.cameras[cam].hours):
            continue
        out.append(
            Segment(
                camera=cam,
                path=str(r["path"]),
                start_ts=start,
                end_ts=float(r["end_time"]),
                attempts=int(r["attempts"]),
            )
        )
    return out


# --------------------------------------------------------------------------
# Per-box decisions that read/write the sidecar DB
# --------------------------------------------------------------------------


def is_scenery(
    conn: sqlite3.Connection, camera: str, det: Detection, frame_ts: float, cfg: WildlifeSection
) -> bool:
    """Has this spot been classified blank often enough recently to skip it?"""
    if cfg.scenery_min_blanks <= 0:
        return False
    box = (det.x1, det.y1, det.x2, det.y2)
    hits = 0
    for r in conn.execute(
        "SELECT x1, y1, x2, y2 FROM sidecar.wildlife_boxes "
        " WHERE camera = ? AND verdict = 'blank' AND frame_ts >= ? AND frame_ts <= ?",
        (camera, frame_ts - cfg.scenery_memory_h * 3600.0, frame_ts),
    ):
        if iou(box, (r["x1"], r["y1"], r["x2"], r["y2"])) >= cfg.scenery_iou:
            hits += 1
            if hits >= cfg.scenery_min_blanks:
                return True
    return False


def assign_visit(
    conn: sqlite3.Connection, camera: str, ts: float, cfg: WildlifeSection
) -> int:
    """The pending visit an animal hit at `ts` joins, or a new one.

    Only PENDING visits are joined: a hit arriving after a visit was already
    reviewed starts a fresh visit rather than vanishing into a discarded one.
    Matching on a time window rather than "the latest visit" keeps this
    correct when an older segment is retried after newer ones were scanned.
    """
    row = conn.execute(
        "SELECT id, start_ts, end_ts FROM sidecar.wildlife_visits "
        " WHERE camera = ? AND review = 'pending' AND end_ts >= ? AND start_ts <= ? "
        " ORDER BY ABS(end_ts - ?) LIMIT 1",
        (camera, ts - cfg.visit_gap_s, ts + cfg.visit_gap_s, ts),
    ).fetchone()
    if row and chains(
        float(row["start_ts"]), float(row["end_ts"]), ts,
        gap_s=cfg.visit_gap_s, max_visit_s=cfg.max_visit_s,
    ):
        conn.execute(
            "UPDATE sidecar.wildlife_visits SET start_ts = MIN(start_ts, ?), "
            "end_ts = MAX(end_ts, ?), hits = hits + 1 WHERE id = ?",
            (ts, ts, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO sidecar.wildlife_visits (camera, start_ts, end_ts, hits, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (camera, ts, ts, _now_iso()),
    )
    return int(cur.lastrowid or 0)


def _write_atomic(path: Path, data: bytes) -> None:
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


@dataclass
class SegmentResult:
    frames: int = 0
    boxes: int = 0
    classified: int = 0
    animals: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)


def process_segment(
    conn: sqlite3.Connection,
    models: Models,
    seg: Segment,
    frames: list[Frame],
    cfg: WildlifeSection,
) -> SegmentResult:
    """Detect, filter, classify and record every box in one segment's frames."""
    res = SegmentResult(frames=len(frames))
    for frame in frames:
        frame_ts = seg.start_ts + frame.pts
        dets = models.detect(frame, size=cfg.frame_width, min_score=cfg.min_detector_score)
        res.boxes += len(dets)
        todo: list[Detection] = []
        for d in dets:
            if is_scenery(conn, seg.camera, d, frame_ts, cfg):
                _insert_box(conn, seg, frame_ts, d, verdict="scenery")
                res.verdicts["scenery"] = res.verdicts.get("scenery", 0) + 1
            else:
                todo.append(d)
        if not todo:
            continue
        tops = models.classify(frame, todo)
        res.classified += len(todo)
        for d, top in zip(todo, tops, strict=True):
            v = taxonomy.decide(top, reject_score=cfg.reject_score)
            verdict = v.verdict
            if verdict == "animal" and v.suggestion in cfg.exclude_suggestions:
                verdict = "excluded"
            visit_id = None
            if verdict == "animal":
                visit_id = assign_visit(conn, seg.camera, frame_ts, cfg)
            box_id = _insert_box(conn, seg, frame_ts, d, verdict=verdict, v=v, visit_id=visit_id)
            res.verdicts[verdict] = res.verdicts.get(verdict, 0) + 1
            if verdict != "animal":
                continue
            res.animals += 1
            rel_crop, rel_frame = relative_paths(box_id, frame_ts)
            box = (d.x1, d.y1, d.x2, d.y2)
            try:
                _write_atomic(
                    cfg.output_dir / rel_crop,
                    render_jpeg(frame, crop=box, pad=0.5, max_edge=320, quality=85),
                )
                _write_atomic(
                    cfg.output_dir / rel_frame,
                    render_jpeg(frame, outline=box, max_edge=960, quality=78),
                )
            except OSError:
                logger.warning("wildlife: image write failed for box %s", box_id, exc_info=True)
                continue
            conn.execute(
                "UPDATE sidecar.wildlife_boxes SET crop_path = ?, frame_path = ? WHERE id = ?",
                (rel_crop, rel_frame, box_id),
            )
    return res


def _insert_box(
    conn: sqlite3.Connection,
    seg: Segment,
    frame_ts: float,
    d: Detection,
    *,
    verdict: str,
    v: taxonomy.Verdict | None = None,
    visit_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO sidecar.wildlife_boxes (camera, segment_ts, frame_ts, x1, y1, x2, y2, "
        " det_score, verdict, reject_score, suggestion, suggestion_score, top_class, top_score, "
        " visit_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            seg.camera, seg.start_ts, frame_ts,
            round(d.x1, 5), round(d.y1, 5), round(d.x2, 5), round(d.y2, 5),
            round(d.score, 4), verdict,
            v.reject_score if v else None,
            v.suggestion if v else None,
            v.suggestion_score if v else None,
            v.top_class if v else None,
            v.top_score if v else None,
            visit_id, _now_iso(),
        ),
    )
    return int(cur.lastrowid or 0)


def _record_segment(
    conn: sqlite3.Connection,
    seg: Segment,
    *,
    status: str,
    res: SegmentResult | None = None,
    elapsed_ms: int | None = None,
    detail: str | None = None,
) -> None:
    r = res or SegmentResult()
    conn.execute(
        "INSERT INTO sidecar.wildlife_segments (camera, start_ts, status, attempts, frames, "
        " boxes, classified, animals, elapsed_ms, detail, scanned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(camera, start_ts) DO UPDATE SET status=excluded.status, "
        " attempts=excluded.attempts, frames=excluded.frames, boxes=excluded.boxes, "
        " classified=excluded.classified, animals=excluded.animals, "
        " elapsed_ms=excluded.elapsed_ms, detail=excluded.detail, scanned_at=excluded.scanned_at",
        (
            seg.camera, seg.start_ts, status, seg.attempts + 1, r.frames, r.boxes,
            r.classified, r.animals, elapsed_ms, detail, _now_iso(),
        ),
    )


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def check_inputs(settings: Settings) -> list[str]:
    """The misconfigurations that would otherwise make this a silent no-op."""
    cfg = settings.wildlife
    problems: list[str] = []
    if not cfg.enabled:
        return problems
    if not cfg.cameras:
        problems.append("wildlife.cameras is empty -- nothing will be scanned")
    for model in (cfg.detector_path, cfg.classifier_path, cfg.classifier_labels_path):
        if not Path(model).is_file():
            problems.append(
                f"model file {model} is missing -- see tools/wildlife_export_models.py"
            )
    if cfg.timezone:
        try:
            local_hour(0.0, cfg.timezone)
        except Exception:  # noqa: BLE001 - any failure means an unusable zone name
            problems.append(f"wildlife.timezone {cfg.timezone!r} is not a known IANA zone")
    if shutil.which("ffmpeg") is None:
        problems.append("ffmpeg is not on PATH")
    try:
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        probe = cfg.output_dir / ".write-probe"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        problems.append(
            f"wildlife.output_dir ({cfg.output_dir}) is not writable: {exc} -- the systemd "
            "unit runs ProtectSystem=strict with ReadWritePaths=/opt/marcellus"
        )
    for p in problems:
        logger.error("wildlife: %s", p)
    return problems


def _load_engine(cfg: WildlifeSection) -> Models:
    from marcellus.wildlife.engine import Engine

    return Engine(
        cfg.detector_path, cfg.classifier_path, cfg.classifier_labels_path, threads=cfg.threads
    )


def scan(
    settings: Settings,
    *,
    now: float | None = None,
    models: Models | None = None,
    lower: float | None = None,
    upper: float | None = None,
    cameras: Sequence[str] | None = None,
    max_runtime_s: float | None = None,
) -> dict[str, Any]:
    """One finder pass over due segments. Idempotent; bounded by max_runtime_s.

    `lower`/`upper`/`cameras` override the lookback window for a manual
    backfill (`marcellus wildlife scan --start ... --end ...`).
    """
    cfg = settings.wildlife
    now = time.time() if now is None else now
    budget = cfg.max_runtime_s if max_runtime_s is None else max_runtime_s
    summary: dict[str, Any] = {
        "enabled": bool(cfg.enabled), "due": 0, "scanned": 0, "deferred": 0,
        "no_file": 0, "error": 0, "frames": 0, "boxes": 0, "classified": 0,
        "animals": 0, "verdicts": {}, "pruned": 0,
    }
    if not cfg.enabled:
        return summary
    if models is None:
        problems = check_inputs(settings)
        if problems:
            summary["problems"] = problems
            return summary

    started = time.monotonic()
    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        segs = find_segments(conn, cfg=cfg, now=now, lower=lower, upper=upper, cameras=cameras)
        summary["due"] = len(segs)
        if segs:
            summary["oldest_due_age_s"] = round(now - segs[0].start_ts)
        # Loaded outside the per-segment try: a broken install is one loud
        # failure, not an error row on every segment. Skipped when idle.
        engine: Models | None = models
        if segs and engine is None:
            engine = _load_engine(cfg)
        for i, seg in enumerate(segs):
            assert engine is not None
            if time.monotonic() - started > budget:
                summary["deferred"] = len(segs) - i
                break
            t0 = time.monotonic()
            path = map_recording_path(
                seg.path, settings.frigate.media_path, settings.frigate.recordings_path
            )
            if not path.is_file():
                # Retention removed it or the mount is wrong; neither heals.
                _record_segment(conn, seg, status="no_file", detail=str(path)[:300])
                summary["no_file"] += 1
                conn.commit()
                continue
            try:
                frames = extract_frames(
                    path, width=cfg.frame_width, count=cfg.frames_per_segment
                )
                res = process_segment(conn, engine, seg, frames, cfg)
            except Exception as exc:  # noqa: BLE001 - record, retry on a later run
                conn.rollback()
                logger.warning("wildlife: %s @ %.0f failed: %s", seg.camera, seg.start_ts, exc)
                _record_segment(conn, seg, status="error", detail=str(exc)[:300])
                summary["error"] += 1
                conn.commit()
                continue
            _record_segment(
                conn, seg, status="scanned", res=res,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
            conn.commit()
            summary["scanned"] += 1
            summary["frames"] += res.frames
            summary["boxes"] += res.boxes
            summary["classified"] += res.classified
            summary["animals"] += res.animals
            for k, n in res.verdicts.items():
                summary["verdicts"][k] = summary["verdicts"].get(k, 0) + n
    finally:
        conn.close()

    summary["elapsed_s"] = round(time.monotonic() - started, 1)
    summary["pruned"] = prune(settings, now=now).get("boxes", 0)
    _write_last_run(cfg.output_dir, summary, now)
    logger.info(
        "wildlife: %d due, %d scanned, %d deferred, %d boxes, %d classified, %d animal hits",
        summary["due"], summary["scanned"], summary["deferred"], summary["boxes"],
        summary["classified"], summary["animals"],
    )
    return summary


def _write_last_run(output_dir: Path, summary: dict[str, Any], now: float) -> None:
    """Heartbeat for a job that runs in another process (cf. face capture)."""
    try:
        payload = dict(summary)
        payload["ran_at"] = now
        _write_atomic(output_dir / _LAST_RUN, json.dumps(payload, indent=2).encode())
    except OSError:
        logger.warning("wildlife: could not write %s", _LAST_RUN)


def read_last_run(settings: Settings) -> dict[str, Any] | None:
    try:
        return cast(
            "dict[str, Any]",
            json.loads((settings.wildlife.output_dir / _LAST_RUN).read_text()),
        )
    except (OSError, ValueError):
        return None


def prune(settings: Settings, *, now: float | None = None) -> dict[str, int]:
    """Drop rows and images past `retention_days`; the table is authoritative."""
    cfg = settings.wildlife
    now = time.time() if now is None else now
    cutoff = now - cfg.retention_days * 86400
    out = {"boxes": 0, "visits": 0, "segments": 0, "dirs": 0}
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        for r in conn.execute(
            "SELECT crop_path, frame_path FROM wildlife_boxes "
            " WHERE frame_ts < ? AND (crop_path IS NOT NULL OR frame_path IS NOT NULL)",
            (cutoff,),
        ).fetchall():
            for rel in (r["crop_path"], r["frame_path"]):
                if rel:
                    with contextlib.suppress(OSError):
                        (cfg.output_dir / rel).unlink()
        out["boxes"] = conn.execute(
            "DELETE FROM wildlife_boxes WHERE frame_ts < ?", (cutoff,)
        ).rowcount or 0
        out["visits"] = conn.execute(
            "DELETE FROM wildlife_visits WHERE end_ts < ?", (cutoff,)
        ).rowcount or 0
        out["segments"] = conn.execute(
            "DELETE FROM wildlife_segments WHERE start_ts < ?", (cutoff,)
        ).rowcount or 0
        conn.commit()
    finally:
        conn.close()
    cutoff_day = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")
    with contextlib.suppress(OSError):
        for child in sorted(cfg.output_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name < cutoff_day:
                shutil.rmtree(child, ignore_errors=True)
                out["dirs"] += 1
    return out


def scoreboard(
    settings: Settings, *, days: float, now: float | None = None
) -> list[dict[str, Any]]:
    """Per scanned camera: what Frigate caught vs what the finder found.

    `missed_share` = kept finder visits / (kept + Frigate wild-animal events).
    Every finder visit comes from a segment where Frigate tracked nothing, so a
    kept visit is by construction an animal Frigate missed. Frigate events are
    not deduplicated into visits, so the share is a lower bound. Pending visits
    are reported but not counted until someone reviews them.
    """
    cfg = settings.wildlife
    now = time.time() if now is None else now
    since = now - days * 86400
    rows: dict[str, dict[str, Any]] = {
        cam: {
            "camera": cam, "frigate_events": 0, "visits": 0, "pending": 0, "kept": 0,
            "discarded": 0, "frames_sent": 0, "segments": 0, "avg_ms": None,
            "missed_share": None,
        }
        for cam in cfg.cameras
    }
    if not rows:
        return []
    cams = list(rows)
    q = ",".join("?" * len(cams))
    lq = ",".join("?" * len(WILD_LABELS))
    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        for r in conn.execute(
            f"SELECT camera, COUNT(*) n FROM event WHERE camera IN ({q}) "
            f"AND label IN ({lq}) AND start_time >= ? GROUP BY camera",
            [*cams, *WILD_LABELS, since],
        ):
            rows[r["camera"]]["frigate_events"] = int(r["n"])
        for r in conn.execute(
            f"SELECT camera, review, COUNT(*) n FROM sidecar.wildlife_visits "
            f"WHERE camera IN ({q}) AND start_ts >= ? GROUP BY camera, review",
            [*cams, since],
        ):
            row = rows[r["camera"]]
            row["visits"] += int(r["n"])
            if r["review"] in ("pending", "kept", "discarded"):
                row[r["review"]] = int(r["n"])
        for r in conn.execute(
            f"SELECT camera, COUNT(*) n FROM sidecar.wildlife_boxes WHERE camera IN ({q}) "
            f"AND frame_ts >= ? AND plus_status = 'sent' GROUP BY camera",
            [*cams, since],
        ):
            rows[r["camera"]]["frames_sent"] = int(r["n"])
        for r in conn.execute(
            f"SELECT camera, COUNT(*) n, AVG(elapsed_ms) ms FROM sidecar.wildlife_segments "
            f"WHERE camera IN ({q}) AND start_ts >= ? AND status = 'scanned' GROUP BY camera",
            [*cams, since],
        ):
            rows[r["camera"]]["segments"] = int(r["n"])
            rows[r["camera"]]["avg_ms"] = round(float(r["ms"])) if r["ms"] is not None else None
    finally:
        conn.close()
    for row in rows.values():
        denom = row["kept"] + row["frigate_events"]
        if denom:
            row["missed_share"] = round(row["kept"] / denom, 3)
    return list(rows.values())
