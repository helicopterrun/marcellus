"""MQTT flight recorder: a rolling JSONL capture of every `frigate/reviews`
and `frigate/events` message the push pipeline consumes.

Why: replay scenarios were hand-written approximations, and the gap between
a canned scenario and a real walk kept hiding bugs (family gating, copy
echo, demotion — all found live, none by replay). With the true wire
captured, any real situation becomes an exact fixture:
`tools/replay_capture.py` republishes a time window with original relative
timing, every field verbatim.

Format: one JSON object per line — `{"ts": <epoch float>, "topic": str,
"payload": <raw message JSON>}`. Size-rotated: when the file exceeds
`max_bytes` it becomes `<path>.1` (previous `.1` dropped), so the recorder
holds roughly `2 * max_bytes` of history and can never fill a disk.

Called from paho's network thread — appends are lock-guarded and the file
handle is reopened after rotation. Failures are swallowed after one log
line: the recorder must never take down the pipeline it observes.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class MqttCapture:
    def __init__(self, path: str | Path, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._warned = False
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, topic: str, payload_bytes: bytes, *, now: float | None = None) -> None:
        try:
            payload = json.loads(payload_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return  # the pipeline drops these too; nothing worth replaying
        line = json.dumps(
            {"ts": now if now is not None else time.time(), "topic": topic, "payload": payload},
            separators=(",", ":"),
        )
        try:
            with self._lock:
                self._rotate_if_needed(len(line) + 1)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as exc:
            if not self._warned:
                self._warned = True
                logger.warning("push: mqtt capture disabled after write failure: %s", exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        rotated = self.path.with_name(self.path.name + ".1")
        os.replace(self.path, rotated)


def read_window(
    paths: list[Path],
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    camera: str | None = None,
) -> list[dict[str, Any]]:
    """Load capture lines across files (oldest rotation first), filtered to a
    time window and optionally one camera. Malformed lines are skipped —
    a torn final line from a crash must not poison the whole capture."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = row.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                if camera and _camera_of(row) != camera:
                    continue
                rows.append(row)
    rows.sort(key=lambda r: r["ts"])
    return rows


def _camera_of(row: dict[str, Any]) -> str | None:
    payload = row.get("payload") or {}
    # reviews: {"after": {"camera": ...}}; events: {"after": {"camera": ...}}
    after = payload.get("after") or payload.get("before") or {}
    camera = after.get("camera")
    return camera if isinstance(camera, str) else None
