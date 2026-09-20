"""Situations: the Phase-1 primitive that replaces "a review item fired".

`notification-experience-plan-2026-08-05.md` §1 reframes the unit of
notification from an event to *a situation is happening* -- a user-authored
rule over Frigate primitives (camera + label + zone + loiter + time-of-day)
naming a thing worth being told about. Everything not matching a situation is
silent as far as push is concerned; it still appears in the reel and the
digests, because this is a *notification* filter, not a visibility filter.

Three things live here, all dependency-free so they unit-test without a DB,
an app, or a broker:

* `Situation` -- the §8 schema object, parsed and defaulted.
* `TrackStore` -- per-track state, the only way to derive loiter (see below).
* `evaluate_device` -- which of a device's situations a review message fires.

**Why loiter needs state.** `frigate/reviews` carries no per-zone dwell
duration on the wire (plan §8, "Per-track state"); a review message says
*which* zones the item has ever touched, never *for how long*. Dwell is
therefore derived: remember when each `(camera, track_id)` was first seen in
each zone and subtract later. Track ids are per-Frigate-lifetime, so the
store is wiped on every MQTT reconnect.

**Where the "later" comes from.** The plan has dwell advanced by subsequent
`frigate/reviews` `type: update` messages. Measured against this deployment
(19.6 min of live traffic, 2026-08-05) that topic published 4 messages total:
two review items, each a `new` and an `end` ~30s apart, with **no `update`
in between**. Frigate publishes a review update when the item's *data*
changes -- a new object, a new zone, a severity promotion -- not on a clock,
so a person standing still is precisely the case that generates no traffic. A
loiter threshold fed only by that topic would never be re-evaluated and never
fire.

`frigate/events` publishes the same objects every ~0.2-0.5s (2031 messages
over the same window) and carries `current_zones` -- live occupancy, which
*drops* a zone on exit, unlike the review topic's cumulative `zones`. So the
store takes dwell from there: entry timestamps that reset when the object
actually leaves, and a tick to re-evaluate against. `frigate/reviews` remains
the sole authority on *whether* something is push-worthy; the object stream
only answers "still there, and for how long". `dwell_source="reviews"`
restores the literal prescribed behaviour for anyone who wants it.

**Phase 1 delivers the Interrupt tier only.** `present`/`ambient` situations
parse, persist, and evaluate, but have no delivery surface until Live
Activities (Phase 2) and widgets (Phase 3) exist -- the plan's §2 tier table
is explicit that the tier decides *how* a situation reaches the user, and the
"how" for those two isn't built. They are logged once and dropped rather than
silently upgraded into a buzz, which would violate the plan's smallest-surface
non-negotiable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from marcellus.push.models import Device, ReviewEvent

logger = logging.getLogger(__name__)

TIERS = ("ambient", "present", "interrupt")

#: Live Activity stages (Phase 2 plan, `ContentState.stage`). They drive the
#: app's visual language, so the strings are a wire contract, not an internal
#: enum: minimal while `arriving`, timer + snapshot at `present`, red-badged
#: once `escalated`, fading at `ending`.
STAGE_ARRIVING = "arriving"
STAGE_PRESENT = "present"
STAGE_ESCALATED = "escalated"
STAGE_ENDING = "ending"
STAGES = (STAGE_ARRIVING, STAGE_PRESENT, STAGE_ESCALATED, STAGE_ENDING)


@dataclass(frozen=True)
class Escalation:
    """When a Present situation crosses the interrupt bar (plan §2).

    `on` is one of the plan §8 forms:

    * `loiter_exceeds:<seconds>` -- dwell reached the bar
    * `audio_event`             -- one of the situation's `audio_events` fired
    * `sub_label_unknown`       -- Frigate recognised no face/plate for the
                                   track (Phase 5 owns the allow/deny lists;
                                   this trigger is the plumbing landing early,
                                   per the Phase 2 plan's own note)
    """

    from_tier: str = "present"
    to_tier: str = "interrupt"
    kind: str = ""  # "loiter_exceeds" | "audio_event" | "sub_label_unknown"
    threshold: float = 0.0

    @classmethod
    def from_dict(cls, raw: Any) -> Escalation | None:
        if not isinstance(raw, dict):
            return None
        on = str(raw.get("on") or "").strip()
        if not on:
            return None
        kind, _, value = on.partition(":")
        kind = kind.strip()
        if kind not in ("loiter_exceeds", "audio_event", "sub_label_unknown"):
            logger.info("push: ignoring unrecognised escalation trigger %r", on)
            return None
        try:
            threshold = float(value) if value else 0.0
        except ValueError:
            threshold = 0.0
        return cls(
            from_tier=str(raw.get("from_tier") or "present"),
            to_tier=str(raw.get("to_tier") or "interrupt"),
            kind=kind,
            threshold=threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        on = f"{self.kind}:{self.threshold:g}" if self.kind == "loiter_exceeds" else self.kind
        return {"from_tier": self.from_tier, "to_tier": self.to_tier, "on": on}


def _str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if v is not None and str(v) != "")


@dataclass(frozen=True)
class Situation:
    """One rule from the registration record's `situations` array (plan §8).

    Fields below the divider are accepted, persisted, and deliberately not
    read by `matches()` this phase -- they belong to handoffs that haven't
    happened yet. Parsing them now means the app can ship the full §8 shape
    before the sidecar acts on all of it.
    """

    id: str
    name: str = ""
    tier: str = "interrupt"
    cameras: tuple[str, ...] = ()  # () = every camera
    labels: tuple[str, ...] = ()  # () = every label
    zones: tuple[str, ...] = ()  # () = anywhere on the camera
    loiter_seconds: float = 0.0
    audio_events: tuple[str, ...] = ()
    # (start_hour, end_hour); wraps across midnight when start > end. None = any.
    time_of_day: tuple[int, int] | None = None
    sound: str = ""

    #: Phase 2: when a Present-tier situation crosses the interrupt bar.
    escalation: Escalation | None = None
    #: Phase 2 (plan §4 lever 5): also start the activity on Frigate
    #: `detection`-severity reviews, ~500ms ahead of the `alert` promotion.
    detection_tier_early_fire: bool = False

    # -- accepted and ignored (see module docstring) --
    require_stationary: bool = False
    sub_label_allow: tuple[str, ...] = ()
    sub_label_deny: tuple[str, ...] = ()
    night_tightening: bool = False
    llm_enrich: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Situation | None:
        """Parse one situation object, or None if it has no usable id.

        Tolerant on purpose: a device's registration is a long-lived record
        written by an app version that may be newer than this sidecar, so an
        unrecognised field is ignored rather than fatal. A missing `id` is the
        one thing that can't be defaulted -- it keys the collapse id, the
        rate-limit window, and the snooze scope.
        """
        if not isinstance(raw, dict):
            return None
        sid = str(raw.get("id") or "").strip()
        if not sid:
            return None

        tier = str(raw.get("tier") or "interrupt").strip().lower()
        if tier not in TIERS:
            tier = "interrupt"

        tod: tuple[int, int] | None = None
        raw_tod = raw.get("time_of_day")
        if isinstance(raw_tod, dict):
            try:
                start = int(raw_tod["start_hour"]) % 24
                end = int(raw_tod["end_hour"]) % 24
            except (KeyError, TypeError, ValueError):
                tod = None
            else:
                tod = (start, end)

        try:
            loiter = max(0.0, float(raw.get("loiter_seconds") or 0.0))
        except (TypeError, ValueError):
            loiter = 0.0

        return cls(
            id=sid,
            name=str(raw.get("name") or sid),
            tier=tier,
            cameras=_str_tuple(raw.get("cameras")),
            labels=_str_tuple(raw.get("labels")),
            zones=_str_tuple(raw.get("zones")),
            loiter_seconds=loiter,
            audio_events=_str_tuple(raw.get("audio_events")),
            time_of_day=tod,
            sound=str(raw.get("sound") or ""),
            require_stationary=bool(raw.get("require_stationary")),
            sub_label_allow=_str_tuple(raw.get("sub_label_allow")),
            sub_label_deny=_str_tuple(raw.get("sub_label_deny")),
            night_tightening=bool(raw.get("night_tightening")),
            escalation=Escalation.from_dict(raw.get("escalation")),
            llm_enrich=bool(raw.get("llm_enrich")),
            detection_tier_early_fire=bool(raw.get("detection_tier_early_fire")),
        )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip back to the §8 wire shape (used by the starter library)."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "tier": self.tier,
            "cameras": list(self.cameras),
            "labels": list(self.labels),
            "zones": list(self.zones),
            "loiter_seconds": self.loiter_seconds,
            "require_stationary": self.require_stationary,
            "sub_label_allow": list(self.sub_label_allow),
            "sub_label_deny": list(self.sub_label_deny),
            "audio_events": list(self.audio_events),
            "night_tightening": self.night_tightening,
            "llm_enrich": self.llm_enrich,
            "detection_tier_early_fire": self.detection_tier_early_fire,
            "sound": self.sound,
        }
        if self.time_of_day is not None:
            out["time_of_day"] = {
                "start_hour": self.time_of_day[0], "end_hour": self.time_of_day[1]
            }
        if self.escalation is not None:
            out["escalation"] = self.escalation.to_dict()
        return out


def parse_situations(raw: Any) -> tuple[Situation, ...]:
    if not isinstance(raw, list):
        return ()
    parsed = [Situation.from_dict(item) for item in raw]
    return tuple(s for s in parsed if s is not None)


def _local_hour(now: float, tz_name: str) -> int:
    """Hour-of-day at the *device's* location (plan §7: "7am" is local, not
    sidecar-local). Falls back to the sidecar's own clock when the device
    didn't send a timezone or sent one this host has no data for."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return datetime.fromtimestamp(now, ZoneInfo(tz_name)).hour
        except Exception:  # noqa: BLE001 - unknown tz must not drop the push
            logger.debug("push: unusable timezone %r, using sidecar-local hour", tz_name)
    return datetime.fromtimestamp(now).hour


