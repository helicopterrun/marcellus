"""Wires the delivery pipeline (`delivery.py`, `cards.py`) to the sidecar's
existing Frigate event flow (Elsinore Phase 2, config-gated, default off).

No new transport, no new MQTT subscription: `PushEngine.handle_event` /
`handle_object_payload` are already the two entry points every
`frigate/reviews` and `frigate/events` message passes through, so this
module is a plain function each of them calls once, guarded by
`settings.push.delivery_enabled`.

**Subject classification here is a deliberate MVP**, not the full-fidelity
mapping the design doc's ladder deserves -- `frigate/reviews` carries labels
but no resolved identity (Phase 5's territory), so `classify_subject` is a
heuristic, documented as such, safe to ship because the whole pipeline is
off by default. **Place classification** (`classify_place`) is Phase 4's:
it reads the user's own `settings.zone_classes`
(`push/policy_settings.py`), falling back to the same name-guessing
heuristic the settings API exposes for a zone nobody has classified yet.
Tightening `classify_subject` is a data/code change here, not a change to
`ladder.py` or `delivery.py`, exactly like `ladder_policy.py`'s own
separation of policy from evaluation order.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from marcellus.push import (
    card_store,
    decision_trace,
    ground,
    live_activities,
    policy_settings,
    store,
)
from marcellus.push.cards import CREATE, DEESCALATE, ENRICH, ESCALATE, RESOLVE, Card
from marcellus.push.cards import SUPPRESSED as SUPPRESSED_MUTATION
from marcellus.push.delivery import (
    _device_eligible,
    _is_snoozed,
    build_card_key,
    build_card_payload,
    send_card_mutation,
    should_push,
    sound_name_for_card,
)
from marcellus.push.delivery import advance_card as _advance_card
from marcellus.push.ladder import (
    SUPPRESSED,
    Snapshot,
    evaluate_ladder,
    evaluate_ladder_explained,
)
from marcellus.push.models import ReviewEvent
from marcellus.push.payload import pretty_label

if TYPE_CHECKING:
    import sqlite3

    from marcellus.config import PushSection
    from marcellus.push.engine import PushEngine
    from marcellus.push.models import Device
    from marcellus.push.transport import PushTransport

#: Mutations that get a snapshot -- the card is still active and its
#: bounding box/identity may still be refining. Never `escalate`/
#: `deescalate` (unrequested scope this round) and never `resolve`: a
#: resolved card is about to leave Notification Center, so spending the
#: NSE's ~15s fetch budget on an image nobody will see is pure waste.
_MEDIA_MUTATIONS = frozenset({CREATE, ENRICH})

#: Cross-camera dedup window (docs/push-notifications.md "Cross-camera
#: deduplication"). Real-world data showed 3-4s gaps between overlapping
#: cameras picking up the same walk-through, but detection latency varies by
#: camera and lighting; 15s catches slow detections without false-merging
#: genuinely separate events arriving well apart. No config knob for v1 --
#: a constant like every other MVP threshold in this module.
_DEDUP_WINDOW_S = 15.0

#: §8 LA cadence: minimum seconds between content-state pushes per activity,
#: for a device with `frequent_pushes_enabled` set.
_LA_UPDATE_MIN_INTERVAL_S = 3.0
#: Phase A default pacing for a device that hasn't opted into frequent
#: pushes -- most devices don't need tighter cadence and it costs more APNs
#: traffic / battery on the receiving end.
_LA_UPDATE_MIN_INTERVAL_SLOW_S = 15.0
#: §8 path growth threshold: push only when this many new points have arrived.
_LA_PATH_GROWTH_THRESHOLD = 3

#: Place-class ordering outermost→innermost, for the zones.ladder.
_PLACE_ORDER = ("street", "yard", "doors", "private", "off_limits")

logger = logging.getLogger(__name__)

#: Per-apns_token snapshot of the last LA push, for delta detection.
#: In-memory only — a sidecar restart flushes it, which just means the first
#: post-restart push always goes out (safe). Device-scoped (Elsinore Phase
#: 4): one Live Activity per device now, so the key drops `card_key`.
_la_prev_state: dict[str, dict[str, Any]] = {}

#: Sentinel (device-scoped) identity the app posts as `situationId`/`trackId`
#: for the one Live Activity per device now runs, and the row's own
#: `collapse_id` -- see `_deliver_live_activities`'s docstring. Defined in
#: `store.py` (which `find_activity`/`find_dismissed_activity`/
#: `stale_activities` filter on) so this module and the store can't drift;
#: re-exported under the historical private names used throughout this file.
_DEVICE_SITUATION_ID = store.DEVICE_SITUATION_ID
_DEVICE_TRACK_ID = store.DEVICE_TRACK_ID

#: Frigate labels this MVP treats as an animal subject, beyond the
#: dangerous-animal labels `ladder.py` already reclassifies via `label`
#: regardless of what subject is passed in.
_ANIMAL_LABELS = frozenset({"dog", "cat", "bird", "deer", "squirrel", "raccoon", "bear", "skunk"})
_VEHICLE_LABELS = frozenset({"car", "motorcycle", "bicycle"})

_SUBJECT_GLYPH = {
    "stranger": "person.stranger",
    "known": "person.identified",
    "person": "person.detected",
    "vehicle": "vehicle.detected",
    "animal": "animal.seen",
    "thing": "thing.detected",
    # V3 subjects keep the `thing.*` semantic-glyph namespace: the app's
    # glyph catalog predates them, and their labelled glyphs stay
    # "thing.package"-shaped via `_glyph_for` for the same reason.
    "package": "thing.detected",
    "bin": "thing.detected",
    "opening": "thing.detected",
}

_SUBJECT_COPY = {
    "stranger": "Person",
    "known": "Person",
    "person": "Person",
    "vehicle": "Vehicle",
    "animal": "Animal",
}


def classify_subject(event: ReviewEvent) -> str:
    """Observable-subject classification (routing v2): label, camera, zone
    only. Identity (sub_label/plate) is never consulted at create time —
    it arrives later via recognition and only relaxes a running story."""
    labels = set(event.labels)
    if "person" in labels:
        return "person"
    if labels & _VEHICLE_LABELS:
        return "vehicle"
    if labels & _ANIMAL_LABELS:
        return "animal"
    # V3 subjects (one alerts stack): the labels that used to pick an LA
    # *family* off a `thing` card now classify the subject itself, so the
    # outcome ladder is the single authority on what they do. `thing`
    # remains the fallback for labels nothing claims.
    if "package" in labels:
        return "package"
    if labels & live_activities.BIN_LABELS:
        return "bin"
    if labels & live_activities.OPENING_LABELS:
        return "opening"
    return "thing"


def zone_place(zone: str, zone_classes: dict[str, str]) -> str:
    """One zone's place class.

    `zone_classes` (Elsinore Phase 4: `push/policy_settings.py`,
    `settings.zone_classes`) is the user's explicit zone -> place-class
    assignment; a zone not in it falls back to the same name-guessing heuristic
    the settings API's `available_zones` uses (`policy_settings.guess_zone_class`),
    rather than the flat "yard" this used to default to -- new zones (a camera
    the user just added) get a reasonable class immediately instead of going
    silent until explicitly configured.
    """
    if zone in zone_classes:
        return zone_classes[zone]
    return policy_settings.guess_zone_class(zone)


def most_severe_zone(
    zones: Sequence[str], *, subject: str, zone_classes: dict[str, str]
) -> tuple[str, str]:
    """The zone that routes loudest for this subject, and its place class.

    Frigate lists an object's zones in its own order, and a tight zone is
    normally listed after the broad one containing it: `charger` is a small
    polygon inside `parking_area`/`back_walkway`, so it always trails one of
    them. Taking zones[0] therefore made the most specific -- and most severe --
    zone on the property unreachable. A person standing at the Tesla wall
    charger evaluated as **quiet**: `parking_area` supplied the place class
    *and* the zone-override key, and `zone_overrides[parking_area][person] =
    quiet` short-circuits `evaluate_ladder` before the table is ever consulted.
    `charger` alone evaluates to urgent. Discovered by the first recorded
    fixture (`cap-charger-person-two-cams`); the hand-written scenarios could
    not find it, because every zone they name is a singleton.

    Ranking runs through `ladder.base_level`, the same override -> off-cell ->
    table precedence the evaluator itself applies, so the winner is the zone
    that will genuinely route loudest. Ranking on place class alone would not
    be enough: a quiet zone's explicit override beats a loud zone's table cell,
    and the user's override is their most specific statement.

    Ties keep the earliest zone, so an event whose zones are all equally severe
    behaves exactly as it did before this existed.
    """
    from marcellus.push import ladder, ladder_policy

    if not zones:
        return "", "street"

    best_zone, best_place, best_rank = "", "street", -2
    for zone in zones:
        if not zone:
            continue
        place = zone_place(zone, zone_classes)
        level, _ = ladder.base_level(subject, place, zone)
        rank = -1 if level == ladder.SUPPRESSED else ladder_policy.LEVELS.index(level)
        if rank > best_rank:
            best_zone, best_place, best_rank = zone, place, rank
    if best_rank == -2:  # every entry was blank
        return "", "street"
    return best_zone, best_place


def classify_place(event: ReviewEvent, zone_classes: dict[str, str]) -> str:
    """The place class the event routes on. See `most_severe_zone` for why this
    is no longer simply the first zone Frigate happened to list."""
    return most_severe_zone(
        event.zones, subject=classify_subject(event), zone_classes=zone_classes
    )[1]


def snapshot_from_review(
    event: ReviewEvent,
    *,
    zone_classes: dict[str, str],
    nobody_home: bool = False,
    night: bool = False,
    dwell_exceeded: bool = False,
    muted: bool = False,
) -> tuple[Snapshot, str, str]:
    """Build the ladder `Snapshot`, returning `(snapshot, subject_kind,
    place_class)` since the payload contract needs the classification
    alongside the level the snapshot evaluates to."""
    subject_kind = classify_subject(event)
    # One decision, used twice: the place class and the zone-override key must
    # name the SAME zone, or a fix to one is undone by the other (see
    # `most_severe_zone`).
    zone, place_class = most_severe_zone(
        event.zones, subject=subject_kind, zone_classes=zone_classes
    )
    label = event.labels[0] if event.labels else ""
    snapshot = Snapshot(
        subject=subject_kind, place=place_class, zone=zone, label=label,
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded, muted=muted,
    )
    return snapshot, subject_kind, place_class


def _fmt_elapsed(elapsed_s: float) -> str:
    """Human elapsed for notification copy: "just now" under a minute, then
    minutes, then h/m — raw "137s" read like debug output."""
    s = int(elapsed_s)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} hr {(s % 3600) // 60} min"


def _copy(
    subject_kind: str, label: str, camera: str, zone_name: str, elapsed_s: float,
    identity: str = "",
    story: str = "",
) -> tuple[str, str]:
    """Title + body (content design 2026-08-15).

    Title: "{Who} in {zone display name}" when the event has a zone —
    the sidecar-edited `zone_names` map (or Frigate `friendly_name`) supplies
    a real place phrase; a bare rule key is humanized as a last resort. With
    no zone the camera never masquerades as a place: "{Who} · {Camera} camera".

    Body ("notable verbs only"): when `story` is set (approaching / running /
    still there / left after …) it leads; otherwise camera (when the title
    used a zone) + friendly elapsed. A line with nothing new stays empty.
    """
    subject_text = _SUBJECT_COPY.get(subject_kind) or pretty_label(label) or "Motion"
    who = identity or subject_text
    camera_pretty = camera.replace("_", " ").title()
    if zone_name:
        friendly = policy_settings.zone_display_name(zone_name)
        place = friendly or zone_name.replace("_", " ").title()
        primary = f"{who} near {place}"
        detail = f"{camera_pretty} camera" if camera else ""
    else:
        primary = f"{who} · {camera_pretty} camera" if camera else who
        detail = ""

    parts = [p for p in (story, detail, _fmt_elapsed(elapsed_s) if elapsed_s > 0 else "") if p]
    return primary, " · ".join(parts)


def _glyph_for(subject_kind: str, label: str) -> str:
    if label:
        # V3 subjects emit the same wire ids their cards had as `thing`s --
        # every app build in the field resolves "thing.package", none know
        # "package.package".
        kind = "thing" if subject_kind in ("package", "bin", "opening") else subject_kind
        return f"{kind}.{label}"
    return _SUBJECT_GLYPH.get(subject_kind, "motion.detected")


#: Normalized-image-space displacement below which movement is jitter, not
#: travel. q10 of real person path steps measured at 0.008, median 0.066
#: (tools/verify_heading.py — since removed, git history — over config captures, 2026-08-15).
_HEADING_MIN_DISPLACEMENT = 0.02


def _movement_vector(
    path_data: Sequence[tuple[float, ...]] | None,
) -> tuple[float, float] | None:
    """Recent direction of travel as a unit vector in normalized image
    space (y down), from the track's path trail: walk back from the newest
    point until the displacement clears the jitter floor. None while the
    trail is too short or the subject hasn't really moved."""
    if not path_data or len(path_data) < 2:
        return None
    x1, y1 = path_data[-1][0], path_data[-1][1]
    for point in reversed(path_data[:-1]):
        dx, dy = x1 - point[0], y1 - point[1]
        dist = math.hypot(dx, dy)
        if dist >= _HEADING_MIN_DISPLACEMENT:
            return (dx / dist, dy / dist)
    return None


