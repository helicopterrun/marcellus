"""Card-model Live Activities (Elsinore Phase 3).

Parallel output channel to the ordinary card push (`delivery.py`): the same
card lifecycle (create/enrich/escalate/deescalate/resolve,
`push/cards.py`/`push/delivery_wire.py`) additionally drives a Live Activity
for cards that fall into one of four "families" worth a Dynamic
Island/lock-screen presence. This module is pure -- family detection, glyph
mapping, and the three APNs payload shapes -- with no I/O; wiring the result
into `push_activities` rows and actually sending lives in `delivery_wire.py`,
mirroring the pure/orchestration split `delivery.py` already uses for the
ordinary card push.

**Not the same Live Activity as `push/activity.py`.** That module is the
notification-experience plan's *situations* Live Activity (`stage`,
`dwell_seconds`, `title`, `subtitle`; `attributes-type:
"SituationActivityAttributes"`), already shipped and untouched by this
phase. This one is shaped for the card model (`level`, `mutation`, `glyph`,
`primary`/`secondary`, `elapsed_seconds`, `deep_link_card_key`,
`thumbnail_handle`/`thumbnail_revision`) and uses the app's renamed
`ElsinoreActivityAttributes` type. They run independently; a device could in
principle have both kinds of activity live at once (not exercised today
since the two card sources -- ladder cards and situations -- are separate
features).

Both kinds of activity share the same `push_activities` table and
`push/store.py` helpers (`open_activity`/`find_activity`/`touch_activity`/
`close_activity`/`record_activity_send`): `situation_id` is just a text
column, and a card's `card_key` fits it exactly as well as a situation id
does. Reusing it (over a new `la_activity_id` column on `push_cards`) is
deliberate -- `push_cards` is one row per *card*, but a Live Activity is one
per *(device, card)*: a card with two registered push-to-start devices runs
two independent activities on two different token, and a single nullable
column on the card row cannot represent that. `push_activities`, keyed on
`(apns_token, situation_id, track_id)`, already does.
"""

from __future__ import annotations

import json
import time
from typing import Any

from marcellus.push.cards import RESOLVE
from marcellus.push.payload import APNS_MAX_PAYLOAD_BYTES, payload_size

#: The app's `ActivityAttributes` conformer for card-model activities.
#: iOS matches the push-to-start push to a registered attributes type by
#: this exact string -- it must equal the Swift type name.
ATTRIBUTES_TYPE = "ElsinoreActivityAttributes"

_RELEVANCE_SCORE = {"urgent": 1.0, "notify": 0.75, "quiet": 0.5, "log": 0.25}

PACKAGE = "package"
BINS = "bins"
OPENINGS = "openings"
PERSON = "person"
PERSON_RESTRICTED = "person_restricted"
FAMILIES = (PACKAGE, BINS, OPENINGS, PERSON, PERSON_RESTRICTED)
#: Catch-all family for `la_only` mode: any pushable card that matches no
#: curated family still gets an activity. The app treats `family` as an
#: opaque string by design ("lets a new family ship server-side without an
#: app rebuild" -- ElsinoreActivityAttributes.swift), so no app change is
#: needed for this value to work.
CATCH_ALL = "activity"

#: Frigate labels that make a card an opening (door/gate/garage). Public:
#: `delivery_wire.classify_subject` uses the same sets to classify the V3
#: subjects, so subject and family can never disagree about a label.
OPENING_LABELS = frozenset({"door", "gate", "garage"})
#: Frigate labels for the bins family -- the bin itself or the correlated
#: truck event.
BIN_LABELS = frozenset({"waste_bin", "garbage_truck"})