def in_time_window(window: tuple[int, int] | None, now: float, tz_name: str) -> bool:
    """`time_of_day` membership, wrapping across midnight when start > end
    (plan §8's schema note)."""
    if window is None:
        return True
    start, end = window
    if start == end:
        return True
    hour = _local_hour(now, tz_name)
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


@dataclass
class TrackState:
    """Everything the sidecar holds about one `(camera, track_id)`."""

    first_seen_in_zone: dict[str, float] = field(default_factory=dict)
    last_update_at: float = 0.0
    #: `(apns_token, situation_id)` pairs already pushed for this track.
    #:
    #: The handoff specifies a bare `tier_fired: bool`. A bool is one bit for
    #: what is really a per-(device, situation) fact: with two phones
    #: registered, whichever one was evaluated first would consume the flag and
    #: the second would never be told; likewise a track matching both
    #: `at-the-door` and `near-my-car` would only ever fire one of them. The
    #: intent -- "the same dwell doesn't fire twice" -- is preserved exactly,
    #: at the granularity that intent actually lives at.
    fired: set[tuple[str, str]] = field(default_factory=set)
    #: `(apns_token, situation_id) -> stage`, for Present-tier situations with
    #: a Live Activity in flight. Per device as well as per situation because
    #: each device runs its own activity for the same real-world moment.
    stages: dict[tuple[str, str], str] = field(default_factory=dict)
    #: §8 instrument fields, updated from frigate/events.
    path_data: list[tuple[float, float, float]] = field(default_factory=list)
    velocity_angle: float | None = None
    average_estimated_speed: float | None = None
    stationary: bool = False
    #: Frigate's object label ("person", "car", ...) — kept so the live map
    #: can color/filter fused positions without re-parsing MQTT.
    label: str | None = None


