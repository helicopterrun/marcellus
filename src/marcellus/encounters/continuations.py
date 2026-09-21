"""Suggested continuations (M5, docs/encounters.md "Suggested
continuations"): score a candidate next-camera observation (or a pure
prediction, when no candidate observation exists yet) against a source atom.
No DB, no I/O -- same pure-core convention as `linker.py`/`transitions.py`.

Product rule: a machine prediction is never shown as certain. `bucket()`
returns `confirmed` ONLY when the caller tells it the candidate already
shares the source's encounter (the linker already joined them) or carries a
human `pin` decision to that encounter -- never from score alone. `score` is
still returned for the app/debugging even when bucketed `None` (below
`min_score`) or dropped by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.transitions import TransitionStats


@dataclass(frozen=True)
class ContinuationWeights:
    topo: float = 0.30
    time: float = 0.30
    direction: float = 0.20
    klass: float = 0.20


@dataclass(frozen=True)
class ContinuationConfig:
    weights: ContinuationWeights = field(default_factory=ContinuationWeights)
    min_score: float = 0.25
    likely_score: float = 0.5
    late_factor: float = 3.0
    early_floor: float = 0.5
    unlearned_time_cap: float = 0.6
    unknown_direction: float = 0.3
    max_window_factor: float = 3.0


@dataclass(frozen=True)
class Source:
    """The source observation's fields, as `encounters/store.observation`
    already returns them."""

    camera: str
    end_time: float
    labels: tuple[str, ...]
    last_zone: str
    encounter_id: str
    atom_id: str


def _class_factor(source_labels: tuple[str, ...], cand_family_label: str) -> tuple[float, str]:
    if cand_family_label in source_labels:
        return 1.0, f"same label {cand_family_label}"
    return 0.6, f"same family as {'/'.join(source_labels) or 'source'}"


def _topo_factor(
    source_camera: str, cand_camera: str, adjacency: Adjacency, stats: TransitionStats | None
) -> tuple[float | None, str]:
    edge = frozenset((source_camera, cand_camera))
    zones = adjacency.shared_zones.get(edge, ())
    if edge in adjacency.edges:
        if zones:
            return 1.0, f"edge {source_camera}>{cand_camera} (shared zone {zones[0]})"
        return 1.0, f"edge {source_camera}>{cand_camera} (config)"
    if stats is not None and stats.source == "learned":
        return 0.7, f"learned transition {source_camera}>{cand_camera}"
    return None, ""


def _time_factor(
    elapsed_s: float | None, stats: TransitionStats | None, cfg: ContinuationConfig
) -> tuple[float, str]:
    default_stats = TransitionStats(p10=0.0, p50=0.0, p90=0.0, samples=0, source="default")
    st = stats if stats is not None else default_stats
    if elapsed_s is None:
        why = f"typical {st.p50:.0f}s" if st.source == "learned" else "prediction window"
        return 1.0, why
    overlapped = elapsed_s < 0
    # Overlapping hand-offs (candidate starts before the source atom ends)
    # are an early arrival, not a penalty case -- clamp to 0 so a -1s and a
    # -20s overlap score identically instead of decaying further negative.
    e_input = max(0.0, elapsed_s)
    p10, p90 = st.p10, st.p90
    if p10 <= e_input <= p90:
        e = 1.0
    elif e_input < p10:
        e = cfg.early_floor if p10 <= 0 else cfg.early_floor + (1.0 - cfg.early_floor) * (
            e_input / p10
        )
    else:
        late_edge = p90 * cfg.late_factor
        e = (
            0.0
            if late_edge <= p90
            else max(0.0, 1.0 - (e_input - p90) / (late_edge - p90))
        )
    if stats is None or stats.source != "learned":
        e = min(e, cfg.unlearned_time_cap)
    if overlapped:
        why = f"overlapped {-elapsed_s:.0f}s"
    else:
        why = f"typical {st.p50:.0f}s" if st.source == "learned" else "unlearned typical time"
    return max(0.0, min(1.0, e)), why


def _direction_factor(
    source: Source, cand_camera: str, adjacency: Adjacency, cfg: ContinuationConfig
) -> tuple[float | None, str]:
    edge = frozenset((source.camera, cand_camera))
    shared = adjacency.shared_zones.get(edge)
    if shared is None:
        # No shared-zone info for this edge at all (config-only edge) -- the
        # caller drops this factor and renormalises the remaining weights.
        return None, ""
    if source.last_zone == "":
        return cfg.unknown_direction, "exit zone unknown"
    if source.last_zone in shared:
        return 1.0, f"exit zone {source.last_zone}"
    return 0.5, f"exit zone {source.last_zone} not shared with {cand_camera}"


def score_candidate(
    source: Source,
    cand_camera: str,
    cand_family_label: str,
    elapsed_s: float | None,
    stats: TransitionStats | None,
    adjacency: Adjacency,
    cfg: ContinuationConfig,
) -> tuple[float, list[str]]:
    """Score one candidate (or pure prediction, when `elapsed_s is None`)
    against `source`. Caller has already filtered out label-family mismatch
    and non-adjacent/non-learned edges are signalled by a `None` topo factor
    here -- callers should treat that as "not a candidate" (score 0, no why)
    since a topology-less pair can never be scored."""
    topo, topo_why = _topo_factor(source.camera, cand_camera, adjacency, stats)
    if topo is None:
        return 0.0, []

    time_f, time_why = _time_factor(elapsed_s, stats, cfg)
    class_f, class_why = _class_factor(source.labels, cand_family_label)
    direction, direction_why = _direction_factor(source, cand_camera, adjacency, cfg)

    weights = cfg.weights
    factors: list[tuple[float, float, str]] = [
        (weights.topo, topo, topo_why),
        (weights.time, time_f, time_why),
        (weights.klass, class_f, class_why),
    ]
    if direction is not None:
        factors.append((weights.direction, direction, direction_why))

    total_weight = sum(w for w, _f, _why in factors)
    if total_weight <= 0:
        return 0.0, []
    score = sum(w * f for w, f, _why in factors) / total_weight
    score = max(0.0, min(1.0, score))
    why = [why for _w, _f, why in factors if why]
    return score, why


def bucket(
    score: float,
    cfg: ContinuationConfig,
    *,
    existing_same_encounter: bool,
    pinned: bool,
) -> str | None:
    if existing_same_encounter or pinned:
        return "confirmed"
    if score >= cfg.likely_score:
        return "likely"
    if score >= cfg.min_score:
        return "possible"
    return None


def predict_window(
    source_end: float, stats: TransitionStats | None, cfg: ContinuationConfig
) -> tuple[float, float]:
    """[end + p10, end + p90 * max_window_factor] -- the window a candidate
    member is searched for, or shown to the app as a pure prediction when
    none exists yet. Default stats (0/0/0) when `stats is None`."""
    st = stats if stats is not None else TransitionStats(
        p10=0.0, p50=0.0, p90=0.0, samples=0, source="default"
    )
    return (source_end + st.p10, source_end + st.p90 * cfg.max_window_factor)


def candidate_search_window(
    source_start: float, source_end: float, stats: TransitionStats | None, cfg: ContinuationConfig
) -> tuple[float, float]:
    """[source.start_time, source.end + p90 * max_window_factor] -- the
    window `members_in_window` is searched in for an EXISTING candidate
    member, wider than `predict_window` on purpose: it starts at the
    source's own start, not its end, so an overlapping hand-off (both
    cameras see the entity at once, e.g. a linker-joined sibling that starts
    while the source is still being seen) is found as a candidate instead of
    only ever showing up as a pure prediction. `predict_window` is still the
    window reported to the app for a pure prediction."""
    st = stats if stats is not None else TransitionStats(
        p10=0.0, p50=0.0, p90=0.0, samples=0, source="default"
    )
    return (source_start, source_end + st.p90 * cfg.max_window_factor)


def continuation_config_from_settings(settings: object) -> ContinuationConfig:
    enc = settings.encounters  # type: ignore[attr-defined]
    return ContinuationConfig(
        weights=ContinuationWeights(
            topo=enc.continuation_w_topo,
            time=enc.continuation_w_time,
            direction=enc.continuation_w_direction,
            klass=enc.continuation_w_class,
        ),
        min_score=enc.continuation_min_score,
        likely_score=enc.continuation_likely_score,
    )
