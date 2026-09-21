"""Pydantic response models for the app-facing `/v1` JSON surface.

These describe the wire shape the iOS app (Elsinore/FrigateKit) already
decodes today -- they must never change what a route emits. `extra="forbid"`
so an undeclared key fails validation loudly in tests instead of being
silently stripped in prod.

Two usage patterns, chosen per route (see the routes themselves for which):

1. Plain `response_model=` on the decorator, for routes whose dict is
   unconditionally complete (every declared key present on every response,
   only its *value* ever null). FastAPI validates + re-serialises through
   the model.
2. Validate-then-return: the route keeps building its own dict (so ETag
   bytes / conditionally-omitted keys are untouched) and calls
   `Model.model_validate(body)` purely as a contract check before handing
   the same dict to its own serialiser. Used for `_etagged` responses and
   for any route that omits a key entirely rather than emitting it as
   `null` (e.g. `motion_unavailable`, `highlights[].events`,
   `objects[].members`) -- those use `response_model_exclude_unset=True`
   instead so a key genuinely absent from the route's dict stays absent
   rather than round-tripping through the model as an explicit `null`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# /v1/capabilities
# --------------------------------------------------------------------------


class ScrubCacheCapabilities(_Wire):
    enabled: bool
    format: str
    cameras: list[str]
    generated: bool
    intervals: list[float]


class ProxyCapabilities(_Wire):
    enabled: bool


class PushCapabilities(_Wire):
    enabled: bool
    transport: str
    attention_subjects: list[str]


class DecisionsCapabilities(_Wire):
    enabled: bool


class SearchCapabilities(_Wire):
    enabled: bool
    related_events: bool


class CapabilitiesResponse(_Wire):
    version: str
    scrub_cache: ScrubCacheCapabilities
    proxy: ProxyCapabilities
    push: PushCapabilities
    decisions: DecisionsCapabilities
    search: SearchCapabilities


# --------------------------------------------------------------------------
# /v1/coverage/{camera}  (§4.4) -- validate-then-return, `_etagged`
# --------------------------------------------------------------------------


class CoverageResponse(_Wire):
    camera: str
    queried: list[float]
    recorded: list[tuple[float, float]]
    latest_segment_end: float | None
    authoritative_through: float
    scrub_retention_days: int
    recording_retention_days: float | None


# --------------------------------------------------------------------------
# /v1/scrub/{camera}/sheets  (§4.3) -- validate-then-return, `_etagged`
# --------------------------------------------------------------------------


class SheetItem(_Wire):
    url: str
    start: float
    interval: float
    cols: int
    rows: int
    cell_w: int
    cell_h: int
    count: int


class SheetsResponse(_Wire):
    sheets: list[SheetItem]


# --------------------------------------------------------------------------
# /v1/reel/{camera}  (§4.5) -- validate-then-return, `_etagged`
# --------------------------------------------------------------------------


class ReelFrame(_Wire):
    start: float
    interval: float
    count: int


class ReelMotion(_Wire):
    start: float
    interval: float
    values: list[float]


class ReelPathSummary(_Wire):
    drift: list[list[float]]
    dwell: list[list[float]]


class ReelContinuation(_Wire):
    camera: str
    event_id: str
    start: float


class ReelEvent(_Wire):
    id: str
    label: str
    zones: list[str]
    start: float
    end: float | None
    score: float | None
    sub_label: str | None
    has_clip: bool
    has_snapshot: bool
    path: ReelPathSummary | None
    continues: ReelContinuation | None


class ReelReview(_Wire):
    id: str
    start: float
    end: float | None
    severity: str
    objects: list[Any]
    zones: list[Any]
    detections: list[Any]


class ReelResponse(_Wire):
    queried: list[float]
    recorded: list[tuple[float, float]]
    latest_segment_end: float | None
    authoritative_through: float
    frames: list[ReelFrame]
    motion: ReelMotion
    events: list[ReelEvent]
    reviews: list[ReelReview]
    motion_unavailable: bool | None = None


# --------------------------------------------------------------------------
# /v1/highlights/{camera}  (§4.7) -- response_model, exclude_unset
# (`events` is only emitted at all when `cluster_s` groups a run of events)
# --------------------------------------------------------------------------


class HighlightItem(_Wire):
    start: float
    end: float | None
    reason: str
    score: float | None
    events: int | None = None


class HighlightsResponse(_Wire):
    highlights: list[HighlightItem]


# --------------------------------------------------------------------------
# /v1/events/search -- plain response_model (every key always present)
# --------------------------------------------------------------------------


class SearchResultItem(_Wire):
    id: str
    camera: str
    label: str
    sub_label: str | None
    zones: list[Any]
    start_time: float
    end_time: float | None
    has_clip: bool
    has_snapshot: bool
    data: dict[str, Any]
    search_distance: float | None
    search_source: str


# --------------------------------------------------------------------------
# /v1/events/{event_id}/related -- plain response_model
# --------------------------------------------------------------------------


class RelatedItem(_Wire):
    camera: str
    event_id: str
    start_time: float
    end_time: float | None
    label: str
    score: Any = None
    source: str


class RelatedResponse(_Wire):
    event_id: str
    related: list[RelatedItem]


# --------------------------------------------------------------------------
# /v1/push/map/live -- response_model, exclude_unset
# (`stale` and `objects[].members` are only emitted when applicable)
# --------------------------------------------------------------------------


class MapLiveMember(_Wire):
    camera: str
    track_id: str
    x: float
    y: float
    forward_ft: float
    age_s: float


class MapLiveObject(_Wire):
    x: float
    y: float
    label: str
    stationary: bool
    cameras: list[str]
    track_ids: list[str]
    members: list[MapLiveMember] | None = None


class MapLiveResponse(_Wire):
    t: float
    objects: list[MapLiveObject]
    stale: bool | None = None


# --------------------------------------------------------------------------
# /v1/push/map/track -- plain response_model (every key always present,
# possibly null)
# --------------------------------------------------------------------------


class MapTrackCamera(_Wire):
    x: float
    y: float


class MapTrackResponse(_Wire):
    points_map: list[list[float]]
    camera: MapTrackCamera
    secure_area: dict[str, Any] | None
    aspect: float
    speed_ft_s: float | None
    distance_ft_range: list[float] | None


# --------------------------------------------------------------------------
# /v1/encounters, /v1/encounters/{id} (docs/encounters.md)
# --------------------------------------------------------------------------


class EncounterMember(_Wire):
    atom_id: str
    camera: str
    start: float
    end: float | None
    severity: str
    labels: list[str]
    zones: list[str]
    event_ids: list[str]
    sub_labels: list[str]
    link_reason: str
    confidence: float
    first_zone: str = ""
    last_zone: str = ""
    direction: str = ""
    heading_deg: float | None = None
    dir_source: str = ""


class EncounterSummary(_Wire):
    id: str
    start: float
    end: float | None
    sealed: bool
    cameras: list[str]
    labels: list[str]
    identities: list[str]
    primary_event_id: str | None
    peak_severity: str
    atom_count: int


class EncountersResponse(_Wire):
    t: float
    encounters: list[EncounterSummary]


class EncounterResponse(_Wire):
    encounter: EncounterSummary
    members: list[EncounterMember]


# --------------------------------------------------------------------------
# /v1/observations (docs/encounters.md "Observations"): a read-only view
# over encounter_members, one row per atom, for callers that want atoms
# directly rather than grouped by encounter.
# --------------------------------------------------------------------------


class Observation(_Wire):
    id: str
    encounter_id: str
    camera: str
    start: float
    end: float | None
    labels: list[str]
    zones: list[str]
    first_zone: str
    last_zone: str
    direction: str
    heading_deg: float | None
    dir_source: str
    severity: str
    event_ids: list[str]
    sub_labels: list[str]
    link_reason: str
    confidence: float


class ObservationsResponse(_Wire):
    t: float
    observations: list[Observation]


class ObservationNeighbours(_Wire):
    prev: Observation | None
    next: Observation | None


class ObservationDetailResponse(_Wire):
    observation: Observation
    encounter: EncounterSummary
    neighbours: ObservationNeighbours


# --------------------------------------------------------------------------
# Camera topology (M2, docs/encounters.md "Camera topology"): adjacency +
# learned/config/default transition stats, for `/v1/topology` and
# `/v1/cameras/{camera}/neighbours`.
# --------------------------------------------------------------------------


class TransitionStatsOut(_Wire):
    p10: float
    p50: float
    p90: float
    samples: int
    source: str


class TopologyEdge(_Wire):
    a: str
    b: str
    zones: list[str]
    source: str
    transitions: dict[str, dict[str, TransitionStatsOut]]


class TopologyResponse(_Wire):
    cameras: list[str]
    edges: list[TopologyEdge]


class NeighbourCamera(_Wire):
    camera: str
    zones: list[str]
    transitions: dict[str, TransitionStatsOut]


class CameraNeighboursResponse(_Wire):
    camera: str
    neighbours: list[NeighbourCamera]
