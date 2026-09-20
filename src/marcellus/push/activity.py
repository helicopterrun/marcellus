"""Live Activity **end** payload (Phase 2 plan, "Push contract additions").

Only the `end` shape lives here now -- the start/update/escalation channel is
`push/live_activities.py` (v2). The engine still ends an unpromoted activity
directly with `build_end`, carrying a `dismissal-date` so the activity fades
on its own tail rather than vanishing the instant the object leaves.

`ContentState` is a wire contract shared with the app's `Codable` struct, so
the key names here are load-bearing: `stage`, `dwell_seconds`, `title`,
`subtitle`, `thumbnail_revision`, snake_case exactly as the handoff spells
them.
"""

from __future__ import annotations

import time
from typing import Any

from marcellus.push.payload import (
    APNS_MAX_PAYLOAD_BYTES,
    body_text,
    payload_size,
    pretty_label,
)
from marcellus.push.situations import (
    STAGE_ARRIVING,
    STAGE_ENDING,
    Match,
)

#: How long the activity lingers after resolution before iOS fades it.
DEFAULT_DISMISSAL_TAIL_S = 30.0
#: A shorter tail for an activity that never earned its place -- an early-fire
#: start off a `detection` review that never promoted to `alert`.
UNPROMOTED_DISMISSAL_TAIL_S = 10.0


def content_state(
    match: Match,
    *,
    stage: str,
    thumbnail_revision: int = 1,
) -> dict[str, Any]:
    """The dynamic half, identical in shape on every push that carries state."""
    return {
        "stage": stage,
        "dwell_seconds": int(match.dwell_s),
        "title": match.situation.name or match.situation.id,
        "subtitle": _subtitle(match, stage),
        "thumbnail_revision": thumbnail_revision,
    }


def _subtitle(match: Match, stage: str) -> str:
    """The line under the title.

    Reuses Phase 1's alert-body authoring so an activity that escalates into a
    banner doesn't visibly change its wording halfway through -- the user is
    meant to experience one thing evolving, not two descriptions of it.
    """
    if stage == STAGE_ARRIVING:
        # No dwell worth quoting yet: "Person, 0s" reads as broken.
        label = pretty_label(match.label) or "Motion"
        return f"{label} just arrived" if not match.audio else pretty_label(match.audio)
    if stage == STAGE_ENDING:
        return "Cleared"
    return body_text(match)


def build_end(
    match: Match,
    *,
    thumbnail_revision: int = 1,
    tail_s: float = DEFAULT_DISMISSAL_TAIL_S,
    now: float | None = None,
) -> dict[str, Any]:
    """Resolution, with the tail the activity fades over."""
    sent_at = time.time() if now is None else now
    payload = {
        "aps": {
            "timestamp": int(sent_at),
            "event": "end",
            "content-state": content_state(
                match, stage=STAGE_ENDING, thumbnail_revision=thumbnail_revision
            ),
            # Epoch seconds; iOS fades the activity at this moment rather than
            # yanking it off the screen the instant the object left frame.
            "dismissal-date": int(sent_at + tail_s),
        },
        "sent_at": round(sent_at, 3),
    }
    return _fit(payload)


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim the two author-controlled strings if a payload nears APNs' cap.

    Live-activity payloads have the same 4KB ceiling as alerts, and the only
    unbounded strings in this shape are the situation name (which the user
    types) and the subtitle derived from it.
    """
    if payload_size(payload) <= APNS_MAX_PAYLOAD_BYTES:
        return payload
    state = payload["aps"].get("content-state")
    if isinstance(state, dict):
        state["title"] = str(state.get("title", ""))[:120]
        state["subtitle"] = str(state.get("subtitle", ""))[:180]
    alert = payload["aps"].get("alert")
    if isinstance(alert, dict):
        alert["title"] = str(alert.get("title", ""))[:120]
        alert["body"] = str(alert.get("body", ""))[:180]
    return payload