def _heading_label(
    path_data: Sequence[tuple[float, ...]] | None, stationary: bool, camera: str,
) -> str | None:
    """One of the §8 heading words, or None when unknown.

    Measured 2026-08-15 (tools/verify_heading.py, git history; 24,572 captured events):
    this install's Frigate reports velocity_angle=0 / speed=0 on every
    message (no zone distance calibration), so the old angle thresholds
    were reading a constant — every moving subject showed "leaving".
    Heading now comes from the path trail dotted against the per-camera
    vector the user draws on /cameras ("toward home"); an uncalibrated
    camera honestly shows no chip rather than a guess."""
    if stationary:
        return "stationary"
    movement = _movement_vector(path_data)
    if movement is None:
        return None
    calib = policy_settings.get_active().get("camera_headings", {}).get(camera)
    if not isinstance(calib, dict):
        # No hand-drawn vector: fall back to the one derived from world
        # geometry (pie azimuth + secure area on the /cameras map).
        calib = policy_settings.derived_camera_heading(camera)
    if not isinstance(calib, dict):
        return None
    dot = movement[0] * calib.get("dx", 0.0) + movement[1] * calib.get("dy", 0.0)
    # cos 60° = 0.5: within 60° of the drawn vector = approaching; within
    # 60° of its opposite = leaving; the perpendicular band = passing.
    if dot >= 0.5:
        return "approaching"
    if dot <= -0.5:
        return "leaving"
    return "passing"


def _build_motion(
    path_data: Sequence[tuple[float, ...]] | None, stationary: bool, camera: str,
    speed_label: str | None = None,
) -> dict[str, str] | None:
    heading = _heading_label(path_data, stationary, camera)
    if heading is None:
        return None
    motion: dict[str, str] = {"heading": heading}
    if speed_label and heading not in ("stationary",):
        motion["speed_label"] = speed_label
    return motion


#: Per-(camera, track) consecutive-heading counter for sustained-direction
#: routing. In-memory like `_la_prev_state`; a restart just resets streaks.
_heading_streaks: dict[tuple[str, str], tuple[str | None, int]] = {}


def _update_heading_streak(camera: str, track_id: str, heading: str | None) -> int:
    prev, count = _heading_streaks.get((camera, track_id), (None, 0))
    count = count + 1 if (heading is not None and heading == prev) else (1 if heading else 0)
    _heading_streaks[(camera, track_id)] = (heading, count)
    return count


def last_heading(camera: str, track_id: str) -> str | None:
    """The most recent heading observed for this track (engine reads this
    to shorten the LA dismissal tail on 'leaving')."""
    return _heading_streaks.get((camera, track_id), (None, 0))[0]


#: Per-(camera, track) last ANNOUNCED distance to the secure area, in feet.
#: In-memory like the streaks; a restart just re-announces.
_announced_distance: dict[tuple[str, str], int] = {}


def _round_distance_ft(ft: float) -> int:
    """Copy-grade rounding: nearest 5 ft, floored at 5 (the exact number is
    projection-model precision theater below that)."""
    return max(5, int(round(ft / 5.0)) * 5)


def _approach_story(nearest_ft: int | None, speed_words: set[str]) -> str:
    """Copy for a confirmed approach. With a calibrated world model this
    says how far out ("approaching — 30 ft out, walking"); beyond 100 ft
    the number is projection-error noise and the classic phrase stands."""
    if nearest_ft is not None and nearest_ft == 0:
        return "at the house"
    if nearest_ft is not None and nearest_ft <= 100:
        pace = (
            ", running" if "running" in speed_words
            else ", walking" if "walking" in speed_words else ""
        )
        return f"approaching — {nearest_ft} ft out{pace}"
    return "approaching the house"


def _stabilize_distance(camera: str, track_id: str, raw_ft: float) -> int:
    """Rounded distance with hysteresis: keep announcing the previous value
    until the raw distance moves ≥10 ft from it, so copy never flaps
    30 → 25 → 30 across consecutive card mutations. 0 (inside the secure
    area) always announces immediately."""
    key = (camera, track_id)
    prev = _announced_distance.get(key)
    if raw_ft <= 2.5:
        _announced_distance[key] = 0
        return 0
    if prev is not None and prev > 0 and abs(raw_ft - prev) < 10.0:
        return prev
    out = _round_distance_ft(raw_ft)
    _announced_distance[key] = out
    return out


def _build_zones_ladder(
    event_zones: tuple[str, ...],
    current_zones: tuple[str, ...],
    zone_classes: dict[str, str],
) -> dict[str, Any] | None:
    """Build zones.ladder (display names outermost→innermost) and current_index."""
    if not event_zones:
        return None
    ordered: list[tuple[int, str]] = []
    for z in event_zones:
        pc = zone_classes.get(z) or policy_settings.guess_zone_class(z)
        idx = _PLACE_ORDER.index(pc) if pc in _PLACE_ORDER else 1
        ordered.append((idx, z))
    ordered.sort(key=lambda t: t[0])
    ladder = [z.replace("_", " ").title() for _, z in ordered[:5]]
    zone_names = [z for _, z in ordered[:5]]
    current_index = -1
    for i, name in enumerate(zone_names):
        if name in current_zones:
            current_index = i
    return {"ladder": ladder, "current_index": current_index}


def _build_la_path(
    path_data: list[tuple[float, float, float]],
) -> dict[str, Any] | None:
    if not path_data:
        return None
    # Wire contract stays [x, y] pairs (4KB ContentState budget) — the
    # per-point timestamp is server-side fuel (speed), never shipped.
    points = live_activities.downsample_path(
        [[pt[0], pt[1]] for pt in path_data],
    )
    if not points:
        return None
    return {"points": points}


def _la_has_visible_delta(
    *,
    mutation: str,
    prev_mutation: str | None,
    prev_level: str | None,
    level: str,
    primary: str,
    prev_primary: str | None,
    glyph: str,
    prev_glyph: str | None,
    current_zones: tuple[str, ...],
    prev_zones: tuple[str, ...] | None,
    path_len: int,
    prev_path_len: int,
    heading: str | None,
    prev_heading: str | None,
) -> str | None:
    """Return the delta reason if there's a visible change worth pushing,
    or None to suppress the push."""
    if mutation in (CREATE, ESCALATE, DEESCALATE, RESOLVE):
        return mutation
    if level != prev_level:
        return "level_change"
    if primary != prev_primary:
        return "text_change"
    if glyph != prev_glyph:
        return "glyph_change"
    if current_zones != prev_zones:
        return "zone_transition"
    if heading is not None and heading != prev_heading:
        return "heading_change"
    if path_len - prev_path_len >= _LA_PATH_GROWTH_THRESHOLD:
        return "path_growth"
    return None


