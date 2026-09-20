"""Freezing a window of the MQTT flight recorder into a checked-in scenario.

The distinction under test throughout: a `steps` scenario is a template that
`build_messages` synthesises a payload from, while a `capture` scenario IS the
payload -- so the export must preserve fields nothing here names, and the two
rewrites it does perform (ids into the `replay-` namespace, timestamps onto the
run clock) have to be exact or a replay resumes the original card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus.push import replay


def _review(ts: float, rid: str, *, detections: list[str], zones: list[str],
            camera: str = "stairway-wide", severity: str = "alert") -> dict:
    body = {
        "id": rid, "camera": camera, "severity": severity,
        "start_time": ts, "end_time": None,
        "thumb_path": f"/media/frigate/clips/review/thumb-{camera}-{rid}.webp",
        "data": {"objects": ["person"], "detections": detections, "zones": zones,
                 "audio": [], "sub_labels": [], "thumb_time": ts + 0.5},
    }
    return {"ts": ts, "topic": "frigate/reviews",
            "payload": {"type": "new", "before": body, "after": body}}


def _event(ts: float, eid: str, *, camera: str = "stairway-wide",
           kind: str = "update") -> dict:
    body = {
        "id": eid, "camera": camera, "label": "person",
        "start_time": ts, "end_time": ts + 9 if kind == "end" else None,
        "frame_time": ts, "score": 0.87, "box": [100, 200, 340, 700],
        "stationary": False, "current_zones": ["charger"], "entered_zones": ["charger"],
        "path_data": [[[0.4, 0.6], ts - 1.0], [[0.41, 0.61], ts]],
    }
    return {"ts": ts, "topic": "frigate/events",
            "payload": {"type": kind, "before": body, "after": body}}


@pytest.fixture
def rows() -> list[dict]:
    """One review naming one event, plus an unrelated event from another camera."""
    return [
        _review(1_787_000_000.0, "rev-1", detections=["evt-1"], zones=["parking_area", "charger"]),
        _event(1_787_000_002.5, "evt-1"),
        _event(1_787_000_003.0, "noise-1", camera="gate-face"),
        _event(1_787_000_011.0, "evt-1", kind="end"),
    ]


# --- pruning -------------------------------------------------------------

def test_prune_keeps_the_story_and_reports_what_it_dropped(rows: list[dict]) -> None:
    kept, dropped = replay.prune_to_story(rows)
    ids = [m["payload"]["after"]["id"] for m in kept]
    assert ids == ["rev-1", "evt-1", "evt-1"]
    assert dropped == {"gate-face": 1}


def test_prune_is_a_no_op_when_no_review_names_a_detection() -> None:
    """No evidence to act on -- dropping everything would be worse than keeping it."""
    only_events = [_event(1_787_000_000.0, "evt-1")]
    kept, dropped = replay.prune_to_story(only_events)
    assert kept == only_events and dropped == {}


# --- export --------------------------------------------------------------

def test_export_writes_a_capture_scenario(rows: list[dict], tmp_path: Path) -> None:
    path = replay.export_capture(rows, name="charger-visit", out_dir=tmp_path)
    assert path.name == "cap-charger-visit.json"
    doc = json.loads(path.read_text())
    assert doc["kind"] == "capture"
    assert doc["id"] == "cap-charger-visit"
    assert doc["camera"] == "stairway-wide"
    assert [m["topic"] for m in doc["messages"]] == [
        "frigate/reviews", "frigate/events", "frigate/events",
    ]
    assert doc["source"]["dropped_unreferenced"] == {"gate-face": 1}
    assert doc["source"]["zones"] == ["parking_area", "charger"]
    assert "charger" in doc["description"] and "real wire" in doc["description"]


def test_export_preserves_fields_the_steps_schema_has_no_room_for(
    rows: list[dict], tmp_path: Path
) -> None:
    """The whole point: score/box/path_data survive, and a template cannot carry them."""
    path = replay.export_capture(rows, name="fidelity", out_dir=tmp_path)
    event = json.loads(path.read_text())["messages"][1]["payload"]["after"]
    assert event["score"] == 0.87
    assert event["box"] == [100, 200, 340, 700]
    assert event["path_data"][0][0] == [0.4, 0.6]


def test_export_refuses_to_clobber_without_overwrite(rows: list[dict], tmp_path: Path) -> None:
    replay.export_capture(rows, name="dup", out_dir=tmp_path)
    with pytest.raises(FileExistsError):
        replay.export_capture(rows, name="dup", out_dir=tmp_path)
    assert replay.export_capture(rows, name="dup", out_dir=tmp_path, overwrite=True).exists()


def test_export_rejects_an_empty_window(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        replay.export_capture([], name="nothing", out_dir=tmp_path)


@pytest.mark.parametrize("name,expected", [
    ("../../etc/passwd", "cap-etc-passwd.json"),
    ("...hidden", "cap-hidden.json"),
    ("card-keeps-its-prefix", "card-keeps-its-prefix.json"),
    ("already.json", "cap-already.json"),
])
def test_export_name_cannot_escape_the_scenarios_dir(
    rows: list[dict], tmp_path: Path, name: str, expected: str
) -> None:
    path = replay.export_capture(rows, name=name, out_dir=tmp_path)
    assert path.name == expected
    assert path.parent == tmp_path


# --- loading -------------------------------------------------------------

def test_load_scenario_accepts_a_capture(rows: list[dict], tmp_path: Path) -> None:
    path = replay.export_capture(rows, name="ok", out_dir=tmp_path)
    assert replay.load_scenario(path)["kind"] == "capture"


@pytest.mark.parametrize("doc,match", [
    ({"kind": "capture", "messages": []}, "non-empty"),
    ({"kind": "capture", "messages": [{"topic": "nope", "payload": {}}]}, "topic"),
    ({"kind": "capture", "messages": [{"topic": "frigate/events", "payload": 3}]}, "payload"),
])
def test_load_scenario_rejects_a_malformed_capture(
    tmp_path: Path, doc: dict, match: str
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match=match):
        replay.load_scenario(path)


def test_list_scenarios_sees_names_without_the_card_prefix(
    rows: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replay, "SCENARIOS_DIR", tmp_path)
    replay.export_capture(rows, name="from-the-recorder", out_dir=tmp_path)
    listed = {s["name"]: s["kind"] for s in replay.list_scenarios()}
    assert listed == {"cap-from-the-recorder": "capture"}


# --- building ------------------------------------------------------------

def test_build_renamespaces_every_id_consistently(rows: list[dict], tmp_path: Path) -> None:
    """A review and its events cross-reference; rewriting one and not the other
    would sever the link the card pipeline enriches through."""
    scenario = replay.load_scenario(replay.export_capture(rows, name="ids", out_dir=tmp_path))
    msgs = replay.build_messages(scenario, run_id="run1")

    review, event = msgs[0]["payload"]["after"], msgs[1]["payload"]["after"]
    assert review["id"].startswith(replay.REPLAY_ID_PREFIX)
    assert event["id"].startswith(replay.REPLAY_ID_PREFIX)
    assert review["data"]["detections"] == [event["id"]]
    assert msgs[2]["payload"]["after"]["id"] == event["id"]  # the end event too
    assert review["id"] != event["id"]


def test_build_leaves_ids_embedded_in_asset_paths(rows: list[dict], tmp_path: Path) -> None:
    """Whole-string replacement only. `thumb_path` still names the original
    review because it points at a real file nothing in the pipeline reads."""
    scenario = replay.load_scenario(replay.export_capture(rows, name="thumb", out_dir=tmp_path))
    msgs = replay.build_messages(scenario, run_id="run1")
    assert "rev-1" in msgs[0]["payload"]["after"]["thumb_path"]


def test_build_derives_delay_from_the_real_gaps(rows: list[dict], tmp_path: Path) -> None:
    scenario = replay.load_scenario(replay.export_capture(rows, name="gaps", out_dir=tmp_path))
    msgs = replay.build_messages(scenario, run_id="run1")
    assert [round(m["delay_s"], 1) for m in msgs] == [0.0, 2.5, 8.5]
    assert all(m["capture_base"] == 1_787_000_000.0 for m in msgs)


def test_build_can_retarget_the_camera(rows: list[dict], tmp_path: Path) -> None:
    scenario = replay.load_scenario(replay.export_capture(rows, name="cam", out_dir=tmp_path))
    msgs = replay.build_messages(scenario, camera="alley-wide", run_id="run1")
    assert msgs[0]["payload"]["after"]["camera"] == "alley-wide"


def test_build_refuses_a_label_override(rows: list[dict], tmp_path: Path) -> None:
    scenario = replay.load_scenario(replay.export_capture(rows, name="lbl", out_dir=tmp_path))
    with pytest.raises(ValueError, match="recorded wire"):
        replay.build_messages(scenario, label="dog", run_id="run1")


# --- time shifting -------------------------------------------------------

def test_stamp_now_shifts_a_capture_instead_of_assigning(rows: list[dict], tmp_path: Path) -> None:
    """Assignment is right for a template and destructive for a capture: the
    offsets between a payload's dozen timestamps ARE the fixture."""
    scenario = replay.load_scenario(replay.export_capture(rows, name="shift", out_dir=tmp_path))
    msgs = replay.build_messages(scenario, run_id="run1")
    event = msgs[1]["payload"]["after"]
    original_gap = event["frame_time"] - event["path_data"][0][1]

    now = 1_800_000_000.0
    for msg in msgs:
        replay.stamp_now(msg["payload"], start_time=now, clock=lambda: now,
                         capture_base=msg["capture_base"])

    assert msgs[0]["payload"]["after"]["start_time"] == now          # story starts now
    assert event["frame_time"] == pytest.approx(now + 2.5)           # offsets intact
    assert event["frame_time"] - event["path_data"][0][1] == pytest.approx(original_gap)
    assert event["score"] == 0.87                                    # not a timestamp
    assert event["box"] == [100, 200, 340, 700]
    assert event["stationary"] is False                              # bool is not an int here