class TrackStore:
    """In-memory `(camera, track_id) -> TrackState`, reaped and wiped.

    In memory rather than in SQLite on purpose: every entry is invalidated by
    the two events that also end the process's knowledge of them -- a Frigate
    restart (new track ids) and a sidecar restart -- so persisting would only
    preserve rows that are already meaningless.
    """

    #: Handoff item 8: drop entries whose last update is older than this.
    reap_after_s: float = 600.0

    def __init__(self) -> None:
        self._tracks: dict[tuple[str, str], TrackState] = {}

    def __len__(self) -> int:
        return len(self._tracks)

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._tracks

    def get(self, camera: str, track_id: str) -> TrackState | None:
        return self._tracks.get((camera, track_id))

    def items(self) -> list[tuple[tuple[str, str], TrackState]]:
        """Snapshot of all live tracks, for the fusion/live-map readers."""
        return list(self._tracks.items())

    def observe(self, event: ReviewEvent, *, now: float) -> None:
        """Record this message's zones against every track it mentions.

        Zone membership on `frigate/reviews` is cumulative -- Frigate adds a
        zone to `data.zones` when the item enters it and never removes it --
        so the *first* message carrying a zone is the closest thing to an
        entry timestamp that exists on this topic.

        For the first message of a track we prefer the review's own
        `start_time` over arrival time: the item may have been dwelling for a
        second or two before Frigate promoted it to a review, and counting
        that dwell is both more accurate and faster to the user. The value is
        clamped so a clock skew between Frigate and the sidecar can't
        manufacture dwell that never happened.
        """
        for track_id in event.track_ids or (event.review_id,):
            key = (event.camera, track_id)
            state = self._tracks.get(key)
            if state is None:
                state = TrackState()
                self._tracks[key] = state
                entered = _clamp_start(event.start_time, now)
            else:
                entered = now
            for zone in event.zones:
                state.first_seen_in_zone.setdefault(zone, entered)
            # A zoneless camera still needs a dwell origin, or a situation with
            # no `zones` and a non-zero loiter could never qualify.
            state.first_seen_in_zone.setdefault("", entered)
            state.last_update_at = now

    def observe_object(
        self,
        camera: str,
        track_id: str,
        current_zones: tuple[str, ...],
        *,
        now: float,
        path_data: tuple[tuple[float, float, float], ...] = (),
        velocity_angle: float | None = None,
        average_estimated_speed: float | None = None,
        stationary: bool = False,
        label: str | None = None,
    ) -> None:
        """Record live occupancy from a `frigate/events` message.

        `current_zones` is the object's zones *right now* -- Frigate removes
        one when the object leaves, which the review topic never does. That
        makes this the only signal that can tell "stood at the door for six
        seconds" from "crossed the porch twice", so a zone dropping out clears
        its entry timestamp and the next entry starts a fresh dwell.
        """
        key = (camera, track_id)
        state = self._tracks.get(key)
        if state is None:
            state = TrackState()
            self._tracks[key] = state
        for zone in current_zones:
            state.first_seen_in_zone.setdefault(zone, now)
        for zone in [z for z in state.first_seen_in_zone if z and z not in current_zones]:
            del state.first_seen_in_zone[zone]
        # The camera-level origin ("" = seen at all) is never cleared by a
        # zone exit: an object that left the porch is still on the doorbell
        # camera, and a zoneless situation is asking about exactly that.
        state.first_seen_in_zone.setdefault("", now)
        state.last_update_at = now
        if path_data:
            state.path_data = list(path_data)
        state.velocity_angle = velocity_angle
        state.average_estimated_speed = average_estimated_speed
        state.stationary = stationary
        if label:
            state.label = label

    def forget(self, camera: str, track_id: str) -> None:
        """Drop a track outright -- Frigate says the object is gone."""
        self._tracks.pop((camera, track_id), None)

    def dwell_origin(
        self, camera: str, track_id: str, zones: tuple[str, ...]
    ) -> float | None:
        """When this track's current stay began, or None if it isn't there.

        None is the load-bearing case: with live occupancy available, a zone
        the object has left has no entry timestamp at all, which is how a
        walk-through is told apart from a stay without asking the caller to
        reason about it.
        """
        state = self._tracks.get((camera, track_id))
        if state is None:
            return None
        candidates = (
            [state.first_seen_in_zone[z] for z in zones if z in state.first_seen_in_zone]
            if zones
            else list(state.first_seen_in_zone.values())
        )
        return min(candidates) if candidates else None

    def dwell_s(self, camera: str, track_id: str, zones: tuple[str, ...], *, now: float) -> float:
        """Seconds this track has been in whichever of `zones` it entered
        earliest. `zones` empty means "anywhere on the camera"."""
        origin = self.dwell_origin(camera, track_id, zones)
        return 0.0 if origin is None else max(0.0, now - origin)

    def mark_fired(self, camera: str, track_id: str, apns_token: str, situation_id: str) -> None:
        state = self._tracks.get((camera, track_id))
        if state is not None:
            state.fired.add((apns_token, situation_id))

    def has_fired(self, camera: str, track_id: str, apns_token: str, situation_id: str) -> bool:
        state = self._tracks.get((camera, track_id))
        return bool(state and (apns_token, situation_id) in state.fired)

    def stage(self, camera: str, track_id: str, apns_token: str, situation_id: str) -> str | None:
        state = self._tracks.get((camera, track_id))
        if state is None:
            return None
        return state.stages.get((apns_token, situation_id))

    def set_stage(
        self, camera: str, track_id: str, apns_token: str, situation_id: str, stage: str
    ) -> None:
        state = self._tracks.get((camera, track_id))
        if state is not None:
            state.stages[(apns_token, situation_id)] = stage

    def reap(self, *, now: float) -> int:
        """Drop tracks untouched for `reap_after_s` (handoff item 8)."""
        stale = [k for k, v in self._tracks.items() if now - v.last_update_at > self.reap_after_s]
        for key in stale:
            del self._tracks[key]
        return len(stale)

    def clear(self) -> None:
        """Wipe everything -- called on every MQTT reconnect, because a
        Frigate restart reissues track ids and any held state would then
        describe the wrong object (handoff item 8)."""
        self._tracks.clear()


