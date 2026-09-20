"""Replay scenario core: importable by both the CLI and the web UI.

All scenario logic lives here. The CLI (`tools/replay_card.py`) is a thin
wrapper; the web handler (`routes/replay.py`) calls `start_run` to drive
scenarios through the sidecar's own MQTT connection.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from marcellus.config import PushSection

logger = logging.getLogger(__name__)

# Package data, NOT repo-relative. The wheel ships only src/marcellus, so a
# `tools/`-relative path resolved to /usr/local/lib/python3.10/tools on an installed
# sidecar: every scenario silently vanished (/replay listed none, every run 400'd)
# while the repo checkout looked fine. Same idiom as server.py's _TEMPLATES_DIR.
SCENARIOS_DIR = Path(__file__).parent / "replay_scenarios"
REPLAY_ID_PREFIX = "replay-"


# ---------------------------------------------------------------------------
# Scenario discovery / loading / message building
# ---------------------------------------------------------------------------

def list_scenarios() -> list[dict[str, str]]:
    """Every scenario in the packaged set.

    Globs `*.json` rather than `card-*.json`: captures exported from the flight
    recorder are named `cap-*` so the picker can tell recorded traffic from a
    hand-written template at a glance, and they would otherwise be invisible.
    The directory is dedicated package data, so there is nothing else in it to
    pick up by accident.
    """
    results = []
    for p in sorted(SCENARIOS_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        results.append({
            "name": p.stem,
            "description": data.get("description", ""),
            "kind": str(data.get("kind") or "steps"),
        })
    return results


def resolve_scenario_path(name: str) -> Path:
    direct = Path(name)
    if direct.suffix == ".json" and direct.exists():
        return direct
    candidate = SCENARIOS_DIR / f"{name}.json"
    if not candidate.exists():
        raise FileNotFoundError(
            f"no scenario named {name!r} ({candidate} does not exist)"
        )
    return candidate


def load_scenario(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if data.get("kind") == "capture":
        return _load_capture_scenario(path, data)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path}: scenario must define a non-empty 'steps' list")
    for i, step in enumerate(steps):
        if step.get("type") not in ("new", "update", "end"):
            raise ValueError(f"{path}: steps[{i}].type must be new|update|end")
        topic = step.get("topic", "frigate/reviews")
        if topic not in ("frigate/reviews", "frigate/events"):
            raise ValueError(f"{path}: steps[{i}].topic must be frigate/reviews or frigate/events")
    return cast("dict[str, Any]", data)


# ---------------------------------------------------------------------------
# Capture-derived scenarios
#
# A `steps` scenario is a template: `build_messages` synthesises a payload from
# a handful of fields (zones, severity, objects), and everything it does not
# name -- score, box, sub_label, path_data, thumbnails -- simply is not there.
# That gap is what `push/capture.py` was written about: "the gap between a
# canned scenario and a real walk kept hiding bugs (family gating, copy echo,
# demotion -- all found live, none by replay)".
#
# A `capture` scenario is the other thing: the recorded wire, every field
# verbatim, with real inter-message timing. Two rewrites make it replayable
# rather than merely readable -- ids are moved into the `replay-` namespace so
# a replay starts a fresh card instead of resuming the original, and wall-clock
# timestamps are shifted so the story happens now.
# ---------------------------------------------------------------------------

#: A number in this range is a wall-clock timestamp, not data. Checked against
#: 534 captured messages: every `start_time` / `end_time` / `frame_time` /
#: `thumb_time`, and every timestamp buried in `path_data`, lands inside it, and
#: nothing else in a Frigate payload does (scores are 0-1, boxes and areas are
#: orders of magnitude below 1e9). Range-testing rather than key-matching is
#: what reaches the timestamps inside `path_data`'s anonymous [[x, y], ts]
#: pairs, which no key name would find.
_EPOCH_MIN = 1_000_000_000.0
_EPOCH_MAX = 20_000_000_000.0

_SAFE_STEM = re.compile(r"[^A-Za-z0-9._-]+")


def _load_capture_scenario(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"{path}: capture scenario must define a non-empty 'messages' list")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"{path}: messages[{i}] must be an object")
        if msg.get("topic") not in ("frigate/reviews", "frigate/events"):
            raise ValueError(
                f"{path}: messages[{i}].topic must be frigate/reviews or frigate/events"
            )
        if not isinstance(msg.get("payload"), dict):
            raise ValueError(f"{path}: messages[{i}].payload must be an object")
    return data


def _shift_epochs(obj: Any, delta: float) -> Any:
    """Move every wall-clock timestamp in a payload by `delta`.

    Relative distances survive exactly, so a replayed story keeps the durations
    it really had -- which is the whole reason to replay a capture instead of a
    hand-written approximation. Mutates dicts and lists in place and returns
    `obj` so scalars can be reassigned by the caller.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            obj[key] = _shift_epochs(value, delta)
        return obj
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            obj[i] = _shift_epochs(value, delta)
        return obj
    # bool is an int subclass; check it first or True becomes 1000000001.0.
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)) and _EPOCH_MIN <= obj < _EPOCH_MAX:
        return obj + delta
    return obj


