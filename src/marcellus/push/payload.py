"""Building the Interrupt-tier APNs payload (plan §8).

Pure functions, no I/O: the payload the relay forwards to Apple is small
enough and load-bearing enough to be worth testing without a network in
sight.

```json
{
  "aps": {
    "alert": {"title": "At the door", "body": "Person, 6s"},
    "sound": "chime.caf",
    "thread-id": "at-the-door",
    "interruption-level": "time-sensitive",
    "mutable-content": 1,
    "category": "situation.at-the-door"
  },
  "situation_id": "at-the-door",
  "handle": "h_9f3a…",
  "server_id": "s_a1b2c3",
  "sent_at": 1785952622.704,
  "actions_available": ["live-view", "snooze-15m", "mute-situation"]
}
```

What is deliberately *not* in here: the snapshot, the label detail, the zone,
the sub-label, and the raw Frigate event id. All of it lives behind the
handle and is fetched by the NSE from the user's own server (transport spec
§2/§4). The payload carries a handle, not image bytes -- a viewable thumbnail
is ~15KB base64 against APNs' 4KB ceiling, so inlining it doesn't merely
strain the budget, it fails (plan §4).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from marcellus.push.library import sound_file
from marcellus.push.situations import Match

logger = logging.getLogger(__name__)

#: APNs' hard ceiling for a standard alert payload.
APNS_MAX_PAYLOAD_BYTES = 4096

#: Advertised to the app so Phase 2 can attach real buttons. `talk` is
#: deliberately absent: plan §3 only allows advertising it when the sidecar
#: knows the camera supports two-way audio via go2rtc, and Phase 1 doesn't
#: probe that. An action offered and then missing is worse than one never
#: offered.
DEFAULT_ACTIONS = ("live-view", "snooze-15m", "mute-situation")


def pretty_label(label: str) -> str:
    text = (label or "").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else ""


def body_text(match: Match, *, suppressed: int = 0) -> str:
    """The one line under the situation name.

    Specific enough to act on without opening anything -- *what* and *how
    long* are the two facts that decide whether you get up. The dwell is only
    shown when the situation actually asked for one; "Person, 0s" is noise.
    """
    if match.audio:
        text = pretty_label(match.audio) or "Sound detected"
    else:
        label = pretty_label(match.label) or "Motion"
        if match.situation.loiter_seconds and match.dwell_s >= 1:
            text = f"{label}, {int(match.dwell_s)}s"
        else:
            text = label
    if suppressed > 0:
        # Plan §6: the window closed quietly, but the next push that gets
        # through says how much it stood in for.
        text = f"{text} · +{suppressed} more"
    return text


def build_payload(
    match: Match,
    *,
    handle: str,
    server_id: str,
    suppressed: int = 0,
    actions: tuple[str, ...] = DEFAULT_ACTIONS,
    now: float | None = None,
) -> dict[str, Any]:
    """The full APNs body for one situation push.

    `sent_at` is stamped here rather than by the caller so every path that can
    emit a situation push -- a live match, the Settings test button, and
    whatever Phase 2 adds -- carries it without having to remember to.
    """
    situation = match.situation
    sent_at = time.time() if now is None else now
    payload: dict[str, Any] = {
        "aps": {
            "alert": {"title": situation.name or situation.id, "body": body_text(
                match, suppressed=suppressed
            )},
            "sound": sound_file(situation.sound),
            "thread-id": situation.id,
            "interruption-level": "time-sensitive",
            # Without this the NSE never runs and the alert ships as delivered,
            # image-less (transport spec §2).
            "mutable-content": 1,
            "category": f"situation.{situation.id}",
        },
        "situation_id": situation.id,
        "handle": handle,
        # Not in plan §8's example, kept from transport spec §2: a device may
        # have more than one server registered, and the NSE has to know which
        # base URL to redeem the handle against without a hostname in the
        # payload.
        "server_id": server_id,
        # Unix epoch seconds, to the millisecond, taken the moment the payload
        # is built -- the last timestamp the sidecar controls before the bytes
        # leave for the relay. The NSE subtracts it to get the sidecar -> NSE
        # and sidecar -> present deltas, which is the only way to see the APNs
        # hop from the outside: Apple gives no delivery receipt.
        #
        # Sub-second precision on purpose. Whole seconds would quantise a
        # measurement whose interesting range is hundreds of milliseconds.
        "sent_at": round(sent_at, 3),
        "actions_available": list(actions),
    }
    return _fit_to_budget(payload)


def payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode())


def _fit_to_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Truncate the body if a payload somehow approaches APNs' 4KB ceiling.

    It cannot with the fields above -- everything here is bounded by a
    situation name and a label -- but a user-authored `name` is the one
    unbounded string in the shape, and an over-budget push is rejected
    outright rather than trimmed by Apple.
    """
    if payload_size(payload) <= APNS_MAX_PAYLOAD_BYTES:
        return payload
    alert = payload["aps"]["alert"]
    alert["title"] = str(alert["title"])[:120]
    alert["body"] = str(alert["body"])[:180]
    if payload_size(payload) > APNS_MAX_PAYLOAD_BYTES:  # pragma: no cover - unreachable today
        logger.warning("push: payload still over %d bytes after trim", APNS_MAX_PAYLOAD_BYTES)
    return payload