def _media_for(
    mutation: str,
    event: ReviewEvent,
    *,
    conn: sqlite3.Connection,
    engine: PushEngine | None,
    config: PushSection,
) -> tuple[str | None, str | None, asyncio.Task[bool] | None]:
    """Mint a handle and kick off the snapshot pre-warm for a mutation that
    gets media (`_MEDIA_MUTATIONS`), returning `(handle, media_url,
    warm_task)`.

    All three are `None` when there's nothing to show: `escalate`/
    `deescalate`/`resolve` never get one (a resolved card is about to leave
    Notification Center -- spending the NSE's ~15s fetch budget on an image
    nobody will see is pure waste), and neither does a mutation that would,
    absent `config.external_base_url` or an `engine` to pre-warm through
    (e.g. a caller only exercising the pure classifier). The bare `handle`
    is returned alongside the full URL because the Live Activity content-
    state (Phase 3) wants just the handle -- the widget already knows its
    own base URL, unlike the card push's self-contained `media` field.

    Same mechanism as the situations path (`PushEngine._fire_group`): mint
    the handle synchronously, fire the Frigate fetch as a background task
    the caller runs concurrently with the send, never in series -- the push
    already carries the URL optimistically, so a slow/failed fetch costs the
    notification its image, not its existence.
    """
    if mutation not in _MEDIA_MUTATIONS or not config.external_base_url or engine is None:
        return None, None, None
    handle = store.mint_handle(
        conn, camera=event.camera, event_id=event.event_id, review_id=event.review_id,
        ttl_s=config.situation_handle_ttl_s,
    )
    conn.commit()
    media = f"{config.external_base_url.rstrip('/')}/v1/push/thumbnail/{handle}"
    warm_task = asyncio.create_task(
        engine.prewarm_thumbnail(handle, camera=event.camera, event_id=event.event_id)
    )
    return handle, media, warm_task


def _resolve_card_for_track(
    conn: sqlite3.Connection,
    *,
    camera: str,
    track_id: str,
    subject_kind: str,
    zone_name: str,
    zones: tuple[str, ...] = (),
    now: float,
    geo_mates: list[tuple[str, str]] | None = None,
    geo_enabled: bool = False,
) -> tuple[str, Card | None, str, bool]:
    """Which card this (camera, track_id) evaluation belongs to, applying
    cross-camera dedup (docs/push-notifications.md "Cross-camera
    deduplication") before a fresh card key would otherwise be minted.

    Returns `(card_key, existing_card_or_None, owning_camera, via_geo)`.
    `owning_camera` is this track's own camera unless the track has been
    merged onto a card another camera created first, in which case it's
    that card's original camera -- callers must persist *that*, not
    `camera`, so a merged card's identity/timeline routing never flips to
    whichever camera happened to enrich it most recently.

    Three paths, checked in order:

    1. This track already has an alias (a prior evaluation merged it onto
       another camera's card) whose target is still open -- keep using it.
       A stale alias (target since closed/resolved) is dropped so this
       track falls through to its own natural key, per the "a fresh card if
       still detected" rule (design doc): once the merged card is gone,
       there's nothing left to enrich.
    2. No alias, and this is a genuinely new track (no row yet under its own
       natural key) with a zone: look for an open card with the same
       `subject_kind`/`zone_name` created within the dedup window. If one
       exists, alias this track onto it instead of creating a sibling.
    3. No zone match, but geometry clusters this track with another
       camera's track that owns an open same-label card (`geo_mates`, from
       fusion.cluster) -- adopt that card WHEN the `geometric_dedup` policy
       flag is on. Flag off: log what would have been adopted
       ("geometric_dedup: would_suppress ...") so a week of logs can be
       grepped against actual duplicate cards before enabling.
    4. Otherwise (existing card under its own key, or no zone to dedup on)
       -- this track's own natural key, unchanged from before this feature.
    """
    alias_key = card_store.get_track_alias(conn, camera, track_id)
    if alias_key is not None:
        aliased = card_store.get_card(conn, alias_key)
        if aliased is not None and not aliased.closed:
            ctx = card_store.get_card_context(conn, alias_key)
            return alias_key, aliased, (ctx or {}).get("camera") or camera, False
        card_store.delete_track_alias(conn, camera, track_id)

    natural_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    existing = card_store.get_card(conn, natural_key)
    if existing is None:
        # Label flip (animal -> person on the same track): keep the story on
        # its original card instead of minting a sibling. The card keeps its
        # birth key (collapse-id stability); subject_kind context updates on
        # the next upsert, so copy/routing follow the new label.
        flipped_key = card_store.find_open_card_for_track(
            conn, camera=camera, track_id=track_id, exclude_key=natural_key,
        )
        if flipped_key is not None:
            flipped = card_store.get_card(conn, flipped_key)
            if flipped is not None and not flipped.closed:
                logger.info(
                    "push: label flip keeps card=%s (was routing as %s)",
                    flipped_key, natural_key,
                )
                ctx = card_store.get_card_context(conn, flipped_key)
                return flipped_key, flipped, (ctx or {}).get("camera") or camera, False
    neighbor_cameras = policy_settings.camera_neighbor_set(camera)
    if existing is None and (zone_name or neighbor_cameras):
        candidate_key = card_store.find_dedup_candidate(
            conn, subject_kind=subject_kind, zone_name=zone_name,
            exclude_key=natural_key, now=now, window_s=_DEDUP_WINDOW_S,
            zones=zones, neighbor_cameras=neighbor_cameras,
        )
        if candidate_key is not None:
            candidate = card_store.get_card(conn, candidate_key)
            if candidate is not None and not candidate.closed:
                card_store.set_track_alias(conn, camera, track_id, candidate_key, now)
                ctx = card_store.get_card_context(conn, candidate_key)
                return candidate_key, candidate, (ctx or {}).get("camera") or camera, False

    # Geometric adoption: zone dedup found nothing, but fusion clustered
    # this track with another camera's track. Adopt that mate's open card
    # (its alias target, else its own natural card) — the mate saw the same
    # physical object within the distance-scaled merge threshold.
    if existing is None and geo_mates:
        for mate_cam, mate_tid in geo_mates:
            mate_key = card_store.get_track_alias(conn, mate_cam, mate_tid)
            if mate_key is None:
                mate_key = card_store.find_open_card_for_track(
                    conn, camera=mate_cam, track_id=mate_tid, exclude_key="",
                )
            if mate_key is None:
                continue
            mate_card = card_store.get_card(conn, mate_key)
            if mate_card is None or mate_card.closed:
                continue
            if not geo_enabled:
                # Validation breadcrumb — fires with the flag OFF so the
                # operator can count would-suppress vs. real duplicates.
                logger.info(
                    "geometric_dedup: would_suppress card=%s adopting=%s/%s",
                    natural_key, mate_cam, mate_tid,
                )
                break
            card_store.set_track_alias(conn, camera, track_id, mate_key, now)
            ctx = card_store.get_card_context(conn, mate_key)
            logger.info(
                "geometric_dedup: adopted card=%s for %s/%s", mate_key, camera, track_id,
            )
            return mate_key, mate_card, (ctx or {}).get("camera") or camera, True

    return natural_key, existing, camera, False