#: Semantic glyph ids (SF Symbol names, or a documented custom name the app's
#: asset catalog resolves) per family/state. Resolve always wins regardless
#: of family -- a card leaving the screen shows the same "done" glyph no
#: matter what it was.
_RESOLVED_GLYPH = "checkmark.circle.fill"
_PERSON_KNOWN_GLYPH = "figure.wave"
_PERSON_STRANGER_GLYPH = "figure.walk"
_PACKAGE_GLYPH = "shippingbox.fill"
_BINS_GLYPH = "trash.fill"
_OPENING_GLYPH = {
    "garage": "door.garage.open.trianglebadge.exclamationmark",
    "gate": "pedestrian.gate.open",
    "door": "door.left.hand.open",
}
#: Catch-all glyphs by subject kind. Must be real SF Symbol names -- the
#: widget renders content-state.glyph via Image(systemName:) with no
#: fallback, so an invalid name is an empty Dynamic Island.
_CATCH_ALL_GLYPH = {
    "stranger": "figure.walk",
    "known": "figure.wave",
    "person": "figure.walk",
    "vehicle": "car.fill",
    "animal": "pawprint.fill",
    "thing": "cube.fill",
    "package": "shippingbox.fill",
    "bin": "trash.fill",
    "opening": "door.left.hand.open",
}
_CATCH_ALL_DEFAULT_GLYPH = "dot.radiowaves.left.and.right"


def classify_family(
    *, subject_kind: str, label: str, place_class: str, level: str = "log",
) -> str | None:
    """The curated family this card's *content* matches, ignoring toggles,
    opening picks, and `catch_all` entirely. Exists as its own function
    (not just `should_start_activity`'s internals) so a caller can tell "no
    curated family matched at all" apart from "a curated family matched but
    wasn't eligible" -- both collapse to the same `None` result out of
    `should_start_activity` itself, which the `la_only` fallback decision
    log needs to tell apart (`delivery_wire.py`).

    The person family is gated on the card's *routed level*, not a place
    class: the routing table is the single authority on what matters, and
    LA families follow it rather than second-guessing geography. The old
    `place_class == "doors"` gate meant a user whose entry zones were
    classified Private (a perfectly sensible classification) could never
    get a person LA at all — observed live 2026-08-14. `person_restricted`
    keeps its place-class identity because Restricted *is* the user's own
    routing vocabulary, and a person there is a categorically different
    situation regardless of level.

    Since the V3 subjects (one alerts stack, 2026-08-20), package/bin/
    opening are subjects of their own and their families are gated on the
    routed level exactly like the person family: level `log` means the
    ladder cell said log (or an old table had nothing louder), so no
    activity runs. The `thing`+label spelling is kept as a fallback for
    callers that still classify these cards as `thing` (an old applied
    table, direct test callers)."""
    if subject_kind == "package" or (subject_kind == "thing" and label == "package"):
        return PACKAGE if level != "log" else None
    if subject_kind == "bin" or (subject_kind == "thing" and label in BIN_LABELS):
        return BINS if level != "log" else None
    if subject_kind == "opening" or (subject_kind == "thing" and label in OPENING_LABELS):
        return OPENINGS if level != "log" else None
    if subject_kind in ("stranger", "known", "person") and place_class == "off_limits":
        return PERSON_RESTRICTED
    if subject_kind in ("stranger", "known", "person") and level in ("notify", "urgent"):
        return PERSON
    return None


def should_start_activity(
    *,
    subject_kind: str,
    label: str,
    place_class: str,
    level: str = "log",
    opening_picks: list[str] | None = None,
    opening_ids: tuple[str, ...] = (),
    catch_all: bool = False,
) -> str | None:
    """Which LA family this card qualifies for, or `None`.

    The retired per-family booleans are gone (one alerts stack,
    2026-08-20): whether a family runs is the routed level's call, decided
    inside `classify_family` -- the outcome ladder is the single authority.

    `opening_picks`/`opening_ids` are the one family-specific refinement
    left (Phase 4 §3): the `openings` family additionally requires this
    card's zone or camera to be one of the openings the user actually
    picked. An empty or absent `opening_picks` means "nothing curated yet"
    and is read permissively (every opening qualifies) rather than as
    "nothing qualifies" -- turning openings off is an Off row in the
    ladder now; an empty picks list is a not-yet-configured state, not a
    choice.

    `catch_all` (`la_only` mode): every pushable card gets *an* activity,
    period. A curated family that doesn't clear the checks above (routed
    to log, or `openings` with picks that don't match) falls back to
    `CATCH_ALL` here rather than returning `None` -- `la_only`'s whole
    contract is that a Live Activity is the only surface, so routing a card
    to no activity at all would silently drop it (the card push is always
    passive/silent in this mode, so nothing else would tell the user). A
    card that matches no curated family at all falls back the same way, as
    before.
    """
    family = classify_family(
        subject_kind=subject_kind, label=label, place_class=place_class, level=level,
    )

    if (
        family == OPENINGS
        and opening_picks
        and not any(oid in opening_picks for oid in opening_ids)
    ):
        family = None

    if family is None and catch_all:
        family = CATCH_ALL
    return family


