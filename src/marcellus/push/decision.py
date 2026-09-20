"""Notification decision engine: parse MQTT review payloads, decide who gets
pushed.

Two pure, dependency-free functions carry the whole contract:

* `parse_review_message` -- turn a `frigate/reviews` MQTT payload into a
  `ReviewEvent`, or `None` if it isn't actionable.
* `devices_for_event` -- which registered devices' subscription filters
  (camera / label / severity) match a given event.

Doing this match at the sidecar, where the MQTT payload already lives, means
a device that wants doorbell-only alerts never causes a send for the garden
camera -- battery, and keeps "the sidecar sees the review id and camera, not
the scene" as tight as possible even internally (spec §1).
"""

from __future__ import annotations

from typing import Any

from marcellus.push.models import SEVERITIES, Device, ReviewEvent, TrackedObject

_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def parse_review_message(payload: dict[str, Any]) -> ReviewEvent | None:
    """Parse one `frigate/reviews` message.

    Fires on `type in ("new", "update", "end")` with `after.severity ==
    "alert"` or `"detection"`. `type == "end"` finalizes a review item and is
    NOT itself pushed (spec's "Architecture at a glance") -- `PushEngine.
    handle_event` short-circuits on it before any push logic runs, exactly as
    it did back when this function dropped "end" messages outright. It is
    parsed through now (rather than returning None) so non-push consumers of
    `handle_event` -- namely `encounters.service.EncounterService.
    observe_review`, wired as the engine's `on_review` hook -- see it too;
    `msg_type=="end"` is how an encounter atom's `end_time` gets filled in
    from the live stream. Returns `None` for anything not actionable rather
    than raising -- a malformed or unrecognised message should be dropped,
    not crash the subscriber loop.
    """
    msg_type = payload.get("type")
    if msg_type not in ("new", "update", "end"):
        return None

    after = payload.get("after") or {}
    if not isinstance(after, dict):
        return None

    severity = after.get("severity")
    if severity not in SEVERITIES:
        return None

    review_id = after.get("id")
    camera = after.get("camera")
    if not review_id or not camera:
        return None

    data = after.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    labels = _strings(data.get("objects"))
    track_ids = _strings(data.get("detections"))
    event_id = track_ids[0] if track_ids else str(review_id)

    try:
        start_time = float(after.get("start_time") or 0.0)
    except (TypeError, ValueError):
        start_time = 0.0

    end_time: float | None
    raw_end = after.get("end_time")
    try:
        end_time = float(raw_end) if raw_end is not None else None
    except (TypeError, ValueError):
        end_time = None

    return ReviewEvent(
        review_id=str(review_id),
        camera=str(camera),
        severity=str(severity),
        labels=labels,
        msg_type=str(msg_type),
        event_id=event_id,
        zones=_strings(data.get("zones")),
        track_ids=track_ids,
        audio=_strings(data.get("audio")),
        sub_labels=_strings(data.get("sub_labels")),
        start_time=start_time,
        end_time=end_time,
    )


def parse_object_message(payload: dict[str, Any]) -> TrackedObject | None:
    """Parse one `frigate/events` message into dwell input.

    Returns None for anything unusable rather than raising -- this topic is
    high-rate (thousands of messages an hour on a live house) and a single odd
    message must never cost the subscriber loop.
    """
    msg_type = payload.get("type")
    if msg_type not in ("new", "update", "end"):
        return None
    after = payload.get("after") or payload.get("before") or {}
    if not isinstance(after, dict):
        return None
    track_id = after.get("id")
    camera = after.get("camera")
    if not track_id or not camera:
        return None
    sub_label = after.get("sub_label")
    if isinstance(sub_label, (list, tuple)):  # Frigate sends [name, score]
        sub_label = sub_label[0] if sub_label else ""
    return TrackedObject(
        track_id=str(track_id),
        camera=str(camera),
        label=str(after.get("label") or ""),
        current_zones=_strings(after.get("current_zones")),
        entered_zones=_strings(after.get("entered_zones")),
        msg_type=str(msg_type),
        stationary=bool(after.get("stationary")),
        sub_label=str(sub_label or ""),
        path_data=_parse_path_xy(after.get("path_data")),
        velocity_angle=_opt_float(after.get("velocity_angle")),
        average_estimated_speed=_opt_float(after.get("average_estimated_speed")),
    )


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_path_xy(raw: Any) -> tuple[tuple[float, float, float], ...]:
    """Extract (x, y, t) triples from Frigate's path_data.
    Frigate sends [[x, y, t], ...] or [[[x, y], t], ...]. Timestamps are
    load-bearing since 2026-08-15 (ground-plane speed needs dt); a point
    without one gets t=0.0 rather than being dropped."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return ()
    result: list[tuple[float, float, float]] = []
    for entry in raw:
        if not entry:
            continue
        t: Any = 0.0
        if len(entry) == 2 and isinstance(entry[0], (list, tuple)) and len(entry[0]) == 2:
            (x, y), t = entry
        elif len(entry) >= 3:
            x, y, t = entry[0], entry[1], entry[2]
        elif len(entry) == 2:
            x, y = entry[0], entry[1]
        else:
            continue
        try:
            result.append((float(x), float(y), float(t)))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _strings(value: Any) -> tuple[str, ...]:
    """A `data.*` list reduced to non-empty strings. Frigate sends `[]` for the
    ones that don't apply and `null` for a few, so both have to be tolerated
    without dropping the message."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if v not in (None, ""))


def matches(device: Device, event: ReviewEvent) -> bool:
    """True if `device`'s subscription filters admit `event`.

    Filters are a subscription, not a client-side discard (spec §1): an empty
    `cameras`/`labels` list means "all", matching the registration contract's
    "omit or [] = all cameras/labels".
    """
    if _SEVERITY_RANK[event.severity] < _SEVERITY_RANK[device.min_severity]:
        return False
    if device.cameras and event.camera not in device.cameras:
        return False
    return not (device.labels and not (set(device.labels) & set(event.labels)))


def devices_for_event(devices: list[Device], event: ReviewEvent) -> list[Device]:
    """Registered devices whose filters match `event`, in input order."""
    return [d for d in devices if matches(d, event)]