def _clamp_start(start_time: float, now: float, *, max_backdate_s: float = 300.0) -> float:
    """`start_time` if it is a sane recent past, else `now`."""
    if start_time <= 0 or start_time > now:
        return now
    if now - start_time > max_backdate_s:
        return now
    return start_time


#: APNs' `apns-collapse-id` ceiling, enforced by Apple and mirrored by the
#: relay. Not a soft limit: a longer value is truncated, not rejected.
COLLAPSE_ID_MAX_BYTES = 64


def build_collapse_id(situation_id: str, track_id: str) -> str:
    """`<situation-id>:<track-id>`, trimmed to fit APNs' 64-byte cap.

    The track id is kept whole and the situation id is shortened to make
    room, because collapsing is keyed on the *track* -- see `Match.collapse_id`
    for why losing the track id would be the expensive way to lose.
    """
    full = f"{situation_id}:{track_id}"
    if len(full.encode()) <= COLLAPSE_ID_MAX_BYTES:
        return full
    room = COLLAPSE_ID_MAX_BYTES - len(track_id.encode()) - 1
    if room <= 0:
        # A track id alone at the ceiling: nothing left to qualify it with,
        # so collapse on the track and accept that one pathological situation
        # id can share it.
        logger.warning(
            "push: track id %r leaves no room for a situation id in a 64-byte "
            "collapse id; collapsing on the track alone", track_id,
        )
        return track_id.encode()[:COLLAPSE_ID_MAX_BYTES].decode(errors="ignore")
    return f"{situation_id.encode()[:room].decode(errors='ignore')}:{track_id}"