def glyph_for(family: str, *, subject_kind: str, label: str, mutation: str) -> str:
    """The `content-state.glyph` for one mutation of a qualifying card.

    Resolve is the same glyph for every family (design doc §3) -- checked
    first so it short-circuits the family-specific branches below rather
    than duplicating the check into each of them.
    """
    if mutation == RESOLVE:
        return _RESOLVED_GLYPH
    if family in (PERSON, PERSON_RESTRICTED):
        return _PERSON_KNOWN_GLYPH if subject_kind == "known" else _PERSON_STRANGER_GLYPH
    if family == PACKAGE:
        return _PACKAGE_GLYPH
    if family == BINS:
        return _BINS_GLYPH
    if family == OPENINGS:
        return _OPENING_GLYPH.get(label, _OPENING_GLYPH["door"])
    if family == CATCH_ALL:
        return _CATCH_ALL_GLYPH.get(subject_kind, _CATCH_ALL_DEFAULT_GLYPH)
    return _RESOLVED_GLYPH


_MAX_PATH_POINTS = 30
_CONTENT_STATE_BUDGET = 4096


def downsample_path(
    raw: list[list[float] | tuple[float, ...]], *, max_points: int = _MAX_PATH_POINTS,
) -> list[list[float]]:
    """Evenly downsample a path to at most `max_points`, preserving first
    and last. Coordinates rounded to 2 decimals, clamped to [0, 1]."""
    if not raw:
        return []
    cleaned: list[list[float]] = []
    for pt in raw:
        if len(pt) < 2:
            continue
        x = round(max(0.0, min(1.0, float(pt[0]))), 2)
        y = round(max(0.0, min(1.0, float(pt[1]))), 2)
        cleaned.append([x, y])
    if len(cleaned) <= max_points:
        return cleaned
    step = (len(cleaned) - 1) / (max_points - 1)
    result = [cleaned[round(i * step)] for i in range(max_points - 1)]
    result.append(cleaned[-1])
    return result


def build_content_state(
    *,
    level: str,
    mutation: str,
    glyph: str,
    primary: str,
    secondary: str,
    elapsed_seconds: int,
    card_key: str,
    thumbnail_handle: str | None,
    thumbnail_revision: int,
    state_since_ts: float | None = None,
    motion: dict[str, Any] | None = None,
    zones: dict[str, Any] | None = None,
    path: dict[str, Any] | None = None,
    extra_stories: int = 0,
    camera: str | None = None,
    story_started_ts: float | None = None,
) -> dict[str, Any]:
    """The dynamic half of the activity, snake_case to match the Swift
    type's `CodingKeys` exactly -- these field names are load-bearing wire
    contract, not a style choice.

    `extra_stories`/`camera` are additive (Elsinore Phase 4, device-scoped
    Live Activities): the count of other eligible open stories beyond the
    primary one this content-state otherwise describes, and the primary's
    camera. Both omitted when there is nothing to add -- `extra_stories`
    stays off the wire at 0 (the common single-story case looks exactly like
    before), `camera` only when the caller has one to report.

    `story_started_ts` (additive) is when the story's card was CREATED, as
    opposed to `state_since_ts`, which resets on every escalation. The
    widget's deep link needs the former: tapping the activity should land
    the scrub on the moment the story began, not on its latest escalation.
    """
    state: dict[str, Any] = {
        "level": level,
        "mutation": mutation,
        "glyph": glyph,
        "primary": primary,
        "secondary": secondary,
        "elapsed_seconds": int(elapsed_seconds),
        "deep_link_card_key": card_key,
        "thumbnail_revision": thumbnail_revision,
    }
    if thumbnail_handle is not None:
        state["thumbnail_handle"] = thumbnail_handle
    if state_since_ts is not None:
        state["state_since_ts"] = state_since_ts
    if motion is not None:
        state["motion"] = motion
    if zones is not None:
        state["zones"] = zones
    if path is not None:
        state["path"] = path
    if extra_stories:
        state["extra_stories"] = extra_stories
    if camera:
        state["camera"] = camera
    if story_started_ts is not None:
        state["story_started_ts"] = story_started_ts
    encoded_size = len(json.dumps(state, separators=(",", ":")).encode())
    assert encoded_size <= _CONTENT_STATE_BUDGET, (
        f"content-state {encoded_size} bytes exceeds {_CONTENT_STATE_BUDGET} byte budget"
    )
    return state