def _capture_ids(messages: list[dict[str, Any]]) -> list[str]:
    """Every Frigate id a captured window refers to, in first-seen order.

    Both the `id` fields and the review's `data.detections` list, because a
    review and its object events cross-reference each other: rewriting one and
    not the other severs the link the card pipeline follows to enrich a card.
    """
    seen: dict[str, None] = {}
    for msg in messages:
        payload = msg.get("payload") or {}
        for side in ("before", "after"):
            body = payload.get(side)
            if not isinstance(body, dict):
                continue
            ident = body.get("id")
            if isinstance(ident, str) and ident:
                seen.setdefault(ident, None)
            data = body.get("data")
            if isinstance(data, dict):
                for det in data.get("detections") or []:
                    if isinstance(det, str) and det:
                        seen.setdefault(det, None)
    return list(seen)


def _substitute_strings(obj: Any, mapping: dict[str, str]) -> Any:
    """Whole-string replacement only -- never substring.

    An id or a camera name is replaced when a field *is* that value, so a
    description or a path that merely contains it is left alone.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            obj[key] = _substitute_strings(value, mapping)
        return obj
    if isinstance(obj, list):
        for i, value in enumerate(obj):
            obj[i] = _substitute_strings(value, mapping)
        return obj
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    return obj


def _scenario_stem(name: str) -> str:
    """A filename that cannot leave the scenarios directory.

    Same hardening as `faces/crosscam.py`: dot runs collapse and leading dots
    are stripped, so neither `..` nor a name that would hide the file (or
    collide with a dotfile) can be talked into existence by a `--export`
    argument.
    """
    stem = _SAFE_STEM.sub("-", name.strip())
    stem = re.sub(r"\.{2,}", ".", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip("-.")
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    if not stem:
        stem = "capture"
    # `cap-` marks recorded traffic in the picker; `card-` are the templates.
    if not stem.startswith(("cap-", "card-")):
        stem = f"cap-{stem}"
    return stem


def prune_to_story(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep the reviews and the object events they actually reference.

    A busy minute on this property is ~400 messages, ~1.3 MB, and almost all of
    it is other cameras' concurrent object updates -- a fixture that carried it
    all would be 2.7 MB of noise wrapped around a 6-message story, and the
    packaged wheel would grow by more than its own size per scenario.

    The card pipeline reaches an object event through a review's
    `data.detections`, so an event no review in the window names cannot affect
    the story. Every retained message is still byte-for-byte what Frigate sent;
    this drops whole messages, never fields.

    Returns `(kept, dropped_per_camera)`. If no review names any detection --
    an older Frigate, or a window with no reviews at all -- nothing is dropped,
    because then this rule has no evidence to act on.
    """
    referenced: set[str] = set()
    for row in rows:
        if not str(row.get("topic", "")).endswith("reviews"):
            continue
        payload = row.get("payload") or {}
        for side in ("before", "after"):
            body = payload.get(side)
            if not isinstance(body, dict):
                continue
            data = body.get("data")
            if isinstance(data, dict):
                for det in data.get("detections") or []:
                    if isinstance(det, str):
                        referenced.add(det)
    if not referenced:
        return list(rows), {}

    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    for row in rows:
        if str(row.get("topic", "")).endswith("reviews"):
            kept.append(row)
            continue
        payload = row.get("payload") or {}
        ids = {
            body.get("id")
            for side in ("before", "after")
            if isinstance(body := payload.get(side), dict)
        }
        if ids & referenced:
            kept.append(row)
        else:
            after = payload.get("after")
            cam = after.get("camera") if isinstance(after, dict) else None
            key = cam if isinstance(cam, str) else "?"
            dropped[key] = dropped.get(key, 0) + 1
    return kept, dropped