async def _deliver_live_activities(
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    *,
    config: PushSection,
    card: Card,
    mutation: str,
    family: str | None,
    camera: str,
    subject_kind: str,
    label: str,
    primary: str,
    secondary: str,
    elapsed_seconds: int,
    media_handle: str | None,
    now: float,
    sound_allowed: bool = True,
    state_since_ts: float | None = None,
    motion: dict[str, Any] | None = None,
    zones: dict[str, Any] | None = None,
    path: dict[str, Any] | None = None,
    place_class: str = "",
) -> set[str]:
    """The Live Activity side of one card mutation, one iteration per
    registered device (Elsinore Phase 4, device-scoped aggregation).

    One Live Activity per *device* now, not one per (device, card): every
    device that can run an activity has at most a single open
    `push_activities` row, keyed on `apns_token` alone under the sentinel
    `situation_id`/`track_id` pair `_DEVICE_SITUATION_ID`/`_DEVICE_TRACK_ID`
    the app posts back (`store.find_activity`/`find_dismissed_activity` are
    device-scoped to match). That row's content-state describes the PRIMARY
    story -- the highest-level currently-open, eligible card for this
    device, tie-broken by most recently updated -- with `extra_stories`
    reporting how many other eligible open stories exist alongside it.

    `card`/`mutation`/`family`/`camera`/... describe the ONE card mutation
    that triggered this call; the aggregate primary may or may not be that
    same card. When it is, the caller's rich copy/motion/zones/path (all
    freshly computed for this mutation) are used verbatim. When some other
    already-open card outranks it, this function rebuilds that card's
    primary/secondary copy from its stored `push_cards` context (label,
    camera, zone_name, state_since_at) -- good enough for a card that isn't
    the one actively mutating, but necessarily without its live
    motion/zones/path (not persisted per-card) or its identity/story copy
    embellishments (also not persisted) -- documented simplification.

    Returns the apns tokens of devices whose Live Activity *demonstrably*
    covers this mutation: a start/update/end APNs send that succeeded. The
    caller demotes the ordinary card push to silent for exactly those
    devices.
    """
    covered: set[str] = set()
    if not config.delivery_la_enabled:
        # Fires on every mutation this function is called for while LA is
        # configured off -- per-update cadence, not a transition. DEBUG
        # (Wave 2B §4).
        logger.debug("push: LA skipped — delivery_la_enabled=False")
        return covered
    card_key = card.card_key
    logger.info(
        "push: LA enter mutation=%s family=%s card_key=%s devices=%d",
        mutation, family, card_key, len(devices),
    )

    policy = policy_settings.get_active()
    la_settings = policy.get("live_activities", {})
    la_only = la_settings.get("la_only", False)
    escalation_sound = policy.get("escalation_sound", "urgent")
    stale_s = config.delivery_la_stale_s

    # Every other currently-open card, from the DB -- the current card's own
    # row hasn't been upserted yet at this point in the pipeline (that
    # happens later, in `send_card_mutation`), so it's overlaid below with
    # the live values this call was given instead of whatever's stale in
    # the DB (or absent, on a brand-new card's first mutation).
    open_cards = {c.card_key: (c, ctx) for c, ctx in card_store.list_open_cards(conn)}
    if mutation == RESOLVE or card.closed or card.resolved:
        open_cards.pop(card_key, None)
    else:
        open_cards[card_key] = (
            card,
            {
                "subject_kind": subject_kind, "place_class": place_class,
                "camera": camera, "zone_name": "", "label": label,
                "family": family or "", "media_handle": card.media_handle,
            },
        )

    for device in devices:
        device_row = store.find_activity(conn, apns_token=device.apns_token)
        tombstone = store.find_dismissed_activity(conn, apns_token=device.apns_token)

        eligible = [
            (c, ctx) for c, ctx in open_cards.values()
            if ctx.get("family")
            and _device_eligible(
                device, camera=ctx.get("camera", ""),
                labels=(ctx.get("label", ""),), card_level=c.level,
            )
            and not _is_snoozed(conn, device, ctx.get("camera", ""), now=now)
        ]
        is_triggering_eligible = any(c.card_key == card_key for c, _ in eligible)

        if tombstone is not None:
            if mutation == ESCALATE:
                # Escalation breaks through a dismissal: the user swiped
                # away a quiet activity, but this story just got more
                # important than the dismissal anticipated.
                store.delete_activity(conn, tombstone["activity_id"])
                tombstone = None
            elif not eligible:
                # The device's last open story just closed while dismissed
                # -- clean slate, so a genuinely new story later gets its
                # own fresh start rather than an inherited suppression.
                store.delete_activity(conn, tombstone["activity_id"])
                tombstone = None
            else:
                # Quiet period: no re-start on CREATE/UPDATE, including a
                # brand-new story joining. Fires once per mutation this
                # device is dismissed for -- per-update cadence, not a
                # transition -- so it's DEBUG (Wave 2B §4).
                logger.debug("push: LA skip device=%s reason=dismissed", device.device_id)
                continue

        if device_row is None:
            if not eligible or not is_triggering_eligible:
                continue
            if mutation not in (CREATE, ESCALATE) or family is None or not device.can_live_activity:
                # Same per-update cadence as the "dismissed" skip above --
                # fires on every non-qualifying mutation for an
                # otherwise-eligible device (e.g. every UPDATE before the
                # first ESCALATE). DEBUG, not INFO (Wave 2B §4).
                logger.debug(
                    "push: LA skip device=%s reason=no_row mutation=%s family=%s"
                    " la_capable=%s pts=%s",
                    device.device_id, mutation, family, device.la_capable,
                    bool(device.push_to_start_token),
                )
                continue
            primary_card, primary_ctx = _pick_primary(eligible)
            content_state = _build_aggregate_state(
                primary_card, primary_ctx, eligible, card_key=card_key,
                mutation=mutation, thumbnail_revision=1,
                current=(primary, secondary, elapsed_seconds, media_handle,
                         state_since_ts, motion, zones, path),
            )
            la_start_sound = (
                sound_name_for_card(card.level, subject_kind, label,
                                    escalation_sound=escalation_sound)
                if sound_allowed and not la_only else None
            )
            payload = live_activities.build_la_start_payload(
                content_state=content_state, family=primary_ctx.get("family") or family,
                camera=primary_ctx.get("camera", camera), track_id=_DEVICE_TRACK_ID,
                card_key=_DEVICE_SITUATION_ID, now=now, stale_s=stale_s,
                sound=la_start_sound,
            )
            logger.info(
                "push: LA start device=%s family=%s pts_token=%s...",
                device.device_id, family, (device.push_to_start_token or "")[:16],
            )
            result = await transport.send_live_activity(
                device, token=device.push_to_start_token, payload=payload,
                collapse_id=_DEVICE_SITUATION_ID, event="start",
                apns_priority=10, apns_expiration=int(now + 900),
            )
            # The intent to start is already an INFO line just above; this is
            # the raw result echo right after it, and on a persistently
            # failing push-to-start token it repeats on every mutation
            # (device_row stays None) -- DEBUG (Wave 2B §4).
            logger.debug("push: LA start result ok=%s error=%s", result.ok, result.error)
            if not result.ok:
                continue
            covered.add(device.apns_token)
            # Only the deep-link fields, deliberately: the visible-delta
            # comparison on the first update must still see an empty
            # `prev` (a start is not an update it could be a duplicate of).
            _la_prev_state[device.apns_token] = _la_deep_link_memory(content_state)
            activity_id = f"a_{secrets.token_urlsafe(8)}"
            store.open_activity(
                conn, activity_id=activity_id, apns_token=device.apns_token,
                situation_id=_DEVICE_SITUATION_ID, track_id=_DEVICE_TRACK_ID,
                camera=primary_ctx.get("camera", camera), collapse_id=_DEVICE_SITUATION_ID,
                handle=media_handle or "", now=now,
            )
            store.record_activity_send(conn, activity_id=activity_id, now=now)
            # Commit before the loop moves on to the next device's own
            # `await transport.*`: a per-frame handler holding this write
            # across another coroutine's send is the convoy that produced
            # `database is locked` and duplicate push-to-start sends
            # (root-cause journal 2026-09-02).
            conn.commit()
            continue

        if not device_row["token"]:
            # No per-activity token yet (app hasn't confirmed the start) --
            # keep the row alive so the sweep doesn't reap a young LA.
            store.touch_activity(conn, device_row["activity_id"], now=now)
            conn.commit()
            continue

        if not eligible:
            # END: nothing eligible remains open for this device.
            content_state = live_activities.build_content_state(
                level=card.level, mutation=RESOLVE,
                glyph=live_activities.glyph_for(
                    family or "", subject_kind=subject_kind, label=label, mutation=RESOLVE,
                ),
                primary=primary, secondary=secondary, elapsed_seconds=elapsed_seconds,
                card_key=card_key, thumbnail_handle=None,
                thumbnail_revision=int(device_row["thumbnail_revision"] or 1),
                state_since_ts=state_since_ts, motion=motion, zones=zones, path=path,
                camera=camera, story_started_ts=round(card.created_at, 1),
            )
            payload = live_activities.build_la_end_payload(
                content_state=content_state, now=now, dismissal_offset=30.0,
            )
            token = device_row["token"]
            result = await transport.send_live_activity(
                device, token=token, payload=payload, collapse_id=_DEVICE_SITUATION_ID,
                event="end", apns_priority=5,
            )
            if result.ok:
                covered.add(device.apns_token)
            store.close_activity(conn, device_row["activity_id"], now=now)
            conn.commit()
            _la_prev_state.pop(device.apns_token, None)
            continue

        primary_card, primary_ctx = _pick_primary(eligible)
        # A brand-new eligible story joining an already-live device activity
        # (whether or not it becomes the primary) is treated the same as an
        # escalation: it bypasses the pacing/delta gates and carries an
        # alert, reusing the start's sound accounting.
        new_story_join = mutation == CREATE and is_triggering_eligible
        is_escalate = mutation == ESCALATE
        bypass = is_escalate or new_story_join

        revision = int(device_row["thumbnail_revision"] or 1)
        current_is_primary = primary_card.card_key == card_key
        if current_is_primary and media_handle:
            revision += 1

        content_state = _build_aggregate_state(
            primary_card, primary_ctx, eligible, card_key=card_key,
            mutation=mutation, thumbnail_revision=revision,
            current=(primary, secondary, elapsed_seconds, media_handle,
                     state_since_ts, motion, zones, path),
        )
        p_primary = content_state["primary"]
        p_secondary = content_state["secondary"]
        glyph_val = content_state["glyph"]

        heading_val = motion.get("heading") if (current_is_primary and motion) else None
        current_zones_tuple = tuple(zones["ladder"]) if (current_is_primary and zones) else ()
        path_len = len(path["points"]) if (current_is_primary and path) else 0
        prev = _la_prev_state.get(device.apns_token, {})
        delta_reason = _la_has_visible_delta(
            mutation=mutation,
            prev_mutation=prev.get("mutation"),
            prev_level=prev.get("level"),
            level=primary_card.level,
            primary=p_primary,
            prev_primary=prev.get("primary"),
            glyph=glyph_val,
            prev_glyph=prev.get("glyph"),
            current_zones=current_zones_tuple,
            prev_zones=prev.get("zones"),
            path_len=path_len,
            prev_path_len=prev.get("path_len", 0),
            heading=heading_val,
            prev_heading=prev.get("heading"),
        )
        last_la_push = float(device_row["last_push_at"] or 0)
        if delta_reason is None and not bypass:
            continue
        min_interval = (
            _LA_UPDATE_MIN_INTERVAL_S
            if device.frequent_pushes_enabled
            else _LA_UPDATE_MIN_INTERVAL_SLOW_S
        )
        if now - last_la_push < min_interval and not bypass:
            continue
        logger.info(
            "la-push reason=%s card_key=%s primary=%s",
            delta_reason or ("new_story" if new_story_join else "escalate"),
            card_key, primary_card.card_key,
        )

        wants_alert = False
        la_sound = None
        la_interruption = None
        if la_only:
            pass
        elif bypass:
            wants_alert = True
            if sound_allowed and (is_escalate and primary_card.level == "urgent" or new_story_join):
                la_sound = sound_name_for_card(
                    primary_card.level, primary_ctx.get("subject_kind", ""),
                    primary_ctx.get("label", ""), escalation_sound=escalation_sound,
                )
            la_interruption = "time-sensitive" if primary_card.level == "urgent" else "active"
        elif family == live_activities.PERSON_RESTRICTED and mutation != RESOLVE:
            wants_alert = True
            la_interruption = "time-sensitive"

        priority = 10 if wants_alert else 5
        payload = live_activities.build_la_update_payload(
            content_state=content_state, now=now, stale_s=stale_s,
            alert=wants_alert, alert_title=p_primary, alert_body=p_secondary,
            sound=la_sound, interruption_level=la_interruption,
        )
        result = await transport.send_live_activity(
            device, token=device_row["token"], payload=payload,
            collapse_id=_DEVICE_SITUATION_ID, event="update",
            apns_priority=priority, apns_expiration=int(now + 900),
        )
        if result.ok:
            covered.add(device.apns_token)
        store.touch_activity(
            conn, device_row["activity_id"], thumbnail_revision=revision, pushed=True, now=now,
        )
        store.record_activity_send(conn, activity_id=device_row["activity_id"], now=now)
        conn.commit()
        _la_prev_state[device.apns_token] = {
            "mutation": mutation, "level": primary_card.level, "primary": p_primary,
            "glyph": glyph_val, "zones": current_zones_tuple,
            "path_len": path_len, "heading": heading_val,
            **_la_deep_link_memory(content_state),
        }

    return covered


_LEVEL_RANK = {"log": 0, "quiet": 1, "notify": 2, "urgent": 3}


def _pick_primary(
    eligible: list[tuple[Card, dict[str, str]]],
) -> tuple[Card, dict[str, str]]:
    """Highest level wins; ties go to the most recently updated story."""
    return max(eligible, key=lambda t: (_LEVEL_RANK.get(t[0].level, 0), t[0].updated_at))


