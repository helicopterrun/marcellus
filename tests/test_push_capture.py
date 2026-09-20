"""The MQTT flight recorder: append/rotation mechanics and window reads."""
from __future__ import annotations

import json
from pathlib import Path

from marcellus.push.capture import MqttCapture, read_window


def _line(path: Path, index: int) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_append_writes_jsonl_and_skips_malformed(tmp_path: Path):
    cap = MqttCapture(tmp_path / "cap.jsonl")
    cap.append("frigate/reviews", b'{"type": "new", "after": {"camera": "garden"}}', now=100.0)
    cap.append("frigate/events", b"not json at all", now=101.0)  # dropped
    rows = _line(tmp_path / "cap.jsonl", 0)
    assert len(rows) == 1
    assert rows[0] == {
        "ts": 100.0, "topic": "frigate/reviews",
        "payload": {"type": "new", "after": {"camera": "garden"}},
    }


def test_rotation_keeps_one_sibling_and_never_grows_unbounded(tmp_path: Path):
    path = tmp_path / "cap.jsonl"
    cap = MqttCapture(path, max_bytes=200)
    for i in range(50):
        cap.append("frigate/events", json.dumps({"i": i, "pad": "x" * 40}).encode(), now=float(i))
    rotated = path.with_name(path.name + ".1")
    assert rotated.exists()
    assert path.stat().st_size <= 200 + 100  # one line of slack past the cap
    # Nothing beyond the pair.
    assert not path.with_name(path.name + ".2").exists()


def test_read_window_filters_time_and_camera_across_rotation(tmp_path: Path):
    path = tmp_path / "cap.jsonl"
    rotated = path.with_name(path.name + ".1")
    rotated.write_text(json.dumps(
        {"ts": 10.0, "topic": "frigate/events", "payload": {"after": {"camera": "garden"}}}
    ) + "\n")
    lines = [
        {"ts": 20.0, "topic": "frigate/reviews", "payload": {"after": {"camera": "garden"}}},
        {"ts": 30.0, "topic": "frigate/events", "payload": {"after": {"camera": "gate"}}},
        {"ts": 40.0, "topic": "frigate/events", "payload": {"after": {"camera": "garden"}}},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "torn-final-line{{{\n")

    rows = read_window([rotated, path], start_ts=15.0, end_ts=35.0)
    assert [r["ts"] for r in rows] == [20.0, 30.0]

    garden = read_window([rotated, path], camera="garden")
    assert [r["ts"] for r in garden] == [10.0, 20.0, 40.0]