@dataclass(frozen=True)
class Match:
    """One situation firing for one track -- everything the push needs."""

    situation: Situation
    track_id: str
    dwell_s: float
    label: str
    zone: str
    audio: str = ""

    @property
    def collapse_id(self) -> str:
        """`<situation-id>:<track-id>` (plan §3 / §8). Same track's updates
        replace rather than stack; distinct tracks stay distinct notifications
        by design.

        Capped at APNs' 64-byte ceiling, which the relay also enforces by
        truncating (`elsinore-push-relay` 4278bdf). Truncating from the right
        would cut into the *track* id -- and two tracks whose ids no longer
        differ are one notification replacing another, silently losing the
        "two people arriving is two notifications" property. So the situation
        id gives way instead: the track id is the part that has to survive
        intact, and the result is still stable across a track's updates,
        which is all a collapse id has to be.
        """
        return build_collapse_id(self.situation.id, self.track_id)


def matches(
    situation: Situation,
    device: Device,
    event: ReviewEvent,
    track_id: str,
    tracks: TrackStore,
    *,
    now: float,
) -> Match | None:
    """Does `situation` fire for this `(event, track_id)` on this device?

    Returns the `Match` (carrying the dwell and the label that qualified, both
    of which the body text needs) or None. Pure apart from reading `tracks`;
    snooze and rate limiting are the engine's, deliberately not folded in here
    so "did the rule match" and "should we interrupt anyway" stay separable.
    """
    if situation.cameras and event.camera not in situation.cameras:
        return None

    label = _first_match(situation.labels, event.labels)
    if situation.labels and label is None:
        return None

    if not in_time_window(situation.time_of_day, now, device.timezone):
        return None

    # An audio event is instantaneous by nature -- a doorbell ring has no
    # dwell to wait for, and holding it back for `loiter_seconds` would be the
    # wrong answer to "someone is at the door". Checked before the zone gate
    # because plan §1 authors `at-the-door` as exactly this OR: person in the
    # porch zone, *or* a doorbell audio event -- a ring qualifies on its own.
    # Note the asymmetry with the camera/label/zone filters above: an empty
    # `audio_events` means this situation is *not about* audio, not "any
    # audio". Reusing `_first_match` here would hand back "" for every
    # situation that never mentioned sound and fire it on the spot, zone and
    # loiter unread.
    audio = (
        next((a for a in event.audio if a in situation.audio_events), None)
        if situation.audio_events
        else None
    )
    if audio is not None:
        return Match(
            situation=situation,
            track_id=track_id,
            dwell_s=tracks.dwell_s(event.camera, track_id, situation.zones, now=now),
            label=label or (event.labels[0] if event.labels else ""),
            zone="",
            audio=audio,
        )

    zone = _first_match(situation.zones, event.zones)
    if situation.zones and zone is None:
        return None

    origin = tracks.dwell_origin(event.camera, track_id, situation.zones)
    if origin is None and situation.zones:
        # The review topic's `zones` is cumulative -- it says the object was
        # in the zone at some point, never that it still is. With live
        # occupancy available the absence of an entry timestamp means it has
        # left, and a situation about a zone should not fire about somewhere
        # the object no longer is.
        return None

    dwell = 0.0 if origin is None else max(0.0, now - origin)
    if situation.loiter_seconds and dwell < situation.loiter_seconds:
        # Not yet -- re-evaluated on the next message about this track. This
        # is the single biggest filter in the whole design (plan §1): a person
        # walking past hits the frame but doesn't dwell.
        return None

    return Match(
        situation=situation,
        track_id=track_id,
        dwell_s=dwell,
        label=label or (event.labels[0] if event.labels else ""),
        zone=zone or "",
    )


