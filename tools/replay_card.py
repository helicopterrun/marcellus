#!/usr/bin/env python3
"""Replay canned Frigate sequences through the card pipeline.

Publishes timed `frigate/reviews` and `frigate/events` messages to MQTT so
the live sidecar's card pipeline (Phase 5) runs end to end: per-device
filtering, snooze, rate cap, quiet hours, payload construction, relay/APNs,
and the app's NSE all exercise against a registered device.

Scenarios ship as package data in
`src/marcellus/push/replay_scenarios/card-*.json` so an installed sidecar
has them too (the web UI at /replay reads the same set). Each step specifies
its topic (`frigate/reviews` or `frigate/events`) so the tool drives both
the review-create/enrich path and the object-end resolve path — the full
card lifecycle.

Decision logging: in `--dry-run` mode, the tool instantiates the real card
pipeline in-process (with LogTransport) and prints what the sidecar decided
at each step: card mutation, level, sounded or not, LA action, per-device
filtering outcome. In live mode (publishing to MQTT), the sidecar's own
structured log lines (`push: card mutation=...`) are the decision trace —
use `journalctl -u marcellus -f` alongside this tool.

Usage:
    python tools/replay_card.py --scenario card-notify-resolve --dry-run
    python tools/replay_card.py --scenario card-la-package --camera doorbell
    python tools/replay_card.py --scenario card-escalate-urgent --speed 4
    python tools/replay_card.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Same as tools/replay_capture.py: run the checkout, not whatever was last
# pip-installed. Without this the two tools disagree about which scenarios
# exist -- an export lands in src/ and this one cannot see it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from marcellus.push.replay import (  # noqa: E402
    REPLAY_ID_PREFIX,
    MqttPublisher,
    build_messages,
    dry_run_scenario,
    list_scenarios,
    load_scenario,
    print_decisions,
    resolve_scenario_path,
    run_scenario,
    stamp_now,
)

# Re-export so existing test imports keep working.
__all__ = [
    "REPLAY_ID_PREFIX",
    "build_messages",
    "dry_run_scenario",
    "list_scenarios",
    "load_scenario",
    "print_decisions",
    "resolve_scenario_path",
    "run_scenario",
    "stamp_now",
]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay canned Frigate sequences through the card pipeline."
    )
    parser.add_argument(
        "--scenario",
        help="Scenario name (looked up in the packaged set, "
        "marcellus/push/replay_scenarios/<name>.json) "
        "or a path to a scenario JSON file.",
    )
    parser.add_argument("--camera", help="Override the scenario's default camera.")
    parser.add_argument("--label", help="Override the scenario's default object label.")
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Time-compression multiplier for inter-message delays (default 1.0 = real time).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the card pipeline in-process (LogTransport) and print decisions. "
        "No MQTT broker needed.",
    )
    parser.add_argument("--list", action="store_true", help="List available card scenarios.")
    parser.add_argument("--mqtt-host", help="Override push.mqtt_host from config.")
    parser.add_argument("--mqtt-port", type=int, help="Override push.mqtt_port from config.")
    parser.add_argument("--config", help="Path to sidecar.yml.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.list:
        scenarios = list_scenarios()
        if not scenarios:
            print("no scenarios found")
            return 0
        for s in scenarios:
            print(f"  {s['name']}")
            if s["description"]:
                print(f"    {s['description'][:100]}")
        return 0

    if not args.scenario:
        print("error: --scenario is required (use --list to see available)", file=sys.stderr)
        return 1

    try:
        scenario = load_scenario(resolve_scenario_path(args.scenario))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    camera = args.camera or scenario.get("camera") or "doorbell"
    messages = build_messages(scenario, camera=camera, label=args.label)

    if args.dry_run:
        decisions = asyncio.run(
            dry_run_scenario(messages, speed=args.speed, camera=camera)
        )
        print_decisions(decisions)
        return 0

    from marcellus.config import load_settings

    push_settings = load_settings(args.config).push
    publisher = MqttPublisher(
        host=args.mqtt_host or push_settings.mqtt_host,
        port=args.mqtt_port or push_settings.mqtt_port,
        client_id=f"marcellus-replay-{uuid.uuid4().hex[:8]}",
        username=push_settings.mqtt_username,
        password=push_settings.mqtt_password,
    )
    try:
        run_scenario(messages, speed=args.speed, publish=publisher)
        print(f"published {len(messages)} messages to MQTT")
        print("watch decisions: journalctl -u marcellus -f --grep 'push: card'")
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
