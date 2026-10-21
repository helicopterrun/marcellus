"""The review inbox: a short daily queue ranked by what a verdict teaches Frigate+.

Labeling every detection doesn't scale: about 500 events a day, 95% of them
correct. A verdict on an easy, confident detection teaches the next model
nothing. So the inbox only offers items that carry signal, each with the
reason it was chosen:

- `phone`      "Not this" tapped on the phone (POST /v1/push/feedback). It
               means the alert wasn't wanted. Here the question is whether
               the detection itself was wrong.
- `missed`     a pending wildlife-finder visit: an animal Frigate never made
               an event of. Missed objects are the examples a model most
               lacks.
- `borderline` top_score in the band just above the camera/label threshold.
               Frigate+'s guide asks for true and false positives "near the
               threshold value" to widen the gap between them.
- `rare`       a label seen only a handful of times on that camera in the
               lookback (an animal on a stairway camera).
- `coverage`   any event on a camera the current Frigate+ model has fewer
               than `camera_target_images` verified images of (the guide's
               "at least 100 verified images per camera").

Selection is greedy by priority, with diversity built in. Each pick halves the
weight of other items in the same (camera, label, light) bucket, which
follows the guide's "cloudy days, sunny days, dawn, dusk, and night" advice.
No camera gets more than 40% of a batch.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from marcellus.config import Settings
from marcellus.db import open_joined, open_sidecar
from marcellus.encounters.notability import solar_elevation_deg
from marcellus.triage import outbox, plusmeta
from marcellus.triage.recorder import clear as recorder_clear
from marcellus.triage.recorder import record as recorder_record

VERDICTS = ("tp", "fp", "wrong", "skip")

#: Base priority per reason. Borderline adds up to 30 for closeness to the
#: threshold (so 50-80), a second strong reason adds 5, and coverage only adds
#: up to 5 on top of another reason: nearly every camera is under the 100
#: image target today, so stacking it would just inflate everything equally.
_PRIORITY = {
    "phone": 110.0, "missed": 95.0, "rare": 60.0, "borderline": 50.0, "coverage": 15.0,
}

#: Feedback verdicts that mean "this was right": anything else (the app
#: sends `not_useful` today) makes the event a candidate.
_POSITIVE_FEEDBACK = {"useful", "real", "tp", "good", "correct"}

#: Share of a batch one camera may take.
_CAMERA_SHARE = 0.4

#: Share of a batch wildlife visits may take, so a finder backlog (57
#: visits on the first day) still leaves room for Frigate's own detections.
_VISIT_SHARE = 0.6

#: Weight multiplier per item already picked from the same bucket.
_DIVERSITY_DECAY = 0.5

#: Wildlife hits shown per visit card (its strongest).
_VISIT_FRAMES = 4


@dataclass
class Item:
    key: str
    kind: str  # 'event' | 'visit'
    camera: str
    label: str
    ts: float
    light: str
    reasons: list[dict[str, str]] = field(default_factory=list)
    priority: float = 0.0
    score: float | None = None
    threshold: float | None = None
    box: list[float] | None = None
    event_id: str | None = None
    has_clip: bool = False
    visit_id: int | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    when: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ----- clocks -----


def _tz(settings: Settings) -> Any:
    name = settings.triage.timezone or settings.wildlife.timezone
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - fall back to UTC
            pass
    return timezone.utc


def local_midnight(settings: Settings, now: float) -> float:
    local = datetime.fromtimestamp(now, _tz(settings))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def when(settings: Settings, ts: float) -> str:
    return datetime.fromtimestamp(ts, _tz(settings)).strftime("%a %b %d %H:%M")


def light_bucket(settings: Settings, ts: float) -> str:
    """'day' | 'twilight' | 'night' -- civil twilight (sun within 6 degrees of
    the horizon) when encounters.latitude/longitude are set, else clock hours."""
    lat, lon = settings.encounters.latitude, settings.encounters.longitude
    if lat is not None and lon is not None:
        elev = solar_elevation_deg(ts, lat, lon)
        return "night" if elev < -6.0 else "twilight" if elev < 6.0 else "day"
    hour = datetime.fromtimestamp(ts, _tz(settings)).hour
    if hour >= 21 or hour < 6:
        return "night"
    if hour < 8 or hour >= 18:
        return "twilight"
    return "day"


# ----- candidates -----


def _reason(code: str, text: str) -> dict[str, str]:
    return {"code": code, "text": text}


def _phone_events(conn: sqlite3.Connection, since: float) -> set[str]:
    """Event ids behind "Not this" taps in the lookback.

    The app sends only the card key. `push_decisions` (30-day retention)
    maps a card back to the events that fed it.
    """
    out: set[str] = set()
    for f in conn.execute(
        "SELECT card_key, event_id, verdict FROM sidecar.push_feedback WHERE received_at >= ?",
        (since,),
    ):
        if str(f["verdict"]).lower() in _POSITIVE_FEEDBACK:
            continue
        if f["event_id"]:
            out.add(str(f["event_id"]))
            continue
        for d in conn.execute(
            "SELECT DISTINCT event_id FROM sidecar.push_decisions WHERE card_key = ?",
            (f["card_key"],),
        ):
            if d["event_id"]:
                out.add(str(d["event_id"]))
    return out


def _event_candidates(
    settings: Settings, meta: plusmeta.PlusMeta, now: float
) -> list[Item]:
    t = settings.triage
    since = now - t.lookback_days * 86400
    images = plusmeta.camera_images(settings, meta) if meta.ok else {}
    conn = open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        phone = _phone_events(conn, since)
        label_counts: dict[tuple[str, str], int] = {
            (r["camera"], r["label"]): int(r["n"])
            for r in conn.execute(
                "SELECT camera, label, COUNT(*) AS n FROM event WHERE start_time >= ? "
                "GROUP BY camera, label",
                (since,),
            )
        }
        rows = conn.execute(
            """
            SELECT e.id, e.camera, e.label, e.start_time, e.has_clip, e.top_score,
                   json_extract(e.data, '$.top_score') AS d_top,
                   json_extract(e.data, '$.box') AS box,
                   json_extract(e.data, '$.type') AS dtype
              FROM event e
              LEFT JOIN sidecar.triage_labels t ON t.event_id = e.id
             WHERE e.start_time >= ?
               AND e.end_time IS NOT NULL
               AND e.has_snapshot = 1
               AND e.plus_id IS NULL
               AND (e.false_positive IS NULL OR e.false_positive = 0)
               AND t.event_id IS NULL
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    out: list[Item] = []
    for r in rows:
        if r["dtype"] not in (None, "object") or r["box"] is None:
            continue
        cam, label = str(r["camera"]), str(r["label"])
        if meta.labels and label not in meta.labels:
            continue  # Frigate+ would refuse the annotation
        top = r["d_top"] if r["d_top"] is not None else r["top_score"]
        thr = meta.threshold(cam, label)
        reasons: list[dict[str, str]] = []
        prios: list[float] = []
        if r["id"] in phone:
            reasons.append(_reason("phone", "You tapped “Not this” on the phone"))
            prios.append(_PRIORITY["phone"])
        if top is not None:
            gap = float(top) - thr
            if gap < t.borderline_margin:
                closeness = 1.0 - max(0.0, gap) / t.borderline_margin
                reasons.append(_reason("borderline", f"scored {top:.2f}, threshold {thr:.2f}"))
                prios.append(_PRIORITY["borderline"] + 30.0 * closeness)
        n_here = label_counts.get((cam, label), 0)
        if n_here <= t.rare_label_max:
            reasons.append(
                _reason("rare", f"{label} seen {n_here}× here in {t.lookback_days} days")
            )
            prios.append(_PRIORITY["rare"])
        cover_gap = 0.0
        if meta.ok:
            have = images.get(cam, 0)
            if have < t.camera_target_images:
                cover_gap = 1.0 - have / t.camera_target_images
                reasons.append(
                    _reason(
                        "coverage",
                        f"Frigate+ has {have} verified image{'' if have == 1 else 's'} "
                        "of this camera",
                    )
                )
        if not reasons:
            continue
        try:
            box = json.loads(r["box"])
        except (TypeError, ValueError):
            box = None
        out.append(
            Item(
                key=f"event:{r['id']}",
                kind="event",
                camera=cam,
                label=label,
                ts=float(r["start_time"]),
                light=light_bucket(settings, float(r["start_time"])),
                reasons=reasons,
                priority=(
                    max(prios) + 5.0 * (len(prios) - 1) + 5.0 * cover_gap
                    if prios
                    else _PRIORITY["coverage"] + 30.0 * cover_gap
                ),
                score=float(top) if top is not None else None,
                threshold=thr,
                box=box,
                event_id=str(r["id"]),
                has_clip=bool(r["has_clip"]),
            )
        )
    return out