def build_la_start_payload(
    *,
    content_state: dict[str, Any],
    family: str,
    camera: str,
    track_id: str,
    card_key: str,
    now: float | None = None,
    stale_s: float = 900.0,
    sound: str | None = None,
) -> dict[str, Any]:
    """The push-to-start payload asking iOS to create the activity.

    iOS requires ``aps.alert`` on every push-to-start push — without it
    ``liveactivitiesd`` rejects the payload with "Received start without
    an alert configuration".
    """
    sent_at = time.time() if now is None else now
    level = content_state.get("level", "log")
    aps: dict[str, Any] = {
        "timestamp": int(sent_at),
        "event": "start",
        "relevance-score": _RELEVANCE_SCORE.get(level, 0.25),
        "stale-date": int(sent_at + stale_s),
        "alert": {
            "title": content_state.get("primary", ""),
            "body": content_state.get("secondary", ""),
        },
        "content-state": content_state,
        "attributes-type": ATTRIBUTES_TYPE,
        "attributes": {
            "card_key": card_key,
            "family": family,
            "camera": camera,
            "track_id": track_id,
        },
    }
    if sound:
        aps["alert"]["sound"] = sound
    payload = {"aps": aps}
    return _fit(payload)


def build_la_update_payload(
    *, content_state: dict[str, Any], now: float | None = None,
    stale_s: float = 900.0,
    alert: bool = False,
    alert_title: str = "",
    alert_body: str = "",
    sound: str | None = None,
    interruption_level: str | None = None,
) -> dict[str, Any]:
    """A content-state advance over the per-activity token.

    When `alert` is True, the update carries an `alert` dict + optional sound
    so iOS surfaces a banner (escalation alert, §5).
    """
    sent_at = time.time() if now is None else now
    level = content_state.get("level", "log")
    aps: dict[str, Any] = {
        "timestamp": int(sent_at),
        "event": "update",
        "relevance-score": _RELEVANCE_SCORE.get(level, 0.25),
        "stale-date": int(sent_at + stale_s),
        "content-state": content_state,
    }
    if alert:
        alert_dict: dict[str, Any] = {"title": alert_title, "body": alert_body}
        if sound:
            alert_dict["sound"] = sound
        aps["alert"] = alert_dict
        if interruption_level:
            aps["interruption-level"] = interruption_level
    payload = {"aps": aps}
    return _fit(payload)


def build_la_end_payload(
    *,
    content_state: dict[str, Any],
    now: float | None = None,
    dismissal_offset: float = 30.0,
) -> dict[str, Any]:
    """Resolution. `dismissal-date` is `timestamp + dismissal_offset` --
    show the resolved state briefly, then iOS clears it from the lock
    screen; the activity itself lingers in the recent-activities area for up
    to 4h on its own, system-controlled schedule."""
    sent_at = time.time() if now is None else now
    level = content_state.get("level", "log")
    payload = {
        "aps": {
            "timestamp": int(sent_at),
            "event": "end",
            "relevance-score": _RELEVANCE_SCORE.get(level, 0.25),
            "dismissal-date": int(sent_at) + int(dismissal_offset),
            "content-state": content_state,
        },
    }
    return _fit(payload)


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """Same trim rule as `delivery.py`/`activity.py`'s own `_fit_to_budget`:
    `primary`/`secondary` are the only unbounded, user-facing strings in
    this shape."""
    if payload_size(payload) <= APNS_MAX_PAYLOAD_BYTES:
        return payload
    state = payload["aps"].get("content-state")
    if isinstance(state, dict):
        state["primary"] = str(state.get("primary", ""))[:120]
        state["secondary"] = str(state.get("secondary", ""))[:180]
    return payload
