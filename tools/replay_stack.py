#!/usr/bin/env python3
"""Run two or more replay_card.py scenarios concurrently, staggered.

Live Activity stacking only shows up on-device when a second scenario's
`start` event fires while an earlier scenario's activity is still live on
the lock screen. Doing that by hand means two terminals and a stopwatch.
This tool launches each scenario as a `replay_card.py` subprocess (it does
not re-implement scenario logic) with a staggered start between launches,
and interleaves their stdout with a `[scenario-name] HH:MM:SS.mmm` prefix
so the timeline can be correlated against device logs.

Usage:
    MARCELLUS_PUSH__MQTT_PASSWORD=... python3 tools/replay_stack.py \\
        --scenarios card-la-person-doors card-la-package \\
        --stagger 8 --config config/sidecar.yml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import replay_card  # noqa: E402

REPLAY_CARD_PATH = TOOLS_DIR / "replay_card.py"


def build_launch_plan(scenarios: list[str], stagger: float) -> list[dict[str, Any]]:
    """Expand scenario names into an ordered launch plan.

    Each entry: `{"name": str, "delay_s": float}` where `delay_s` is the
    offset from the start of the run (0 for the first scenario).
    """
    return [{"name": name, "delay_s": i * stagger} for i, name in enumerate(scenarios)]


def validate_scenario_names(names: list[str]) -> None:
    known = {s["name"] for s in replay_card.list_scenarios()}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(
            f"unknown scenario(s): {', '.join(unknown)}. "
            f"valid scenarios: {', '.join(sorted(known))}"
        )


def format_timestamp(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch)
    return f"{dt:%H:%M:%S}.{dt.microsecond // 1000:03d}"


def build_child_args(name: str, *, speed: float, config: str | None) -> list[str]:
    args = ["--scenario", name, "--speed", str(speed)]
    if config:
        args += ["--config", config]
    return args


async def _spawn_replay_card(name: str, args: list[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable, str(REPLAY_CARD_PATH), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _stream_output(
    name: str, stream: Any, *, now: Callable[[], float] = time.time,
) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        text = line.decode(errors="replace").rstrip("\n")
        print(f"[{name}] {format_timestamp(now())} {text}", flush=True)


async def _run_child(
    name: str, proc: Any, *, now: Callable[[], float],
) -> int:
    await _stream_output(name, proc.stdout, now=now)
    return await proc.wait()


async def run_stack(
    scenarios: list[str],
    *,
    stagger: float,
    speed: float,
    config: str | None,
    dry_run: bool = False,
    spawn: Callable[[str, list[str]], Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    plan = build_launch_plan(scenarios, stagger)

    if dry_run:
        for step in plan:
            child_args = build_child_args(step["name"], speed=speed, config=config)
            print(
                f"[{step['name']}] launch at +{step['delay_s']:.1f}s -> "
                f"replay_card.py {' '.join(child_args)}"
            )
        return 0

    spawn = spawn or _spawn_replay_card
    sleep = sleep or asyncio.sleep

    procs: list[tuple[str, Any]] = []
    for i, step in enumerate(plan):
        if i > 0:
            await sleep(stagger)
        child_args = build_child_args(step["name"], speed=speed, config=config)
        proc = await spawn(step["name"], child_args)
        procs.append((step["name"], proc))

    returncodes = await asyncio.gather(
        *(_run_child(name, proc, now=now) for name, proc in procs)
    )

    failed = [name for (name, _), rc in zip(procs, returncodes, strict=False) if rc != 0]
    if failed:
        print(f"error: scenario(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run two or more replay_card.py scenarios concurrently with a "
            "staggered start, for Live Activity stacking tests."
        ),
        epilog=(
            "Canonical stacking test:\n"
            "  MARCELLUS_PUSH__MQTT_PASSWORD=... python3 tools/replay_stack.py "
            "--scenarios card-la-person-doors card-la-package --stagger 8 "
            "--config config/sidecar.yml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--scenarios", nargs="+", metavar="NAME", required=True,
        help="Scenario names to launch, in launch order (2 or more).",
    )
    parser.add_argument(
        "--stagger", type=float, default=8.0,
        help="Seconds to wait between launching each scenario (default 8).",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Passed through to each replay_card.py child as --speed (default 1.0).",
    )
    parser.add_argument("--config", help="Path to sidecar.yml, passed through to each child.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the launch plan without spawning any children.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if len(args.scenarios) < 2:
        print("error: --scenarios needs 2 or more names for a stacking test", file=sys.stderr)
        return 1

    try:
        validate_scenario_names(args.scenarios)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return asyncio.run(run_stack(
        args.scenarios, stagger=args.stagger, speed=args.speed,
        config=args.config, dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