def _visit_candidates(settings: Settings, now: float) -> list[Item]:
    since = now - settings.triage.lookback_days * 86400
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        visits = conn.execute(
            "SELECT * FROM wildlife_visits WHERE review = 'pending' AND start_ts >= ?",
            (since,),
        ).fetchall()
        out: list[Item] = []
        for v in visits:
            hits = conn.execute(
                "SELECT id, frame_ts, det_score, reject_score, suggestion, suggestion_score, "
                "x1, y1, x2, y2 FROM wildlife_boxes WHERE visit_id = ? AND frame_path IS NOT NULL "
                "ORDER BY det_score * (1 - COALESCE(reject_score, 0)) DESC LIMIT ?",
                (v["id"], _VISIT_FRAMES),
            ).fetchall()
            if not hits:
                continue
            votes: dict[str, float] = {}
            for h in hits:
                if h["suggestion"]:
                    votes[h["suggestion"]] = votes.get(h["suggestion"], 0.0) + float(
                        h["suggestion_score"] or 0.0
                    )
            label = max(votes, key=lambda k: votes[k]) if votes else "animal"
            start = float(v["start_ts"])
            out.append(
                Item(
                    key=f"visit:{v['id']}",
                    kind="visit",
                    camera=str(v["camera"]),
                    label=label,
                    ts=start,
                    light=light_bucket(settings, start),
                    reasons=[
                        _reason("missed", "Frigate missed this; the wildlife finder found it")
                    ],
                    priority=_PRIORITY["missed"],
                    score=float(hits[0]["det_score"]),
                    visit_id=int(v["id"]),
                    frames=[
                        {
                            "box_id": int(h["id"]),
                            "offset_s": round(float(h["frame_ts"]) - start, 1),
                            "box": [h["x1"], h["y1"], h["x2"] - h["x1"], h["y2"] - h["y1"]],
                        }
                        for h in hits
                    ],
                )
            )
        return out
    finally:
        conn.close()


