"""Tests for tools/replay_card.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from marcellus.push import replay as replay_core

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

_spec = importlib.util.spec_from_file_location(
    "replay_card", TOOLS_DIR / "replay_card.py"
)
assert _spec is not None and _spec.loader is not None
replay_card = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_card)


def test_list_scenarios_finds_card_scenarios():
    scenarios = replay_card.list_scenarios()
    names = [s["name"] for s in scenarios]
    assert "card-notify-resolve" in names
    assert "card-la-package" in names
    assert "card-escalate-urgent" in names
    assert "card-la-person-doors" in names


def test_resolve_scenario_path_by_name():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    assert path.exists()


def test_resolve_scenario_path_unknown_raises():
    with pytest.raises(FileNotFoundError):
        replay_card.resolve_scenario_path("does-not-exist")


def test_load_scenario_rejects_missing_steps(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"camera": "doorbell"}))
    with pytest.raises(ValueError, match="steps"):
        replay_card.load_scenario(bad)


def test_load_scenario_rejects_bad_topic(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"steps": [{"type": "new", "topic": "frigate/bogus"}]}))
    with pytest.raises(ValueError, match="frigate/reviews or frigate/events"):
        replay_card.load_scenario(bad)


def test_build_messages_notify_resolve_lifecycle():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="abc123")

    assert len(messages) == 3
    assert [m["topic"] for m in messages] == [
        "frigate/reviews", "frigate/reviews", "frigate/events",
    ]
    assert [m["payload"]["type"] for m in messages] == ["new", "update", "end"]

    # Review messages have review_id, event messages have track_id
    assert messages[0]["payload"]["after"]["id"].startswith(replay_card.REPLAY_ID_PREFIX)
    assert messages[2]["payload"]["after"]["id"].startswith(replay_card.REPLAY_ID_PREFIX)
    # Event message id is the track_id (different from review_id)
    assert messages[2]["payload"]["after"]["id"] != messages[0]["payload"]["after"]["id"]


def test_build_messages_event_format_differs_from_review():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="xyz")

    # Review message has data.objects, data.detections
    review_after = messages[0]["payload"]["after"]
    assert "data" in review_after
    assert "objects" in review_after["data"]

    # Event message has label, current_zones (no data wrapper)
    event_after = messages[2]["payload"]["after"]
    assert "label" in event_after
    assert "current_zones" in event_after
    assert "data" not in event_after


def test_build_messages_respects_camera_override():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, camera="patio", run_id="x")
    for m in messages:
        assert m["payload"]["after"]["camera"] == "patio"


def test_run_scenario_publishes_in_order_with_scaled_delays():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="abc")

    sent: list[tuple[str, dict]] = []
    slept: list[float] = []
    clock_n = {"n": 0}

    def fake_clock() -> float:
        clock_n["n"] += 1
        return 1000.0 + clock_n["n"]

    replay_card.run_scenario(
        messages, speed=2.0,
        publish=lambda t, p: sent.append((t, json.loads(p))),
        sleep=slept.append,
        clock=fake_clock,
    )
    assert len(sent) == 3
    assert [t for t, _ in sent] == [
        "frigate/reviews", "frigate/reviews", "frigate/events",
    ]
    # delay_s [0, 5, 8] / speed 2.0 = [0, 2.5, 4.0]; leading 0 not slept
    assert slept == [2.5, 4.0]


def test_run_scenario_stamps_start_time():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="abc")

    sent: list[dict] = []
    replay_card.run_scenario(
        messages, speed=100.0,
        publish=lambda t, p: sent.append(json.loads(p)),
        sleep=lambda _: None,
        clock=lambda: 5000.0,
    )
    assert all(p["after"]["start_time"] == 5000.0 for p in sent)
    assert sent[-1]["after"]["end_time"] == 5000.0


def test_run_scenario_rejects_non_positive_speed():
    with pytest.raises(ValueError, match="speed"):
        replay_card.run_scenario([], speed=0, publish=lambda *_: None)


@pytest.mark.asyncio
async def test_dry_run_scenario_notify_resolve():
    path = replay_card.resolve_scenario_path("card-notify-resolve")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="drytest")
    decisions = await replay_card.dry_run_scenario(messages, speed=100.0)

    assert len(decisions) == 3
    assert decisions[0]["mutation"] == "create"
    assert decisions[0]["level"] == "notify"
    # LA start accepted → the card push still delivers, demoted to passive
    # (la_first); the NSE still runs on it, but the LA carries the alert.
    assert decisions[0]["card"] == "demoted (LA covers)"
    assert decisions[0]["interruption_level"] == "passive"
    assert not decisions[0]["sounded"]
    assert decisions[0]["la_action"] == "start"

    assert decisions[1]["mutation"] == "enrich"
    assert decisions[1]["card"] == "demoted (LA covers)"

    assert decisions[2]["mutation"] == "resolve"
    assert decisions[2]["level"] == "notify"


@pytest.mark.asyncio
async def test_dry_run_scenario_escalate_urgent():
    from marcellus.push import policy_settings
    policy_settings.apply_settings(policy_settings.default_settings() | {"mute_sounds": False})

    path = replay_card.resolve_scenario_path("card-escalate-urgent")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="drytest")
    decisions = await replay_card.dry_run_scenario(messages, speed=100.0, camera="patio")

    mutations = [d["mutation"] for d in decisions]
    # The quiet-level create still sends a card push -- demoted to passive
    # once its LA covers the device, but delivered (2026-08-29: la_first
    # demotion delivers passive instead of suppressing).
    assert "create" in mutations
    assert "escalate" in mutations

    escalate = next(d for d in decisions if d["mutation"] == "escalate")
    assert escalate["level"] == "urgent"
    # Merged ladder (2026-08-16): the quiet create's glance outcome already
    # started the activity, so the escalation UPDATES it (the dry-run
    # simulates the app's token upload, so the update covers and the card
    # push is demoted to passive); the urgent sound rides the update alert.
    create = decisions[0]
    assert create.get("la_action") == "start"
    assert escalate["card"] == "demoted (LA covers)"
    assert not escalate["sounded"]
    assert escalate["la_action"] == "update"
    assert escalate["la_sound_name"] == "urgent.caf"


@pytest.mark.asyncio
async def test_dry_run_scenario_la_package():
    path = replay_card.resolve_scenario_path("card-la-package")
    scenario = replay_card.load_scenario(path)
    messages = replay_card.build_messages(scenario, run_id="drytest")
    decisions = await replay_card.dry_run_scenario(messages, speed=100.0)

    assert decisions[0]["la_action"] == "start"
    assert decisions[0]["la_token_type"] == "push-to-start"


def test_cli_list_prints_scenarios(capsys):
    exit_code = replay_card.main(["--list"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "card-notify-resolve" in out
    assert "card-la-package" in out


def test_cli_dry_run_prints_decisions(capsys):
    exit_code = replay_card.main([
        "--scenario", "card-notify-resolve", "--dry-run", "--speed", "100",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "create" in out
    assert "enrich" in out
    assert "resolve" in out


def test_cli_no_scenario_errors(capsys):
    exit_code = replay_card.main([])
    assert exit_code == 1
    assert "required" in capsys.readouterr().err


def test_cli_unknown_scenario_errors(capsys):
    exit_code = replay_card.main(["--scenario", "nope", "--dry-run"])
    assert exit_code == 1
    assert "nope" in capsys.readouterr().err


def test_scenarios_ship_inside_the_package_not_the_repo():
    """Regression: a repo-relative SCENARIOS_DIR is invisible to an installed sidecar.

    The wheel packages only `src/marcellus`, so a path resolved out of the
    repo's `tools/` lands beside site-packages and globs nothing -- /replay renders
    an empty picker and every run 400s, with no error logged anywhere. Assert
    containment rather than a literal path, so a future rename stays free.
    """
    package_root = Path(replay_core.__file__).resolve().parents[1]
    scenarios_dir = replay_core.SCENARIOS_DIR.resolve()
    assert scenarios_dir.is_relative_to(package_root), scenarios_dir
    assert sorted(p.name for p in scenarios_dir.glob("card-*.json"))