def _la_deep_link_memory(content_state: dict[str, Any]) -> dict[str, Any]:
    """The slice of a sent content-state that the deferred end
    (`end_activity_if_card_closed`) replays so the ended activity keeps
    deep-linking to a real story: the device sentinel it would otherwise
    carry decodes as camera "device" at no time at all in the app."""
    return {
        "card_key": content_state["deep_link_card_key"],
        "camera": content_state.get("camera"),
        "state_since_ts": content_state.get("state_since_ts"),
        "story_started_ts": content_state.get("story_started_ts"),
    }


def _build_aggregate_state(
    primary_card: Card,
    primary_ctx: dict[str, str],
    eligible: list[tuple[Card, dict[str, str]]],
    *,
    card_key: str,
    mutation: str,
    thumbnail_revision: int,
    current: tuple[
        str, str, int, str | None, float | None,
        dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None,
    ],
) -> dict[str, Any]:
    """The device-wide content-state: the PRIMARY story's copy/instrument
    fields, plus `extra_stories`/`camera`. When the primary IS the card that
    triggered this call, the caller's freshly-computed copy/motion/zones/
    path/thumbnail are used as-is; otherwise they're rebuilt from the
    primary's stored `push_cards` context (see `_deliver_live_activities`
    docstring for what's necessarily lost in that case).
    """
    (cur_primary, cur_secondary, cur_elapsed, cur_thumbnail, cur_state_since,
     cur_motion, cur_zones, cur_path) = current
    extra_stories = max(0, len(eligible) - 1)
    camera = primary_ctx.get("camera", "")
    if primary_card.card_key == card_key:
        p_primary, p_secondary, p_elapsed = cur_primary, cur_secondary, cur_elapsed
        # `cur_thumbnail` is only non-None on CREATE/ENRICH (the mutations
        # that mint fresh media, `_MEDIA_MUTATIONS`) -- an escalate/resolve
        # for this same card falls back to the card's own persisted handle
        # instead of blanking the widget's thumbnail mid-story.
        p_thumbnail = cur_thumbnail or (primary_card.media_handle or None)
        p_state_since = cur_state_since
        p_motion, p_zones, p_path = cur_motion, cur_zones, cur_path
        glyph = live_activities.glyph_for(
            primary_ctx.get("family", ""), subject_kind=primary_ctx.get("subject_kind", ""),
            label=primary_ctx.get("label", ""), mutation=mutation,
        )
        state_mutation = mutation
    else:
        p_elapsed = int(max(0.0, time.time() - primary_card.state_since_at))
        p_primary, p_secondary = _copy(
            primary_ctx.get("subject_kind", ""), primary_ctx.get("label", ""),
            camera, primary_ctx.get("zone_name", ""), p_elapsed,
        )
        # Non-triggering primary: no live mint happened this call, so use
        # its own persisted handle (carried on the `push_cards` context by
        # `list_open_cards`/`_row_to_ctx`, alongside label/family/camera) --
        # '' counts as "no handle at all" and is treated as None.
        p_thumbnail = primary_ctx.get("media_handle") or primary_card.media_handle or None
        p_state_since = (
            round(primary_card.state_since_at, 1) if primary_card.state_since_at else None
        )
        p_motion = p_zones = p_path = None
        glyph = live_activities.glyph_for(
            primary_ctx.get("family", ""), subject_kind=primary_ctx.get("subject_kind", ""),
            label=primary_ctx.get("label", ""), mutation=ENRICH,
        )
        state_mutation = "update"

    return live_activities.build_content_state(
        level=primary_card.level, mutation=state_mutation, glyph=glyph,
        primary=p_primary, secondary=p_secondary, elapsed_seconds=p_elapsed,
        card_key=primary_card.card_key, thumbnail_handle=p_thumbnail,
        thumbnail_revision=thumbnail_revision, state_since_ts=p_state_since,
        motion=p_motion, zones=p_zones, path=p_path,
        extra_stories=extra_stories, camera=camera,
        story_started_ts=round(primary_card.created_at, 1),
    )



async def end_activity_if_card_closed(
    conn: sqlite3.Connection,
    device: Device,
    transport: PushTransport,
    *,
    token: str,
    now: float | None = None,
) -> bool:
    """Deferred end for the fast create→resolve race, device-scoped
    (Elsinore Phase 4): the app now posts `attach_activity_token` for the
    one device-wide activity, and by the time the token lands every open
    story it covered may already have closed. `_deliver_live_activities`
    can't send that end itself in that case (iOS rejects `end` on the p2s
    token), so it leaves the row open; the token-upload route calls this the
    moment the token lands to send the deferred end instead of stranding the
    LA on the lock screen until its stale-date.

    No card key to check anymore -- the device's *aggregate* eligibility
    (any open, eligible story) is what decides whether there's still
    something to show.
    """
    now = time.time() if now is None else now
    eligible = [
        (c, ctx) for c, ctx in card_store.list_open_cards(conn)
        if ctx.get("family")
        and _device_eligible(
            device, camera=ctx.get("camera", ""),
            labels=(ctx.get("label", ""),), card_level=c.level,
        )
        and not _is_snoozed(conn, device, ctx.get("camera", ""), now=now)
    ]
    if eligible:
        return False
    device_row = store.find_activity(conn, apns_token=device.apns_token)
    if device_row is None:
        return False
    # No specific closed card to rebuild copy from (the situation id is the
    # device sentinel, not a card key) -- reuse the last content this
    # device's activity actually showed, deep-link target included: the
    # ended activity stays tappable on the lock screen for its dismissal
    # window, and the sentinel would route that tap to a camera called
    # "device" at no time at all.
    prev = _la_prev_state.get(device.apns_token, {})
    content_state = live_activities.build_content_state(
        level=prev.get("level", "log"), mutation=RESOLVE,
        glyph=prev.get("glyph")
        or live_activities.glyph_for("", subject_kind="", label="", mutation=RESOLVE),
        primary=prev.get("primary", ""), secondary="", elapsed_seconds=0,
        card_key=prev.get("card_key") or _DEVICE_SITUATION_ID, thumbnail_handle=None,
        thumbnail_revision=int(device_row["thumbnail_revision"] or 1),
        state_since_ts=prev.get("state_since_ts"),
        camera=prev.get("camera") or device_row["camera"] or None,
        story_started_ts=prev.get("story_started_ts"),
    )
    payload = live_activities.build_la_end_payload(
        content_state=content_state, now=now, dismissal_offset=30.0,
    )
    result = await transport.send_live_activity(
        device, token=token, payload=payload, collapse_id=_DEVICE_SITUATION_ID, event="end",
        apns_priority=5,
    )
    store.close_activity(conn, device_row["activity_id"], now=now)
    _la_prev_state.pop(device.apns_token, None)
    # A closed device activity's own dismissal tombstone must not outlive it
    # -- a future story deserves its own fresh start, not a suppression left
    # over from the activity that just ended.
    tombstone = store.find_dismissed_activity(conn, apns_token=device.apns_token)
    if tombstone is not None:
        store.delete_activity(conn, tombstone["activity_id"])
    logger.info(
        "push: LA deferred end device=%s ok=%s error=%s",
        device.device_id, result.ok, result.error,
    )
    return result.ok