def summarize_capture(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """What a window contains -- the provenance block, and the material for an
    auto-description. Kept separate from `export_capture` so a caller can show
    it before deciding to freeze the window."""
    from marcellus.push.capture import _camera_of

    cameras: dict[str, int] = {}
    zones: dict[str, None] = {}
    labels: dict[str, None] = {}
    reviews = 0
    for row in rows:
        cam = _camera_of(row)
        if cam:
            cameras[cam] = cameras.get(cam, 0) + 1
        payload = row.get("payload") or {}
        after = payload.get("after")
        if not isinstance(after, dict):
            continue
        if str(row.get("topic", "")).endswith("reviews"):
            reviews += 1
            data = after.get("data")
            if isinstance(data, dict):
                for zone in data.get("zones") or []:
                    if isinstance(zone, str):
                        zones.setdefault(zone, None)
                for obj in data.get("objects") or []:
                    if isinstance(obj, str):
                        labels.setdefault(obj, None)
        else:
            lbl = after.get("label")
            if isinstance(lbl, str):
                labels.setdefault(lbl, None)

    first, last = float(rows[0]["ts"]), float(rows[-1]["ts"])
    return {
        "cameras": dict(sorted(cameras.items(), key=lambda kv: (-kv[1], kv[0]))),
        "zones": list(zones),
        "labels": list(labels),
        "reviews": reviews,
        "events": len(rows) - reviews,
        "messages": len(rows),
        "window_s": round(last - first, 3),
        "captured_from": datetime.fromtimestamp(first, tz=timezone.utc).isoformat(),
        "captured_to": datetime.fromtimestamp(last, tz=timezone.utc).isoformat(),
    }


def identities_in(rows: list[dict[str, Any]]) -> list[str]:
    """Recognized names a window would publish.

    Face recognition puts the person's actual name in `sub_label`, and this
    repository is public. A fixture is checked in forever, so "no names in this
    particular window" has to be established at export time rather than assumed
    -- the six hand-written scenarios predate recognition being enabled and the
    question never came up.
    """
    found: dict[str, None] = {}
    for row in rows:
        payload = row.get("payload") or {}
        for side in ("before", "after"):
            body = payload.get(side)
            if not isinstance(body, dict):
                continue
            candidates = [body.get("sub_label")]
            data = body.get("data")
            if isinstance(data, dict):
                candidates.extend(data.get("sub_labels") or [])
            for value in candidates:
                if isinstance(value, str) and value.strip():
                    found.setdefault(value, None)
    return list(found)


def export_capture(
    rows: list[dict[str, Any]],
    *,
    name: str,
    description: str = "",
    out_dir: Path | None = None,
    overwrite: bool = False,
    relevant_only: bool = True,
    allow_identities: bool = False,
) -> Path:
    """Freeze a window of the MQTT flight recorder into a checked-in scenario.

    This is the step the flight recorder was missing. `tools/replay_capture.py`
    could already republish a window, but a window is not a fixture: the
    recorder is size-rotated and holds hours, so a real situation ages out long
    before anyone can build a test around it. Writing it into the packaged
    scenario set makes it permanent, named, reviewable in a diff, and runnable
    from the same picker as the hand-written ones.

    Payloads are stored verbatim -- no distillation into the `steps` schema,
    which would throw away exactly the fields (score, box, sub_label,
    path_data) that make a capture worth keeping.
    """
    if not rows:
        raise ValueError("nothing to export: the window is empty")

    dropped: dict[str, int] = {}
    if relevant_only:
        rows, dropped = prune_to_story(rows)
        if not rows:
            raise ValueError("nothing to export: the window has no reviews to build a story from")

    if not allow_identities and (names := identities_in(rows)):
        raise ValueError(
            f"window contains recognized identities ({', '.join(sorted(names))}); "
            "pass allow_identities to export it anyway"
        )

    stem = _scenario_stem(name)
    target_dir = Path(out_dir) if out_dir is not None else SCENARIOS_DIR
    path = target_dir / f"{stem}.json"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (pass overwrite to replace it)")

    source = summarize_capture(rows)
    camera = next(iter(source["cameras"]), "") or ""
    labels = source["labels"]
    if not description:
        zones = ", ".join(source["zones"]) or "no zones"
        description = (
            f"Captured {source['captured_from'][:16].replace('T', ' ')}Z: "
            f"{camera or 'multi-camera'} {'/'.join(labels) or 'traffic'} near {zones} -- "
            f"{source['reviews']} reviews / {source['events']} events "
            f"over {source['window_s']:.0f}s of real wire."
        )

    document = {
        "id": stem,
        "kind": "capture",
        "camera": camera,
        "label": labels[0] if labels else "",
        "description": description,
        "source": {
            **source,
            # Provenance, so the fixture itself records that it is the story and
            # not the whole minute -- and exactly how much was set aside.
            "dropped_unreferenced": dropped,
            "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        "messages": [
            {"ts": float(row["ts"]), "topic": row["topic"], "payload": row["payload"]}
            for row in rows
        ],
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2) + "\n")
    tmp.replace(path)
    return path