def _first_match(wanted: tuple[str, ...], present: tuple[str, ...]) -> str | None:
    """The first of `present` that `wanted` admits; None if none do. An empty
    `wanted` means "anything", answered with the first present value (or `""`
    when the event carries none) so callers can still name what qualified."""
    if not wanted:
        return present[0] if present else ""
    for value in present:
        if value in wanted:
            return value
    return None


def evaluate_device(
    device: Device,
    event: ReviewEvent,
    tracks: TrackStore,
    *,
    now: float,
    tiers: tuple[str, ...] = ("interrupt",),
) -> list[Match]:
    """Every situation of `device` that fires an *alert* for `event`.

    A review item can carry several tracked objects; each is evaluated
    separately so two people arriving 30s apart become two notifications
    rather than one collapsed blur (they have distinct track ids, hence
    distinct collapse ids).

    `tiers` defaults to interrupt-only, which is the alert path. Present-tier
    situations are normally driven by the Live Activity state machine instead
    -- but a device with no push-to-start token has no activity to drive, and
    the engine passes `("interrupt", "present")` to fall those back onto this
    path (Phase 2 handoff item 9, "the app works without Phase 2").
    """
    found: list[Match] = []
    track_ids = event.track_ids or (event.review_id,)
    for situation in device.situations:
        if situation.tier not in tiers:
            continue
        for track_id in track_ids:
            if tracks.has_fired(event.camera, track_id, device.apns_token, situation.id):
                continue
            hit = matches(situation, device, event, track_id, tracks, now=now)
            if hit is not None:
                found.append(hit)
    return found