async def handle_delivery_event(
    event: ReviewEvent,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    engine: PushEngine | None = None,
    now: float | None = None,
    nobody_home: bool = False,
    night: bool = False,
    dwell_exceeded: bool = False,
) -> int:
    """One `frigate/reviews` message through the delivery pipeline. Returns
    the number of cards mutated (0 if `delivery_enabled` is off).

    `engine` is only needed to pre-warm a snapshot thumbnail
    (`PushEngine.prewarm_thumbnail`, same as the situations path) -- optional
    so pure-logic-focused callers/tests that don't care about `media` don't
    have to construct one. Without it (or without `config.external_base_url`
    set), `media` is simply omitted, same as "nothing to show".
    """
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now
    policy = policy_settings.get_active()
    escalation_sound = policy.get("escalation_sound", "urgent")

    # Quiet hours (§4): check before ladder evaluation.
    import datetime
    local_now = datetime.datetime.now()
    now_minutes = local_now.hour * 60 + local_now.minute
    qh_active, qh_mode = policy_settings.is_quiet_hours(policy, now_minutes)

    muted = bool(policy.get("mute_sounds"))

    # Motion BEFORE routing (2026-08-15): heading streaks and ground speed
    # are ladder modifiers now, not just LA decoration. Computed once per
    # track here, reused for the LA content build below.
    approaching_secure = False
    leaving_scene = False
    moving_fast = False
    track_motion: dict[str, dict[str, str] | None] = {}
    # Nearest distance (ft) from any of this event's tracks to the secure
    # area, rounded/hysteresis-stabilized for copy; None when the world
    # model can't say. geo_members maps each of this event's tracks to its
    # cross-camera cluster mates (other cameras only), for dedup adoption.
    nearest_ft: int | None = None
    speed_words: set[str] = set()
    geo_members: dict[str, list[tuple[str, str]]] = {}
    if engine is not None:
        # World projection first (was after the motion loop): the motion
        # loop needs per-track map positions for distance-to-secure.
        _scale = policy.get("map_scale_ft")
        _aspect = ground.map_aspect(policy)
        from marcellus.push import fusion
        _positions: list[fusion.TrackPos] = []
        _clusters: list[fusion.Cluster] = []
        if _scale and _scale > 0:
            _positions = fusion.track_world_positions(
                engine.tracks, policy, now=time.time(),
            )
            _clusters = fusion.cluster(
                _positions, scale_ft=_scale, aspect_h_over_w=_aspect,
            )
        _pos_by_track = {(p.camera, p.track_id): p for p in _positions}

        _raw_nearest: float | None = None
        for _tid in (event.track_ids or (event.event_id,)):
            _ts = engine.tracks.get(event.camera, _tid)
            if _ts is None:
                continue
            _heading = _heading_label(_ts.path_data, _ts.stationary, event.camera)
            _speed = ground.speed_label(ground.speed_ft_s(_ts.path_data, event.camera))
            _streak = _update_heading_streak(event.camera, _tid, _heading)
            if _heading == "approaching" and _streak >= 2:
                approaching_secure = True
            if _heading == "leaving" and _streak >= 2:
                leaving_scene = True
            if _speed == "running":
                moving_fast = True
            if _speed:
                speed_words.add(_speed)
            track_motion[_tid] = _build_motion(
                _ts.path_data, _ts.stationary, event.camera, speed_label=_speed,
            )
            _tp = _pos_by_track.get((event.camera, _tid))
            if _tp is not None:
                _d = ground.distance_to_secure_ft(
                    _tp.x, _tp.y, policy.get("secure_area"),
                    scale_ft=_scale, aspect_h_over_w=_aspect,
                )
                if _d is not None and (_raw_nearest is None or _d < _raw_nearest):
                    _raw_nearest = _d
        if _raw_nearest is not None:
            _key_tid = (event.track_ids or (event.event_id,))[0]
            nearest_ft = _stabilize_distance(event.camera, _key_tid, _raw_nearest)

        # Geometric-fusion logs (kept from the log-only phase) + the
        # cluster-mate index for dedup adoption below.
        for _tp in _positions:
            if _tp.camera == event.camera:
                # Unconditional per-review-event position dump, one line per
                # tracked position on this camera -- pure per-frame/update
                # diagnostic, not a transition. DEBUG (Wave 2B §4; the
                # geometric_dedup lines just below stay INFO -- they're the
                # deliberate flag-off validation breadcrumb, gated on an
                # actual multi-member cluster, not every position).
                logger.debug(
                    # camera dropped -- %(push_ctx)s's cam= already names it.
                    "push: world pos track=%s map=(%.3f, %.3f)",
                    _tp.track_id, _tp.x, _tp.y,
                )
        for _cl in _clusters:
            if len(_cl.members) <= 1:
                continue
            logger.info(
                "push: geometric_dedup would_link=%s label=%s map=(%.3f, %.3f)",
                ",".join(f"{m.camera}/{m.track_id}" for m in _cl.members),
                _cl.label, _cl.x, _cl.y,
            )
            for _m in _cl.members:
                if _m.camera == event.camera:
                    geo_members[_m.track_id] = [
                        (o.camera, o.track_id)
                        for o in _cl.members if o.camera != event.camera
                    ]
    # A track approaching outranks another leaving in the same event.
    leaving_scene = leaving_scene and not approaching_secure

    snapshot, subject_kind, place_class = snapshot_from_review(
        event, zone_classes=policy["zone_classes"],
        nobody_home=nobody_home, night=night, dwell_exceeded=dwell_exceeded,
    )
    snapshot = replace(
        snapshot, approaching_secure=approaching_secure,
        leaving_scene=leaving_scene, moving_fast=moving_fast,
    )
    ladder_result = evaluate_ladder_explained(snapshot)
    level = ladder_result.level
    decision_stage = ladder_result.stage
    decision_modifiers = list(ladder_result.modifiers)

    # Quiet hours: cap_quiet caps level at quiet (urgent exempt). SUPPRESSED
    # is not in ladder_policy.LEVELS -- a muted/suppressed snapshot has
    # nothing to cap, so it must skip this block rather than hit .index().
    qh_capped = False
    if qh_active and qh_mode == "cap_quiet" and level not in ("urgent", SUPPRESSED):
        from marcellus.push import ladder_policy
        quiet_idx = ladder_policy.LEVELS.index("quiet")
        level_idx = ladder_policy.LEVELS.index(level)
        if level_idx > quiet_idx:
            level = "quiet"
            qh_capped = True
            decision_modifiers.append("quiet_hours_cap")
    zone_name = event.zones[0] if event.zones else ""
    track_ids = event.track_ids or (event.event_id,)

    # Build reasons for decision trace.
    from marcellus.push import ladder_policy as _lp
    _zone_override_hit = bool(
        _lp.ZONE_OVERRIDES.get(snapshot.zone, {}).get(snapshot.subject)
    )
    trace_reasons: list[str] = []
    if _zone_override_hit:
        trace_reasons.append("zone_override")
    else:
        trace_reasons.append("routing_table")
    if qh_capped:
        trace_reasons.append("quiet_hours_cap")
    if approaching_secure:
        trace_reasons.append("approaching")
        if nearest_ft is not None and nearest_ft <= 100:
            trace_reasons.append(f"dist_{nearest_ft}ft")
    if leaving_scene:
        trace_reasons.append("leaving")
    if moving_fast:
        trace_reasons.append("running")

    mutated = 0
    for track_id in track_ids:
        card_key, existing, owning_camera, via_geo = _resolve_card_for_track(
            conn, camera=event.camera, track_id=track_id, subject_kind=subject_kind,
            zone_name=zone_name, zones=event.zones, now=now,
            geo_mates=geo_members.get(track_id),
            geo_enabled=bool(policy.get("geometric_dedup")),
        )
        card, mutation, sound = _advance_card(existing, level, card_key=card_key, now=now)
        if _zone_override_hit:
            # Sticky for the story's lifetime -- `card_store.upsert_card`
            # ORs this with whatever's already on the row, so a single hit
            # anywhere in the story keeps the resolve push non-ephemeral.
            card.zone_override_hit = True
        if via_geo and "geo_dedup" not in trace_reasons:
            trace_reasons.append("geo_dedup")

        mutation_name = {
            CREATE: "create", ESCALATE: "escalate", DEESCALATE: "deescalate",
        }.get(mutation, "")
        if mutation in (CREATE, ESCALATE, DEESCALATE):
            decision_trace.append(
                conn,
                camera=event.camera,
                label=snapshot.label,
                subject=subject_kind,
                zones=list(event.zones) if event.zones else [],
                place=place_class,
                level=card.level,
                reasons=list(trace_reasons),
                event_id=event.event_id,
                stage=decision_stage,
                modifiers=tuple(decision_modifiers),
                card_key=card_key,
                mutation=mutation_name,
                zone=snapshot.zone,
                sound=sound,
            )
        elif (
            mutation == SUPPRESSED_MUTATION
            and (existing is None or not existing.closed)
            and (snapshot.subject, snapshot.place) in _lp.OFF_CELLS
        ):
            # An `off` cell silenced this before evaluation -- trace it
            # anyway (level "off", once per track: later updates find the
            # closed row above and skip). Without this, Recent Decisions
            # shows nothing for exactly the cells the user silenced, so
            # there's no evidence trail to ever dial one back up. Global
            # mute also lands on SUPPRESSED but is excluded by the
            # OFF_CELLS check -- muting everything shouldn't flood the
            # trace.
            decision_trace.append(
                conn,
                camera=event.camera,
                label=snapshot.label,
                subject=subject_kind,
                zones=list(event.zones) if event.zones else [],
                place=place_class,
                level="off",
                reasons=["routing_table", "suppressed"],
                event_id=event.event_id,
                stage="off_cell",
                modifiers=(),
                card_key=card_key,
                mutation="suppressed",
                zone=snapshot.zone,
                sound=False,
            )

        # RESOLVE copy shows the story duration (matches the LA's frozen
        # timer); live mutations show time-in-current-state.
        elapsed = max(
            0.0, now - (card.created_at if mutation == RESOLVE else card.state_since_at)
        )
        identity = event.sub_labels[0] if event.sub_labels else ""
        if not identity and engine is not None:
            identity = getattr(engine, "_sub_labels", {}).get(
                (event.camera, track_id), ""
            )
        # Notable verbs only (content design 2026-08-15): one story phrase
        # when something is actually happening, silence otherwise.
        if mutation == RESOLVE:
            story = f"left after {_fmt_elapsed(elapsed)}"
        elif approaching_secure:
            story = _approach_story(nearest_ft, speed_words)
        elif moving_fast:
            story = "moving fast"
        elif dwell_exceeded:
            story = "still there"
        elif leaving_scene:
            story = "leaving"
        else:
            story = ""
        primary, secondary = _copy(
            subject_kind, snapshot.label, owning_camera, zone_name,
            0.0 if mutation == RESOLVE else elapsed,
            identity=identity, story=story,
        )
        if owning_camera != event.camera:
            # A second camera is now contributing to a card it didn't
            # create -- surface that in the copy rather than silently
            # merging (docs "Cross-camera deduplication" §2). The card's
            # own `camera` field stays the original, first-seen camera
            # (below) so the app's timeline routing is unaffected.
            secondary = f"{secondary} · also on {event.camera.replace('_', ' ').title()}"

        # mute_sounds / quiet-hours mute_sounds mode: omit sound entirely
        # (urgent exempt from quiet-hours mute).
        if muted:
            sound = False
        if qh_active and qh_mode == "mute_sounds" and card.level != "urgent":
            sound = False

        payload = None
        warm_task = None
        media_handle = None
        media = None
        if should_push(card.level):
            media_handle, media, warm_task = _media_for(
                mutation, event, conn=conn, engine=engine, config=config,
            )
        if media_handle:
            # Sticky, same shape as zone_override_hit above -- only
            # CREATE/ENRICH mint a fresh handle (`_MEDIA_MUTATIONS`), so an
            # escalate/resolve mutation leaves this untouched and
            # `card_store.upsert_card` falls back to whatever was last
            # persisted instead of blanking the story's thumbnail.
            card.media_handle = media_handle

        # Live Activities go first so the card push below knows whether an
        # LA demonstrably covers this mutation (start accepted by APNs, or
        # an update landed on a confirmed per-activity token). Only then is
        # the card push demoted to a silent NC entry -- an LA that failed
        # anywhere along the way leaves the normal banner intact.
        la_only = bool(policy["live_activities"].get("la_only", False))
        # la_only's catch-all is quiet+ by its own contract -- decoupled from
        # should_push, which no longer includes quiet (2026-08-14).
        # A "glance" outcome cell is la_only applied per-cell: the merged
        # ladder promises a Live Activity with no banner, so the catch-all
        # guarantees an activity for *uncurated* content (a quiet person in
        # the yard matches no family today). Content a curated family
        # claims runs under that family's own name -- and since the V3
        # subjects, those families are routed by the same ladder cell as
        # everything else, with opening picks as the one curation left.
        cell_glance = policy_settings.outcome_for(subject_kind, place_class) == "glance"
        if cell_glance and not la_only:
            native = live_activities.classify_family(
                subject_kind=subject_kind, label=snapshot.label,
                place_class=place_class, level=card.level,
            )
            if native is not None:
                cell_glance = False
        la_catch_all = (la_only or cell_glance) and card.level in ("quiet", "notify", "urgent")
        family = live_activities.should_start_activity(
            subject_kind=subject_kind, label=snapshot.label, place_class=place_class,
            level=card.level,
            opening_picks=policy["live_activities"].get("opening_picks"),
            opening_ids=(zone_name, owning_camera) if zone_name else (owning_camera,),
            # Catch-all only for cards that would have pushed at all --
            # log-level noise shouldn't mint activities.
            catch_all=la_catch_all,
        )
        if family == live_activities.CATCH_ALL:
            # Diagnose *why* the catch-all fired: distinguish "no curated
            # family matches this card at all" (ordinary la_only behavior,
            # nothing to log) from "a curated family matched but wasn't
            # eligible" (family toggled off, or openings picks didn't
            # match) -- the case this fallback exists for.
            native_family = live_activities.classify_family(
                subject_kind=subject_kind, label=snapshot.label, place_class=place_class,
                level=card.level,
            )
            if native_family is not None:
                logger.info(
                    "la: family=%s not_eligible -> fallback family=%s (la_only)",
                    native_family, live_activities.CATCH_ALL,
                )
        # §8 instrument fields — derived from the engine's track store.
        la_state_since_ts = round(card.state_since_at, 1) if card.state_since_at else None
        la_motion: dict[str, Any] | None = None
        la_zones = None
        la_path = None
        if engine is not None:
            track_state = engine.tracks.get(event.camera, track_id)
            if track_state is not None:
                la_motion = track_motion.get(track_id)
                # Same rounded/hysteresis distance the alert copy used, so
                # the LA chip and the notification never disagree. Additive
                # optional field per the LA contract; ≤100 ft or absent.
                if la_motion is not None and nearest_ft is not None and nearest_ft <= 100:
                    la_motion = {**la_motion, "distance_ft": nearest_ft}
                live_zones = tuple(
                    z for z in track_state.first_seen_in_zone if z
                )
                la_zones = _build_zones_ladder(
                    event.zones, live_zones, policy["zone_classes"],
                )
                la_path = _build_la_path(track_state.path_data)

        la_covered = await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=family, camera=owning_camera, subject_kind=subject_kind,
            label=snapshot.label, primary=primary, secondary=secondary,
            elapsed_seconds=int(elapsed), media_handle=media_handle, now=now,
            sound_allowed=sound,
            state_since_ts=la_state_since_ts, motion=la_motion,
            zones=la_zones, path=la_path,
        )
        # One alerts stack: the decisions feed carries the LA side of the
        # same decision. Only on the mutations that appended an entry above
        # -- annotating on enrich would overwrite the create entry's story.
        if mutation in (CREATE, ESCALATE, DEESCALATE):
            if family is None:
                # Would the content have matched a family at a loud-enough
                # cell? Distinguishes ladder/picks skips from plain
                # no-family content.
                content_family = live_activities.classify_family(
                    subject_kind=subject_kind, label=snapshot.label,
                    place_class=place_class, level="notify",
                )
                if content_family is None:
                    la_reason = "no_family"
                elif content_family == live_activities.OPENINGS and card.level != "log":
                    la_reason = "opening_not_picked"
                else:
                    la_reason = "cell_below_glance"
                decision_trace.annotate(
                    conn, event.event_id, la_started=False, la_reason=la_reason,
                )
            else:
                started = bool(la_covered)
                if not started:
                    la_reason = "device_not_la_capable"
                elif family == live_activities.CATCH_ALL:
                    la_reason = "catch_all"
                else:
                    la_reason = "started"
                decision_trace.annotate(
                    conn, event.event_id, family=family, la_started=started,
                    la_reason=la_reason,
                )
        # la_first demotion: if the delivery mode is la_first and this is
        # NOT an escalation, broaden demotion to all la_capable devices whose
        # (device-wide) Live Activity is open AND covers this card as one of
        # its eligible open stories — even if the LA update was
        # delta-suppressed this tick. Device-scoped (Elsinore Phase 4): the
        # LA is one per device now, so "covers this card" means the device
        # activity is open and this card independently qualifies as an
        # eligible story for that device, not a card-scoped row lookup.
        delivery_mode = policy["live_activities"].get("delivery", "la_first")
        is_escalation = mutation == ESCALATE and card.level in ("notify", "urgent")
        demote_tokens: set[str] = set(la_covered)
        if delivery_mode == "la_first" and not la_only and not is_escalation and family is not None:
            for device in devices:
                if device.apns_token in demote_tokens or not device.la_capable:
                    continue
                row = store.find_activity(conn, apns_token=device.apns_token)
                if (
                    row is not None and not row["ended_at"]
                    and _device_eligible(
                        device, camera=owning_camera, labels=(snapshot.label,),
                        card_level=card.level,
                    )
                    and not _is_snoozed(conn, device, owning_camera, now=now)
                ):
                    demote_tokens.add(device.apns_token)

        if demote_tokens:
            logger.info(
                "push: card push demoted to passive for %d device(s) — LA covers %s mutation=%s",
                len(demote_tokens), card_key, mutation,
            )

        # RESOLVE always builds a payload, even for a card whose level never
        # reached a pushable one (alerts-slice2 §E): `send_card_mutation`'s
        # own RESOLVE branch decides ephemeral vs. non-ephemeral off the
        # story's peak, but it can only do that if it gets a payload to mark
        # up in the first place. Every other mutation still gates on
        # `should_push`.
        if should_push(card.level) or mutation == RESOLVE:
            payload = build_card_payload(
                card, mutation, sound=sound and not la_only,
                subject_kind=subject_kind, place_class=place_class,
                label=snapshot.label, camera=owning_camera, zone_name=zone_name,
                glyph=_glyph_for(subject_kind, snapshot.label),
                primary=primary, secondary=secondary, event_ts=now, media=media,
                la_active=la_only, escalation_sound=escalation_sound,
            )

        sent_count = await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=subject_kind, place_class=place_class,
            camera=owning_camera, zone_name=zone_name,
            labels=event.labels, zones=event.zones, now=now,
            label=snapshot.label, family=family or "",
            demote_tokens=demote_tokens,
            suppress_demoted=delivery_mode == "la_first" and not la_only,
        )
        if mutation in (CREATE, ESCALATE, DEESCALATE):
            decision_trace.annotate(
                conn, event.event_id, sent=sent_count,
                sound=bool(sound and not la_only and sent_count > 0),
            )
        if warm_task is not None:
            # Runs concurrently with the sends above, not in series (plan §4
            # lever 4's rule, reused here): the push already carries the
            # `media` URL optimistically, so a slow or failed Frigate fetch
            # costs the notification its image, never its existence.
            with contextlib.suppress(Exception):
                await warm_task
        conn.commit()
        mutated += 1

    return mutated


