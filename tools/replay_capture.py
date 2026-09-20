#!/usr/bin/env python3
"""Replay a window of the MQTT flight recorder — a real situation, exactly
as it happened, every field verbatim, original relative timing (scalable).

List what's in the capture:
    python tools/replay_capture.py --list [--capture PATH]

Replay a window live (publishes to the broker; BOTH sidecar instances will
consume it — only the one holding the phone's registration pushes):
    python tools/replay_capture.py --from "2026-08-14 10:15" --to "2026-08-14 10:20" \
        [--camera garden] [--speed 4] [--capture PATH] \
        --mqtt-host 192.168.50.111 --mqtt-username frigate

MQTT password comes from MARCELLUS_PUSH__MQTT_PASSWORD. Timestamps are
local time ("YYYY-MM-DD HH:MM[:SS]") or raw epoch floats.

Replayed review/event ids collide with their originals in card stores —
replaying a story the sidecar has already seen resumes/duplicates that card
rather than starting a fresh one. Fine for a dev instance with fresh state;
on production prefer a dry look at the capture first.

Freeze a window into a permanent, checked-in scenario instead:
    python tools/replay_capture.py --from "2026-08-23 01:12" --to "2026-08-23 01:14" \
        --camera gate-face --export gate-face-sidewalk-pass

The recorder is size-rotated and holds hours, so a real situation is gone by
tomorrow; --export writes it into the packaged scenario set, where it is
named, diffable, and runnable from the same /replay picker as the templates.
Unlike a raw replay, an exported scenario has its ids moved into the `replay-`
namespace and its timestamps shifted to run time, so replaying it starts a
fresh card instead of resuming the original.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from marcellus.push import replay  # noqa: E402
from marcellus.push.capture import _camera_of, read_window  # noqa: E402


def parse_ts(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"unparseable time: {value!r} (use 'YYYY-MM-DD HH:MM' or epoch)")


def capture_paths(base: Path) -> list[Path]:
    return [base.with_name(base.name + ".1"), base]


def summarize(rows: list[dict]) -> None:
    if not rows:
        print("capture is empty (or nothing in the window)")
        return
    first, last = rows[0]["ts"], rows[-1]["ts"]
    print(f"{len(rows)} messages, "
          f"{dt.datetime.fromtimestamp(first):%Y-%m-%d %H:%M:%S} → "
          f"{dt.datetime.fromtimestamp(last):%H:%M:%S}")
    by_camera: Counter[str] = Counter()
    reviews = 0
    for row in rows:
        cam = _camera_of(row) or "?"
        by_camera[cam] += 1
        if row["topic"].endswith("reviews"):
            reviews += 1
    print(f"reviews: {reviews}, events: {len(rows) - reviews}")
    for cam, n in by_camera.most_common():
        print(f"  {cam}: {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", default="config/mqtt-capture.jsonl")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--camera")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-username")
    ap.add_argument(
        "--export", metavar="NAME",
        help="Freeze this window into the packaged scenario set as cap-<NAME>.json "
             "instead of replaying it.",
    )
    ap.add_argument("--description", help="Scenario description (auto-generated if omitted).")
    ap.add_argument("--out-dir", help="Write the scenario here instead of the packaged set.")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing scenario.")
    ap.add_argument(
        "--allow-identities", action="store_true",
        help="Export even though the window contains recognized names (sub_label). "
             "The scenario set is checked into a PUBLIC repo -- be sure.",
    )
    ap.add_argument(
        "--all-traffic", action="store_true",
        help="Export every message in the window, not just the reviews and the object "
             "events they reference. Much larger; use when the noise IS the point.",
    )
    args = ap.parse_args()

    rows = read_window(
        capture_paths(Path(args.capture)),
        start_ts=parse_ts(args.start) if args.start else None,
        end_ts=parse_ts(args.end) if args.end else None,
        camera=args.camera,
    )
    if args.export:
        if not args.start:
            raise SystemExit("--export needs a window: pass --from (and usually --to)")
        if not rows:
            raise SystemExit("nothing to export in that window")
        summarize(rows)
        path = replay.export_capture(
            rows,
            name=args.export,
            description=args.description or "",
            out_dir=Path(args.out_dir) if args.out_dir else None,
            overwrite=args.force,
            relevant_only=not args.all_traffic,
            allow_identities=args.allow_identities,
        )
        doc = json.loads(path.read_text())
        dropped = doc["source"].get("dropped_unreferenced") or {}
        if dropped:
            total = sum(dropped.values())
            detail = ", ".join(f"{cam} {n}" for cam, n in sorted(dropped.items()))
            print(f"\nkept {len(doc['messages'])} of {len(rows)} messages "
                  f"— dropped {total} unreferenced object events ({detail})")
            print("  pass --all-traffic to keep them")
        print(f"\nexported → {path}  ({path.stat().st_size / 1024:.0f} KB)")
        print(f"replay it with: python tools/replay_card.py --scenario {path.stem} --dry-run")
        return

    if args.list or not args.start:
        summarize(rows)
        if not args.list:
            print("\n(no --from given — listing only; add --from/--to to replay)")
        return
    if not rows:
        raise SystemExit("nothing to replay in that window")

    import paho.mqtt.client as mqtt

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    password = os.environ.get("MARCELLUS_PUSH__MQTT_PASSWORD")
    if args.mqtt_username:
        client.username_pw_set(args.mqtt_username, password)
    client.connect(args.mqtt_host, args.mqtt_port, 30)
    client.loop_start()

    print(f"replaying {len(rows)} messages at {args.speed}x…")
    prev_ts = rows[0]["ts"]
    for i, row in enumerate(rows, 1):
        gap = (row["ts"] - prev_ts) / args.speed
        if gap > 0:
            time.sleep(gap)
        prev_ts = row["ts"]
        client.publish(row["topic"], json.dumps(row["payload"]), qos=0)
        print(f"  {i}/{len(rows)} {row['topic']} +{gap:.1f}s", end="\r")
    client.loop_stop()
    client.disconnect()
    print(f"\ndone — {len(rows)} messages republished")


if __name__ == "__main__":
    main()