def select(items: list[Item], budget: int) -> list[Item]:
    """Greedy pick by priority, decayed per already-picked bucket.

    The camera and visit caps hold while anything uncapped is left; if they
    would leave the batch short (few cameras with candidates, a day of only
    wildlife), a second pass fills it without them.
    """
    if budget <= 0:
        return []
    cap = max(2, math.ceil(budget * _CAMERA_SHARE))
    visit_cap = max(1, math.ceil(budget * _VISIT_SHARE))
    pool = sorted(items, key=lambda i: (-i.priority, -i.ts))
    picked: list[Item] = []
    buckets: dict[tuple[str, str, str], int] = {}
    per_cam: dict[str, int] = {}
    visits = 0
    for capped in (True, False):
        while pool and len(picked) < budget:
            best_i, best_w = -1, -1.0
            for i, it in enumerate(pool):
                if it.priority <= best_w:
                    break  # sorted by priority: nothing later can beat best_w
                if capped and per_cam.get(it.camera, 0) >= cap:
                    continue
                if capped and it.kind == "visit" and visits >= visit_cap:
                    continue
                b = (it.camera, it.label, it.light)
                w = it.priority * _DIVERSITY_DECAY ** buckets.get(b, 0)
                if w > best_w:
                    best_i, best_w = i, w
            if best_i < 0:
                break
            it = pool.pop(best_i)
            picked.append(it)
            b = (it.camera, it.label, it.light)
            buckets[b] = buckets.get(b, 0) + 1
            per_cam[it.camera] = per_cam.get(it.camera, 0) + 1
            visits += it.kind == "visit"
    return picked


def reviewed_since(settings: Settings, since: float) -> int:
    stamp = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        labels = conn.execute(
            "SELECT COUNT(*) AS n FROM triage_labels WHERE source = 'inbox' AND labeled_at >= ?",
            (stamp,),
        ).fetchone()["n"]
        visits = conn.execute(
            "SELECT COUNT(*) AS n FROM wildlife_visits WHERE review != 'pending' "
            "AND reviewed_at >= ?",
            (stamp[:19],),
        ).fetchone()["n"]
    finally:
        conn.close()
    return int(labels) + int(visits)


def build(settings: Settings, *, more: bool = False, now: float | None = None) -> dict[str, Any]:
    """Today's batch plus the numbers around it."""
    now = time.time() if now is None else now
    meta = plusmeta.get(settings)
    budget = settings.triage.daily_budget
    done = reviewed_since(settings, local_midnight(settings, now))
    want = budget if more else max(0, budget - done)
    candidates = _event_candidates(settings, meta, now) + _visit_candidates(settings, now)
    items = select(candidates, want)
    for it in items:
        it.when = when(settings, it.ts)
    by_reason: dict[str, int] = {}
    for c in candidates:
        for r in c.reasons:
            by_reason[r["code"]] = by_reason.get(r["code"], 0) + 1
    return {
        "items": [i.as_dict() for i in items],
        "budget": budget,
        "done_today": done,
        "candidates": len(candidates),
        "by_reason": by_reason,
        "plus_meta_ok": meta.ok,
    }


# ----- verdicts -----


class VerdictError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _parse_key(key: str) -> tuple[str, str]:
    kind, _, ident = key.partition(":")
    if kind not in ("event", "visit") or not ident:
        raise VerdictError("invalid_key", f"bad item key {key!r}")
    return kind, ident