def resound_payload_for(card: Card, context: dict[str, str]) -> dict[str, Any]:
    """`delivery.sweep_urgent_resound`'s `payload_for_resound` callback:
    rebuilds the same copy the card's context implies, sound forced on.
    Same shape as a live `escalate`, since a re-sound is exactly that from
    the app's point of view -- the card doesn't change level, but it does
    buzz again."""
    now = time.time()
    elapsed = max(0.0, now - card.state_since_at)
    subject_kind = context.get("subject_kind") or ""
    primary, secondary = _copy(
        subject_kind, "", context.get("camera", ""), context.get("zone_name", ""), elapsed,
        story="still there",
    )
    active = policy_settings.get_active()
    la_only = bool(active.get("live_activities", {}).get("la_only", False))
    return build_card_payload(
        card, "escalate", sound=not la_only, la_active=la_only,
        subject_kind=subject_kind, place_class=context.get("place_class", ""),
        label=context.get("label", ""), camera=context.get("camera", ""),
        zone_name=context.get("zone_name", ""),
        glyph=_glyph_for(subject_kind, ""), primary=primary, secondary=secondary, event_ts=now,
        escalation_sound=active.get("escalation_sound", "urgent"),
    )


async def handle_delivery_resolve(
    camera: str,
    track_id: str,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    zone_name: str = "",
    subject_kind: str = "",
    now: float | None = None,
) -> int:
    """A tracked object ended (`frigate/events` `msg_type == "end"`) -- the
    faster, authoritative resolution signal `advance_card`'s `resolved=True`
    needs (see `cards.classify_mutation`'s docstring on why a ladder level
    alone can't say this)."""
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now

    # This track was merged onto another camera's card (cross-camera dedup,
    # docs "Cross-camera deduplication") -- it was never the card's identity,
    # just one contributor among possibly several. Only the *original*
    # camera resolving (below, its own natural key) ends the card, even if
    # this one lingers a beat behind: dropping the alias quietly is correct
    # whether the owning camera is still tracking (this contributor just
    # stops enriching) or has already resolved too (the card is already
    # closing on its own path, and this track will get a fresh card of its
    # own if it's still detected afterward -- it will have moved zones by
    # then in the walk-through case that motivated dedup in the first place).
    _heading_streaks.pop((camera, track_id), None)
    # Same lifetime as the streaks: without this the announced-distance map
    # leaks one entry per track ever seen (nothing else deletes from it).
    _announced_distance.pop((camera, track_id), None)
    if card_store.get_track_alias(conn, camera, track_id) is not None:
        card_store.delete_track_alias(conn, camera, track_id)
        return 0

    # subject_kind isn't known from the object stream alone (no labels are
    # guaranteed to match what the review classified it as); resolve every
    # card keyed on this track id under any subject kind class this MVP uses.
    candidates = (
        [subject_kind] if subject_kind else list(_SUBJECT_GLYPH.keys())
    )
    resolved = 0
    for kind in candidates:
        card_key = build_card_key(camera=camera, subject_kind=kind, subject_id=track_id)
        existing = card_store.get_card(conn, card_key)
        if existing is None or existing.closed:
            continue
        card, mutation, sound = _advance_card(
            existing, existing.level, card_key=card_key, now=now, resolved=True,
        )
        elapsed = max(
            0.0, now - (card.created_at if mutation == RESOLVE else card.state_since_at)
        )
        story = f"left after {_fmt_elapsed(elapsed)}" if mutation == RESOLVE else ""
        primary, secondary = _copy(
            kind, "", camera, zone_name, 0.0 if mutation == RESOLVE else elapsed,
            story=story,
        )
        # End the LA first; if the end landed on a confirmed activity, the
        # resolve card push goes out quiet (NC text only, no banner).
        la_covered = await _deliver_live_activities(
            conn, devices, transport, config=config, card=card, mutation=mutation,
            family=None, camera=camera, subject_kind=kind, label="",
            primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
            media_handle=None, now=now, sound_allowed=sound,
            state_since_ts=round(card.state_since_at, 1) if card.state_since_at else None,
        )
        policy = policy_settings.get_active()
        la_only = bool(policy.get("live_activities", {}).get("la_only", False))
        delivery_mode = policy.get("live_activities", {}).get("delivery", "la_first")
        # RESOLVE is terminal for this card -- no future tick to catch a
        # delta-suppressed miss, so `la_covered` (an end or update that
        # demonstrably sent) is definitive; no broadening needed the way the
        # ongoing-mutation demotion above does.
        demote_resolve: set[str] = set(la_covered)
        payload = None
        if should_push(card.level) or mutation == RESOLVE:
            payload = build_card_payload(
                card, mutation, sound=sound and not la_only, subject_kind=kind,
                place_class="", camera=camera, zone_name=zone_name,
                glyph=_glyph_for(kind, ""),
                primary=primary, secondary=secondary, event_ts=now,
                la_active=la_only,
                escalation_sound=policy.get("escalation_sound", "urgent"),
            )
        await send_card_mutation(
            conn, transport, devices, card, mutation, payload,
            subject_kind=kind, camera=camera, zone_name=zone_name,
            now=now, demote_tokens=demote_resolve,
            suppress_demoted=delivery_mode == "la_first" and not la_only,
        )
        conn.commit()
        resolved += 1
    return resolved


