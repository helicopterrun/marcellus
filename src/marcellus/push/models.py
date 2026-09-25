"""Plain dataclasses shared across the push-notification pipeline.

Kept dependency-free (no FastAPI/pydantic imports) so `decision.py` stays
trivially unit-testable -- it never has to construct an app or a DB
connection to be exercised.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from marcellus.push.situations import Situation

SEVERITIES = ("detection", "alert")  # low -> high, matches Frigate's own field


@dataclass(frozen=True)
class ReviewEvent:
    """A `frigate/reviews` MQTT message, reduced to what the decision engine
    and handle minting need. Not the raw Frigate event id -- see `event_id`.
    """

    review_id: str
    camera: str
    severity: str  # "alert" | "detection"
    labels: tuple[str, ...] = field(default_factory=tuple)
    msg_type: str = "new"  # "new" | "update" | "end"
    # The Frigate *event* id this review item is about (from
    # `after.data.detections[0]`), distinct from `review_id` (`after.id`).
    # Falls back to `review_id` if Frigate ever sends a review with no
    # detections attached yet.
    event_id: str = ""

    # -- situation evaluation (notification-experience plan §8) --
    # `after.data.zones`: cumulative, every zone the item has *ever* entered.
    # Frigate never removes one, so this answers "was it there" and never
    # "is it still there" -- see push.situations for what that costs.
    zones: tuple[str, ...] = field(default_factory=tuple)
    # `after.data.detections`: the tracked-object ids behind this review item.
    # These are the track ids the per-track store and `apns-collapse-id` are
    # keyed on -- one review item can carry several, and each is a separate
    # thing that might be happening.
    track_ids: tuple[str, ...] = field(default_factory=tuple)
    audio: tuple[str, ...] = field(default_factory=tuple)  # `after.data.audio`
    sub_labels: tuple[str, ...] = field(default_factory=tuple)  # Phase 5
    start_time: float = 0.0  # `after.start_time`, the dwell origin
    # `after.end_time` -- only ever set on a `type: "end"` message (Frigate
    # leaves it null until the review item finalizes). encounters/service.py
    # prefers this real end over the live-hook's own wall clock when present.
    end_time: float | None = None

    def __post_init__(self) -> None:
        if not self.event_id:
            object.__setattr__(
                self, "event_id", self.track_ids[0] if self.track_ids else self.review_id
            )


@dataclass(frozen=True)
class TrackedObject:
    """A `frigate/events` message, reduced to what dwell needs.

    Never a push trigger on its own -- `frigate/reviews` stays the sole
    authority on whether something is worth notifying about (transport spec's
    "Architecture at a glance", unchanged). This carries the two facts that
    topic cannot: whether the object is *still* in a zone, and a tick often
    enough to notice it crossing a loiter threshold.
    """

    track_id: str  # `after.id` -- the same id `review.data.detections` lists
    camera: str
    label: str = ""
    #: Live occupancy. Frigate removes a zone when the object leaves it, which
    #: `review.data.zones` (cumulative) never does.
    current_zones: tuple[str, ...] = field(default_factory=tuple)
    entered_zones: tuple[str, ...] = field(default_factory=tuple)
    msg_type: str = "update"  # "new" | "update" | "end"
    stationary: bool = False  # Phase 5's `require_stationary`
    sub_label: str = ""  # Phase 5's sub_label allow/deny
    #: §8 instrument fields — may be absent depending on Frigate version.
    path_data: tuple[tuple[float, float, float], ...] = field(default_factory=tuple)
    velocity_angle: float | None = None
    average_estimated_speed: float | None = None


@dataclass(frozen=True)
class Device:
    """One registered push-notification device (`push_devices` row)."""

    apns_token: str
    device_id: str
    bundle_id: str
    environment: str  # "sandbox" | "prod"
    app_version: str = ""
    cameras: tuple[str, ...] = field(default_factory=tuple)  # () = all cameras
    labels: tuple[str, ...] = field(default_factory=tuple)  # () = all labels
    min_severity: str = "alert"

    # -- v2 registration (plan §8) --
    # 1 = the v1 camera+label+severity subscription; 2 = the situation shape.
    # Derived from the row, not trusted from the client: what actually decides
    # the evaluation path is whether `situations` is non-empty, and the two
    # must never disagree.
    schema_version: int = 1
    timezone: str = ""  # IANA name; "" = fall back to the sidecar's clock
    location: tuple[float, float] | None = None  # (lat, lon); Phase 5
    situations: tuple[Situation, ...] = field(default_factory=tuple)
    live_activity_token: str = ""  # superseded by push_to_start_token; unread
    #: Phase 2: one per app install, creates Live Activities. Empty means this
    #: device can't run them, and its Present-tier situations fall back to
    #: alert pushes ("the app works without Phase 2").
    push_to_start_token: str = ""
    la_capable: bool = True
    #: Phase A: fast (3s) vs default (15s) Live Activity update cadence.
    frequent_pushes_enabled: bool = False
    #: UniFi Protect doorbell ring -> push opt-out. On by default.
    doorbell_rings: bool = True
    #: M-2: this device's 3 doorbell-LCD quick-reply slot ids, or `None` to
    #: use `unifi_protect.lcd_presets`' default order.
    doorbell_slots: tuple[str, str, str] | None = None

    @property
    def can_live_activity(self) -> bool:
        """Whether Phase 2's Live Activity path is available to this device."""
        return bool(self.push_to_start_token) and self.la_capable

    @property
    def uses_situations(self) -> bool:
        """The one switch between the two evaluation paths (plan §8's
        backward-compatibility rule): empty or absent situations keeps this
        device on today's alert-firing behaviour, unchanged."""
        return bool(self.situations)