def apply_verdict(
    settings: Settings, *, key: str, verdict: str, correct_label: str | None = None,
    plus_enabled: bool, now: float | None = None,
) -> dict[str, Any]:
    """Record a verdict and queue its Frigate+ upload. Returns the plus status.

    The label is written even when the upload can't be queued: the verdict
    is still a true statement for threshold tuning, and the page shows why
    nothing was queued.
    """
    if verdict not in VERDICTS:
        raise VerdictError("invalid_verdict", f"verdict must be one of {VERDICTS}")
    now = time.time() if now is None else now
    kind, ident = _parse_key(key)
    if kind == "visit":
        return _visit_verdict(settings, int(ident), verdict, plus_enabled, now)

    label = {"tp": "tp", "fp": "fp", "wrong": "fp", "skip": "skip"}[verdict]
    correct = (correct_label or "").strip() or None
    if verdict == "wrong" and correct is None:
        correct = "unknown"
    try:
        recorder_record(
            frigate_db=settings.frigate.db_path, sidecar_db=settings.sidecar.db_path,
            event_id=ident, label=label,  # type: ignore[arg-type]
            note="wrong label" if verdict == "wrong" else None,
            force=True, correct_label=correct if verdict == "wrong" else None, source="inbox",
        )
    except LookupError as exc:
        raise VerdictError("not_found", "event not found", 404) from exc

    conn = open_sidecar(settings.sidecar.db_path)
    try:
        if verdict == "skip":
            outbox.cancel_event(conn, ident)
            conn.commit()
            return {"status": "none"}
        if not plus_enabled:
            return {"status": "off", "message": "Frigate+ isn't configured in Frigate"}
        try:
            q = outbox.enqueue_event(settings, conn, event_id=ident, kind=label, now=now)
        except outbox.NotSubmittable as exc:
            outbox.cancel_event(conn, ident)
            conn.commit()
            return {"status": "not_sent", "reason": exc.reason, "message": str(exc)}
        conn.commit()
        return {"status": "queued", "send_in_s": round(q.send_at - now), "outbox_id": q.id}
    finally:
        conn.close()


def _visit_verdict(
    settings: Settings, visit_id: int, verdict: str, plus_enabled: bool, now: float
) -> dict[str, Any]:
    # "wrong" on a visit means "an animal, but not the suggested species":
    # still a keep, and the uploaded frame carries no label to be wrong.
    keep = verdict in ("tp", "wrong")
    stamp = datetime.fromtimestamp(now, tz=timezone.utc).isoformat(timespec="seconds")
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        cur = conn.execute(
            "UPDATE wildlife_visits SET review = ?, reviewed_at = ? WHERE id = ?",
            ("kept" if keep else "discarded", stamp, visit_id),
        )
        if not cur.rowcount:
            raise VerdictError("not_found", "visit not found", 404)
        if not keep:
            outbox.cancel_visit(conn, visit_id)
            conn.commit()
            return {"status": "none"}
        if not plus_enabled:
            conn.commit()
            return {"status": "off", "message": "Frigate+ isn't configured in Frigate"}
        box_ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM wildlife_boxes WHERE visit_id = ? AND frame_path IS NOT NULL "
                "ORDER BY det_score * (1 - COALESCE(reject_score, 0)) DESC LIMIT ?",
                (visit_id, settings.triage.wildlife_frames_per_visit),
            )
        ]
        queued = outbox.enqueue_frames(settings, conn, box_ids=box_ids, now=now)
        conn.commit()
        if not queued:
            return {
                "status": "not_sent", "reason": "already_sent", "message": "frames already sent",
            }
        return {
            "status": "queued",
            "send_in_s": round(queued[0].send_at - now),
            "frames": len(queued),
        }
    finally:
        conn.close()


def undo(settings: Settings, *, key: str) -> dict[str, Any]:
    """Take a verdict back. An upload that already left can't be recalled."""
    kind, ident = _parse_key(key)
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        if kind == "visit":
            vid = int(ident)
            outbox.cancel_visit(conn, vid)
            sent = conn.execute(
                "SELECT COUNT(*) AS n FROM wildlife_boxes "
                "WHERE visit_id = ? AND plus_status = 'sent'",
                (vid,),
            ).fetchone()["n"]
            if sent:
                conn.commit()
                return {"ok": False, "reason": "already_sent",
                        "message": "already uploaded; remove it on plus.frigate.video"}
            conn.execute(
                "UPDATE wildlife_visits SET review = 'pending', reviewed_at = NULL WHERE id = ?",
                (vid,),
            )
            conn.commit()
            return {"ok": True}
        outbox.cancel_event(conn, ident)
        conn.commit()
        if outbox.sent_for_event(conn, ident):
            return {"ok": False, "reason": "already_sent",
                    "message": "already uploaded; remove it on plus.frigate.video"}
    finally:
        conn.close()
    recorder_clear(sidecar_db=settings.sidecar.db_path, event_id=ident)
    return {"ok": True}