def test_stamp_now_still_assigns_for_a_template_scenario() -> None:
    payload = {"type": "end", "after": {"start_time": None, "end_time": None}}
    replay.stamp_now(payload, start_time=42.0, clock=lambda: 99.0)
    assert payload["after"] == {"start_time": 42.0, "end_time": 99.0}


# --- identities ----------------------------------------------------------

def _named(ts: float) -> dict:
    row = _review(ts, "rev-x", detections=["evt-x"], zones=["front_door"])
    row["payload"]["after"]["data"]["sub_labels"] = ["Sam"]
    return row


def test_export_refuses_a_window_carrying_recognized_names(tmp_path: Path) -> None:
    """The scenario set is checked into a public repo and face recognition puts
    a real name in sub_label -- that must not be a silent default."""
    with pytest.raises(ValueError, match="Sam"):
        replay.export_capture([_named(1_787_000_000.0)], name="named", out_dir=tmp_path)


def test_export_allows_names_when_asked(tmp_path: Path) -> None:
    path = replay.export_capture(
        [_named(1_787_000_000.0)], name="named", out_dir=tmp_path, allow_identities=True
    )
    assert path.exists()


def test_identities_in_finds_both_shapes() -> None:
    event = _event(1_787_000_000.0, "evt-1")
    event["payload"]["after"]["sub_label"] = "Alex"
    assert sorted(replay.identities_in([_named(1.0), event])) == ["Alex", "Sam"]


def test_identities_in_ignores_blanks(rows: list[dict]) -> None:
    assert replay.identities_in(rows) == []