def build_messages(
    scenario: dict[str, Any],
    *,
    camera: str | None = None,
    label: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expand a scenario into ordered MQTT messages.

    Each returned item: `{"delay_s": float, "topic": str, "payload": dict}`,
    plus `"capture_base"` for capture scenarios (see `stamp_now`).
    """
    if scenario.get("kind") == "capture":
        return _build_capture_messages(scenario, camera=camera, label=label, run_id=run_id)

    camera = camera or scenario.get("camera") or "doorbell"
    label = label or scenario.get("label") or "person"
    run_id = run_id or uuid.uuid4().hex[:8]
    scenario_id = scenario.get("id") or "card"
    review_id = f"{REPLAY_ID_PREFIX}{scenario_id}-{run_id}"
    track_id = f"{review_id}-t1"

    messages: list[dict[str, Any]] = []
    prev_review_after: dict[str, Any] | None = None
    prev_event_after: dict[str, Any] | None = None

    for step in scenario["steps"]:
        msg_type = step["type"]
        topic = step.get("topic", "frigate/reviews")
        zones = step.get("zones", [])

        if topic == "frigate/events":
            after: dict[str, Any] = {
                "id": track_id,
                "camera": camera,
                "label": label,
                "current_zones": zones,
                "entered_zones": zones,
                "start_time": None,
                "end_time": None,
                "stationary": False,
                "sub_label": step.get("sub_labels", [None])[0] if step.get("sub_labels") else None,
            }
            payload = {
                "type": msg_type,
                "before": prev_event_after or after,
                "after": after,
            }
            prev_event_after = after
        else:
            after = {
                "id": review_id,
                "camera": camera,
                "severity": step.get("severity", "alert"),
                "start_time": None,
                "end_time": None,
                "data": {
                    "objects": step.get("objects", [label]),
                    "detections": [track_id],
                    "zones": zones,
                    "audio": step.get("audio", []),
                    "sub_labels": step.get("sub_labels", []),
                },
            }
            payload = {
                "type": msg_type,
                "before": prev_review_after or after,
                "after": after,
            }
            prev_review_after = after

        messages.append({
            "delay_s": float(step.get("delay_s", 0.0)),
            "topic": topic,
            "payload": payload,
        })
    return messages


def _build_capture_messages(
    scenario: dict[str, Any],
    *,
    camera: str | None = None,
    label: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recorded wire -> replayable messages: ids renamespaced, timing derived.

    Timestamps are deliberately NOT shifted here. `build_messages` runs once up
    front for every scenario in a run, but `start_run` staggers them, so a shift
    applied at build time would land the later stories in the past. The runner
    shifts at publish time using `capture_base`.
    """
    if label:
        raise ValueError(
            "a capture scenario's labels are recorded wire, not a template -- "
            "drop --label, or export a window that has the label you want"
        )
    run_id = run_id or uuid.uuid4().hex[:8]
    scenario_id = str(scenario.get("id") or "capture")
    recorded = copy.deepcopy(cast("list[dict[str, Any]]", scenario["messages"]))

    # One replacement pass, exact-match: the ids, plus the camera if the caller
    # is retargeting the story at a different one.
    #
    # Whole-string only, which deliberately leaves the original id where it is
    # EMBEDDED in an asset path -- `thumb_path` is
    # ".../thumb-stairway-wide-1787442590.078477-figu26.webp". That looks like a
    # miss and is not one: nothing in the push pipeline reads `thumb_path` (it
    # is Frigate's own pointer), the card is keyed on the review `id`, which is
    # rewritten, and leaving it means a replayed card still points at the real
    # thumbnail the story produced rather than a file that never existed.
    replacements = {
        original: f"{REPLAY_ID_PREFIX}{scenario_id}-{run_id}-{i + 1}"
        for i, original in enumerate(_capture_ids(recorded))
    }
    recorded_camera = scenario.get("camera")
    if camera and isinstance(recorded_camera, str) and camera != recorded_camera:
        replacements[recorded_camera] = camera

    base = float(recorded[0].get("ts") or 0.0)
    messages: list[dict[str, Any]] = []
    previous = base
    for row in recorded:
        ts = float(row.get("ts") or previous)
        messages.append({
            "delay_s": max(0.0, ts - previous),
            "topic": row["topic"],
            "payload": _substitute_strings(row["payload"], replacements),
            "capture_base": base,
        })
        previous = ts
    return messages


def stamp_now(
    payload: dict[str, Any],
    *,
    start_time: float,
    clock: Callable[[], float],
    capture_base: float | None = None,
) -> None:
    """Put a message on the current clock.

    For a template scenario that means assigning `start_time` (and an end stamp
    on the closing message) -- there is nothing else in the payload to move. For
    a capture, assignment would be destructive: the payload carries real times in
    a dozen places and their offsets *from each other* are the fixture. So the
    whole payload shifts by one delta instead.
    """
    if capture_base is not None:
        _shift_epochs(payload, start_time - capture_base)
        return
    after = payload.get("after", {})
    after["start_time"] = start_time
    if payload.get("type") == "end":
        after["end_time"] = clock()


def run_scenario(
    messages: list[dict[str, Any]],
    *,
    speed: float = 1.0,
    publish: Callable[[str, str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    if speed <= 0:
        raise ValueError("speed must be > 0")
    start_time = clock()
    for msg in messages:
        delay = msg["delay_s"] / speed
        if delay > 0:
            sleep(delay)
        stamp_now(
            msg["payload"], start_time=start_time, clock=clock,
            capture_base=msg.get("capture_base"),
        )
        publish(msg["topic"], json.dumps(msg["payload"]))


def _inject_activity_token(db_path: str | Path, la_send: dict[str, Any]) -> None:
    """After a dry-run LA start, inject a per-activity token so subsequent
    update/end pushes have somewhere to go."""
    from marcellus import db as _db
    conn = _db.open_sidecar(db_path)
    try:
        rows = conn.execute(
            "SELECT activity_id FROM push_activities WHERE ended_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE push_activities SET token = ? WHERE activity_id = ?",
                (f"fake-activity-token-{row['activity_id']}", row["activity_id"]),
            )
        conn.commit()
    finally:
        conn.close()


def print_decisions(decisions: list[dict[str, Any]]) -> None:
    for d in decisions:
        parts = [f"step {d['step']}: {d['topic'].split('/')[-1]}/{d['type']}"]
        parts.append(f"mutation={d['mutation']}")
        if d.get("level"):
            parts.append(f"level={d['level']}")
        if "sounded" in d:
            parts.append(f"sounded={'yes' if d['sounded'] else 'no'}")
        if d.get("sound_name"):
            parts.append(f"sound={d['sound_name']}")
        if d.get("interruption_level"):
            parts.append(f"interruption={d['interruption_level']}")
        if d.get("la_action"):
            la_part = f"LA={d['la_action']}"
            if d.get("la_token_type"):
                la_part += f" ({d['la_token_type']})"
            parts.append(la_part)
        if d.get("thread_id"):
            parts.append(f"thread-id={d['thread_id']}")
        if d.get("category"):
            parts.append(f"category={d['category']}")
        print("  ".join(parts))


# ---------------------------------------------------------------------------
# Dry-run: in-process card pipeline with decision logging
# ---------------------------------------------------------------------------

async def dry_run_scenario(
    messages: list[dict[str, Any]],
    *,
    speed: float = 1.0,
    camera: str = "doorbell",
) -> list[dict[str, Any]]:
    """Run the scenario through the real card pipeline in-process with
    LogTransport, returning a decision log for each step."""
    import tempfile

    from marcellus import db
    from marcellus.config import PushSection
    from marcellus.push import store
    from marcellus.push.engine import PushEngine
    from marcellus.push.transport import LogTransport

    decisions: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sidecar.db"
        conn = db.open_sidecar(db_path)
        store.upsert_device(
            conn, apns_token="replay-device", bundle_id="com.pondhouse.Elsinore",
            environment="sandbox", cameras=[], min_severity="detection",
            push_to_start_token="pts-replay",
        )
        conn.commit()
        conn.close()

        transport = LogTransport()
        engine = PushEngine(db_path=str(db_path), transport=transport, server_id="replay")
        config = PushSection(delivery_enabled=True)
        engine.push_config = config

        start_time = time.time()
        for i, msg in enumerate(messages):
            delay = msg["delay_s"] / speed
            if delay > 0:
                await asyncio.sleep(delay)
            stamp_now(
                msg["payload"], start_time=start_time, clock=time.time,
                capture_base=msg.get("capture_base"),
            )

            topic = msg["topic"]
            payload = msg["payload"]
            sent_before = len(transport.sent)

            if topic == "frigate/events":
                await engine.handle_object_payload(payload)
            else:
                await engine.handle_review_payload(payload)

            # LogTransport.sent is list[dict[str, object]]; everything below
            # reads nested JSON-ish structure, so widen once here.
            new_sends = cast("list[dict[str, Any]]", transport.sent[sent_before:])
            card_sends = [s for s in new_sends if "payload" in s and not s.get("live_activity")]
            la_sends = [s for s in new_sends if s.get("live_activity")]

            step_decision: dict[str, Any] = {
                "step": i + 1,
                "topic": topic,
                "type": payload["type"],
            }

            if card_sends:
                p = card_sends[0]["payload"]
                step_decision["mutation"] = p.get("mutation", "")
                step_decision["level"] = p.get("level", "")
                step_decision["sounded"] = "sound" in p.get("aps", {})
                step_decision["interruption_level"] = p.get("aps", {}).get("interruption-level", "")
                step_decision["thread_id"] = p.get("aps", {}).get("thread-id", "")
                step_decision["category"] = p.get("aps", {}).get("category", "")
                if p.get("aps", {}).get("sound"):
                    step_decision["sound_name"] = p["aps"]["sound"]
                if la_sends and step_decision["interruption_level"] == "passive":
                    # la_first (and la_only) demote the card push to passive
                    # while an LA covers the device -- it still delivers (the
                    # NSE must still run to pre-warm snapshots), just without
                    # sound/banner, so flag it rather than reading as a full
                    # alert.
                    step_decision["card"] = "demoted (LA covers)"
            elif la_sends:
                # The card push genuinely didn't send this step (e.g. a
                # quiet-peak resolve, or a resolve held for the LA's
                # dismissal window) -- read mutation/level from the LA's
                # content-state instead of reporting a misleading
                # "(no push)".
                state = la_sends[0].get("payload", {}).get("aps", {}).get("content-state", {})
                step_decision["mutation"] = state.get("mutation", "")
                step_decision["level"] = state.get("level", "")
                step_decision["card"] = "no card send (LA covers)"
            else:
                step_decision["mutation"] = "(no push)"

            if la_sends:
                la_send = la_sends[0]
                step_decision["la_action"] = la_send["event"]
                # Where did the sound go? Under la_first the card is often
                # demoted and the LA start/escalation alert carries it — the
                # decision log must show that, or a silent card reads as a
                # dropped sound.
                la_alert = la_send.get("payload", {}).get("aps", {}).get("alert") or {}
                if la_alert.get("sound"):
                    step_decision["la_sound_name"] = la_alert["sound"]
                tok = la_send.get("token", "")
                step_decision["la_token_type"] = (
                    "push-to-start" if tok.startswith("pts") else "per-activity"
                )
                # Simulate the app uploading the per-activity token after
                # a successful start — without this, update/end pushes can
                # never be sent in dry-run mode.
                if la_send["event"] == "start":
                    _inject_activity_token(db_path, la_send)
            elif topic == "frigate/reviews":
                step_decision["la_action"] = "(none)"

            decisions.append(step_decision)
    return decisions


# ---------------------------------------------------------------------------
# MQTT publisher (used by both CLI and web handler)
# ---------------------------------------------------------------------------

class MqttPublisher:
    def __init__(
        self, *, host: str, port: int, client_id: str,
        username: str | None, password: str | None,
    ) -> None:
        import paho.mqtt.client as mqtt_client
        self._client = mqtt_client.Client(
            client_id=client_id,
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]  # paho re-exports it without __all__
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.connect(host, port)
        self._client.loop_start()

    def __call__(self, topic: str, payload_json: str) -> None:
        self._client.publish(topic, payload_json)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


# ---------------------------------------------------------------------------
# Web-driven run state
# ---------------------------------------------------------------------------

@dataclass
class ReplayRun:
    run_id: str
    scenarios: list[str]
    state: str = "queued"
    decisions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    messages_sent: int = 0
    messages_total: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenarios": self.scenarios,
            "state": self.state,
            "decisions": self.decisions,
            "error": self.error,
            "messages_sent": self.messages_sent,
            "messages_total": self.messages_total,
            "dry_run": self.dry_run,
        }


_current_run: ReplayRun | None = None
_run_task: asyncio.Task[None] | None = None
_run_lock = asyncio.Lock()


def get_current_run() -> ReplayRun | None:
    return _current_run


async def start_run(
    scenario_names: list[str],
    *,
    speed: float = 1.0,
    dry_run: bool = False,
    push_settings: PushSection | None = None,
    stagger: float = 8.0,
    wait: bool = True,
) -> ReplayRun:
    """Start a replay run.

    With ``wait=True`` (the historical behavior, kept for tests) this blocks
    until the run completes. With ``wait=False`` the run executes as a
    background task and the caller polls ``get_current_run()`` — a 1x
    scenario means minutes of wall clock, and holding an HTTP request open
    that long is what made the replay page feel hung.
    """
    global _current_run, _run_task

    if _run_lock.locked():
        raise RuntimeError("a replay run is already in progress")

    run_id = uuid.uuid4().hex[:8]
    run = ReplayRun(
        run_id=run_id,
        scenarios=scenario_names,
        dry_run=dry_run,
    )
    _current_run = run
    coro = _execute_run(run, speed=speed, dry_run=dry_run,
                        push_settings=push_settings, stagger=stagger)
    if wait:
        await coro
    else:
        # Keep a reference so the task isn't garbage-collected mid-run.
        _run_task = asyncio.create_task(coro)
        # Let it enter _run_lock before we return, so an immediate second
        # POST sees "already in progress" instead of racing.
        await asyncio.sleep(0)
    return run


async def _execute_run(
    run: ReplayRun,
    *,
    speed: float,
    dry_run: bool,
    push_settings: PushSection | None,
    stagger: float,
) -> None:
    async with _run_lock:
        try:
            run.state = "running"
            all_messages: list[tuple[str, list[dict[str, Any]]]] = []
            for name in run.scenarios:
                scenario = load_scenario(resolve_scenario_path(name))
                msgs = build_messages(scenario, run_id=run.run_id)
                all_messages.append((name, msgs))
                run.messages_total += len(msgs)

            if dry_run:
                for i, (_name, msgs) in enumerate(all_messages):
                    if i > 0:
                        await asyncio.sleep(stagger / speed)
                    decisions = await dry_run_scenario(msgs, speed=speed)
                    run.decisions.extend(decisions)
                    run.messages_sent += len(msgs)
            else:
                if push_settings is None:
                    raise RuntimeError("push_settings required for live run")
                publisher = MqttPublisher(
                    host=push_settings.mqtt_host,
                    port=push_settings.mqtt_port,
                    client_id=f"marcellus-replay-{run.run_id}",
                    username=push_settings.mqtt_username,
                    password=push_settings.mqtt_password,
                )
                try:
                    for i, (_name, msgs) in enumerate(all_messages):
                        if i > 0:
                            await asyncio.sleep(stagger / speed)
                        start_time = time.time()
                        for msg in msgs:
                            delay = msg["delay_s"] / speed
                            if delay > 0:
                                await asyncio.sleep(delay)
                            stamp_now(
                                msg["payload"], start_time=start_time, clock=time.time,
                                capture_base=msg.get("capture_base"),
                            )
                            publisher(msg["topic"], json.dumps(msg["payload"]))
                            run.messages_sent += 1
                finally:
                    publisher.close()

            run.state = "done"
        except Exception as exc:
            run.state = "failed"
            run.error = str(exc)
            logger.exception("replay: run %s failed", run.run_id)