def _relaxed_level(current_level: str, mode: str) -> str | None:
    """Compute the target level for a recognition relaxation. Returns the
    new level, or ``None`` if no change."""
    from marcellus.push import ladder_policy

    levels = ladder_policy.LEVELS
    idx = levels.index(current_level)
    if mode == "relax_one":
        target = max(0, idx - 1)
    elif mode == "relax_to_quiet":
        target = min(idx, levels.index("quiet"))
    else:
        return None
    return levels[target] if target < idx else None


async def handle_zone_transition(
    camera: str,
    track_id: str,
    current_zones: tuple[str, ...],
    *,
    label: str,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    engine: PushEngine | None = None,
    now: float | None = None,
) -> int:
    """A tracked object's zone set changed (`frigate/events`).

    Reviews own story *existence*; this hook exists because Frigate's review
    items go quiet on stationary objects — a person who loiters and drifts
    into hotter ground (driveway → charger) never gets a review update, so
    the review's frozen zone list kept a Restricted-zone loiter routed
    Semi-private (observed live 2026-08-14, person at 0.92 in `charger` for
    minutes with no escalation). The event stream knew within seconds.

    Escalation-only, by design: if the hottest current zone routes ABOVE the
    card's level, synthesize a review-shaped update through
    `handle_delivery_event` (which owns escalation, LA late-start, demotion,
    and sounds). Routing DOWN stays review-authoritative — an object stepping
    briefly onto cooler ground must not deescalate a story the review still
    considers hot.

    No card (alias or direct) → no-op: this hook never *creates* stories.
    """
    if not config.delivery_enabled or not current_zones:
        return 0
    now = time.time() if now is None else now

    probe = ReviewEvent(
        review_id=f"zone-transition:{camera}:{track_id}", camera=camera,
        severity="alert", labels=(label,) if label else (),
        track_ids=(track_id,), zones=tuple(current_zones),
    )
    subject_kind = classify_subject(probe)

    card_key = card_store.get_track_alias(conn, camera, track_id)
    if card_key is None:
        card_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    card = card_store.get_card(conn, card_key)
    if card is None or card.closed or card.resolved:
        return 0
    from marcellus.push import ladder_policy
    if card.level not in ladder_policy.LEVELS:
        return 0

    policy = policy_settings.get_active()
    zone_classes = policy["zone_classes"]

    # Rank each current zone by what it would route for this subject —
    # per-zone Snapshot so zone overrides apply, same authority as the
    # review path.
    best_zone: str | None = None
    best_idx = -1
    for zone in current_zones:
        place = zone_classes.get(zone) or policy_settings.guess_zone_class(zone)
        level = evaluate_ladder(Snapshot(
            subject=subject_kind, place=place, zone=zone,
            label=label, nobody_home=False, night=False,
            dwell_exceeded=False, muted=False,
        ))
        if level in ladder_policy.LEVELS:
            idx = ladder_policy.LEVELS.index(level)
            if idx > best_idx:
                best_idx = idx
                best_zone = zone

    if best_zone is None or best_idx <= ladder_policy.LEVELS.index(card.level):
        return 0

    logger.info(
        "push: zone transition escalates card=%s zone=%s (%s -> %s)",
        card.card_key, best_zone, card.level, ladder_policy.LEVELS[best_idx],
    )
    ordered = (best_zone, *(z for z in current_zones if z != best_zone))
    synthetic = ReviewEvent(
        review_id=f"zone-transition:{camera}:{track_id}", camera=camera,
        severity="alert", labels=(label,) if label else (),
        track_ids=(track_id,), zones=ordered,
    )
    return await handle_delivery_event(
        synthetic, conn=conn, devices=devices, transport=transport,
        config=config, engine=engine, now=now,
    )


async def handle_recognition_event(
    camera: str,
    track_id: str,
    sub_label: str,
    *,
    conn: sqlite3.Connection,
    devices: list[Device],
    transport: PushTransport,
    config: PushSection,
    label: str = "person",
    now: float | None = None,
) -> int:
    """A face sub_label or plate landed on a tracked object — check
    recognition settings and emit a silent deescalate if applicable.

    Identity-driven mutations are always silent: no sound, no LA alert,
    regardless of mute settings (design brief: "watching the instrument
    calm itself down is the feature")."""
    if not config.delivery_enabled:
        return 0
    now = time.time() if now is None else now
    policy = policy_settings.get_active()
    recognition = policy.get("recognition", {})

    if label == "person" or label in _ANIMAL_LABELS:
        mode = recognition.get("known_person", "off")
        subject_kind = "person"
    elif label in _VEHICLE_LABELS:
        mode = recognition.get("known_vehicle", "off")
        subject_kind = "vehicle"
    else:
        return 0

    if mode == "off" or not sub_label:
        return 0

    card_key = build_card_key(camera=camera, subject_kind=subject_kind, subject_id=track_id)
    existing = card_store.get_card(conn, card_key)
    if existing is None or existing.closed or existing.resolved:
        return 0

    target_level = _relaxed_level(existing.level, mode)
    if target_level is None:
        return 0

    card, mutation, _sound = _advance_card(
        existing, target_level, card_key=card_key, now=now,
    )
    if mutation != DEESCALATE:
        return 0

    context = card_store.get_card_context(conn, card_key) or {}

    decision_trace.append(
        conn,
        camera=camera,
        label=label,
        subject=subject_kind,
        zones=[],
        place=context.get("place_class", ""),
        level=card.level,
        reasons=["recognition_relax"],
        event_id=f"{camera}:{track_id}",
        stage="table",
        modifiers=("nudge_down",),
        card_key=card_key,
        mutation="deescalate",
        zone=context.get("zone_name", ""),
        sound=False,
    )
    zone_name = context.get("zone_name", "")
    place_class = context.get("place_class", "")
    owning_camera = context.get("camera", camera)
    elapsed = max(0.0, now - card.state_since_at)
    primary, secondary = _copy(
        subject_kind, label, owning_camera, zone_name, elapsed, identity=sub_label,
    )
    # The deescalated level may have dropped this card out of (or, in
    # principle, kept it in) LA eligibility -- re-derive rather than passing
    # `family=None`, which would otherwise stomp the card's stored `family`
    # to empty on every recognition relax regardless of the new level.
    relaxed_family = live_activities.classify_family(
        subject_kind=subject_kind, label=label, place_class=place_class, level=card.level,
    )

    la_covered = await _deliver_live_activities(
        conn, devices, transport, config=config, card=card, mutation=mutation,
        family=relaxed_family, camera=owning_camera, subject_kind=subject_kind, label=label,
        primary=primary, secondary=secondary, elapsed_seconds=int(elapsed),
        media_handle=None, now=now, sound_allowed=False,
        state_since_ts=round(card.state_since_at, 1) if card.state_since_at else None,
        place_class=place_class,
    )
    demote_recog: set[str] = set(la_covered)
    recog_policy = policy_settings.get_active()
    recog_la_only = bool(recog_policy.get("live_activities", {}).get("la_only", False))
    recog_delivery = recog_policy.get("live_activities", {}).get("delivery", "la_first")
    recog_family = context.get("family") or None
    if recog_delivery == "la_first" and not recog_la_only and recog_family is not None:
        for device in devices:
            if device.apns_token in demote_recog or not device.la_capable:
                continue
            row = store.find_activity(conn, apns_token=device.apns_token)
            if (
                row is not None and not row["ended_at"]
                and _device_eligible(
                    device, camera=owning_camera, labels=(context.get("label", ""),),
                    card_level=card.level,
                )
                and not _is_snoozed(conn, device, owning_camera, now=now)
            ):
                demote_recog.add(device.apns_token)
    payload = None
    if should_push(card.level):
        payload = build_card_payload(
            card, mutation, sound=False, subject_kind=subject_kind,
            place_class=place_class, label=label, camera=owning_camera,
            zone_name=zone_name, glyph=_glyph_for(subject_kind, label),
            primary=primary, secondary=secondary, event_ts=now,
            la_active=True,
            escalation_sound=recog_policy.get("escalation_sound", "urgent"),
        )
    _recog_la = recog_policy.get("live_activities", {})
    await send_card_mutation(
        conn, transport, devices, card, mutation, payload,
        subject_kind=subject_kind, place_class=place_class,
        camera=owning_camera, zone_name=zone_name, now=now,
        label=label, family=relaxed_family or "",
        demote_tokens=demote_recog,
        suppress_demoted=_recog_la.get("delivery", "la_first") == "la_first"
        and not _recog_la.get("la_only", False),
    )
    conn.commit()
    logger.info(
        "recognition: %s on %s:%s -> deescalate %s->%s (mode=%s)",
        sub_label, camera, track_id, existing.level, target_level, mode,
    )
    return 1
