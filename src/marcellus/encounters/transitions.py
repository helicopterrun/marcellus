"""Camera topology (M2, docs/encounters.md "Camera topology"): learned
inter-camera transition times, directed `cam_a -> cam_b` per label family
(`linker.family_of`). Pure core + thin sqlite I/O, same split as
`linker.py`/`store.py` -- `collect_samples`/`summarise` take plain values in
and return plain values out; `learn`/`load_transitions` are the only
functions here that touch a `sqlite3.Connection`.

Samples come from consecutive same-encounter atoms on different, adjacent
cameras that share a label family -- see `collect_samples` for the exact
eligibility rule. `learn` writes one row per directed adjacency edge x
family into `camera_transitions`, always: a manual `encounters.
transition_overrides` entry wins ('config'), else enough samples yields
percentiles ('learned'), else `encounters.transition_default_s` is written
with the (sub-threshold) sample count actually seen ('default'). Never
raises into its caller (`service.reconcile`) -- see that module's catch.

Overlap: the same eligibility walk also feeds `count_concurrency` /
`overlap_verdict` -- a camera pair whose samples are mostly "seen together"
(gap <= `overlap_concurrent_max_s`, incl. any negative gap) shares a view.
`learn` stores the verdict in `camera_overlap` and, for overlapping pairs,
keeps the concurrent samples out of walking times (same place, not a walk).
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import LABEL_FAMILIES, family_of
from marcellus.encounters.types import TransitionStats

__all__ = [
    "TransitionStats",
    "TransitionConfig",
    "MemberRow",
    "OverlapStats",
    "collect_samples",
    "count_concurrency",
    "overlap_verdict",
    "load_overlap",
    "summarise",
    "learn",
    "load_transitions",
    "transition_config_from_settings",
]

#: Samples clamp to [0, ...]; an m2 that started slightly before m1 ended
#: (clock skew / overlapping detections) is still a same-instant handoff.
_MIN_GAP_S = -5.0


@dataclass(frozen=True)
class TransitionConfig:
    min_samples: int
    max_sample_s: float
    default: dict[str, float]  # {"p10":.., "p50":.., "p90":..}
    overrides: dict[str, dict[str, float]] = field(default_factory=dict)
    overlap: frozenset[frozenset[str]] = frozenset()
    not_overlap: frozenset[frozenset[str]] = frozenset()
    overlap_concurrent_max_s: float = 1.0
    overlap_min_fraction: float = 0.65


@dataclass(frozen=True)
class OverlapStats:
    """Whether a camera pair shares a view. `source` is 'config', 'learned'
    or 'default'; `concurrent`/`samples` are the evidence behind it."""

    overlap: bool
    source: str
    concurrent: int
    samples: int


@dataclass(frozen=True)
class MemberRow:
    """The subset of an `encounter_members` row `collect_samples` needs."""

    encounter_id: str
    atom_id: str
    camera: str
    start_time: float
    end_time: float | None
    labels: tuple[str, ...]


def _override_for(
    cfg: TransitionConfig, cam_a: str, cam_b: str, family: str
) -> dict[str, float] | None:
    """Family-specific override wins over the unqualified pair override."""
    specific = cfg.overrides.get(f"{cam_a}>{cam_b}:{family}")
    if specific is not None:
        return specific
    return cfg.overrides.get(f"{cam_a}>{cam_b}")


def _eligible_pairs(
    members: Sequence[MemberRow],
    adjacency: Adjacency,
    *,
    split_atoms: frozenset[str] = frozenset(),
) -> Iterator[tuple[MemberRow, MemberRow, set[str], float]]:
    """Yield `(m1, m2, shared_families, raw_gap)` for every eligible pair:
    same encounter_id; m2 is the FIRST later member (by start_time) on a
    camera different from m1's; m1.end_time is not None (an open atom is
    skipped); the cameras are adjacent and differ; m1 and m2 share at least
    one label family; neither atom has a human split decision. `raw_gap` is
    `m2.start_time - m1.end_time`, unclamped and unfiltered (negative when
    the atoms overlap in time)."""
    by_encounter: dict[str, list[MemberRow]] = {}
    for m in members:
        by_encounter.setdefault(m.encounter_id, []).append(m)

    for rows in by_encounter.values():
        ordered = sorted(rows, key=lambda r: r.start_time)
        for i, m1 in enumerate(ordered):
            if m1.end_time is None:
                continue
            if m1.atom_id in split_atoms:
                continue
            m2 = None
            for cand in ordered[i + 1 :]:
                if cand.camera == m1.camera:
                    continue
                m2 = cand
                break
            if m2 is None:
                continue
            if m2.atom_id in split_atoms:
                continue
            if not adjacency.adjacent(m1.camera, m2.camera) or m1.camera == m2.camera:
                continue
            m1_families = {family_of(label) for label in m1.labels}
            m2_families = {family_of(label) for label in m2.labels}
            shared = m1_families & m2_families
            if not shared:
                continue
            yield m1, m2, shared, m2.start_time - m1.end_time


def collect_samples(
    members: Sequence[MemberRow],
    adjacency: Adjacency,
    cfg: TransitionConfig,
    *,
    split_atoms: frozenset[str] = frozenset(),
    skip_concurrent_for: frozenset[frozenset[str]] = frozenset(),
) -> dict[tuple[str, str, str], list[float]]:
    """`{(cam_a, cam_b, family): [gap_seconds, ...]}` -- one sample per
    (m1, m2, shared family) eligible pair (see `_eligible_pairs`) whose
    clamped gap falls in `[-5, cfg.max_sample_s]`. Pairs in
    `skip_concurrent_for` (overlapping cameras) additionally drop samples
    with raw gap <= `cfg.overlap_concurrent_max_s` -- same place, not a walk.
    """
    out: dict[tuple[str, str, str], list[float]] = {}
    for m1, m2, shared, raw_gap in _eligible_pairs(members, adjacency, split_atoms=split_atoms):
        if raw_gap < _MIN_GAP_S:
            # Overlapping by more than the tolerance is co-presence
            # (two cameras seeing the same thing at once), not a
            # hand-off -- it would only teach a zero-second transition.
            continue
        if raw_gap > cfg.max_sample_s:
            continue
        if (
            raw_gap <= cfg.overlap_concurrent_max_s
            and frozenset((m1.camera, m2.camera)) in skip_concurrent_for
        ):
            continue
        gap = max(raw_gap, 0.0)
        for family in shared:
            out.setdefault((m1.camera, m2.camera, family), []).append(gap)
    return out


def count_concurrency(
    members: Sequence[MemberRow],
    adjacency: Adjacency,
    cfg: TransitionConfig,
    *,
    split_atoms: frozenset[str] = frozenset(),
) -> dict[frozenset[str], tuple[int, int]]:
    """Per undirected pair, `(concurrent, total)`. Each eligible (m1, m2)
    occurrence counts once (not per family). concurrent = raw gap <=
    `overlap_concurrent_max_s` (any negative gap included); total =
    concurrent + transit-eligible (concurrent_max < gap <= max_sample_s)."""
    conc: dict[frozenset[str], int] = {}
    total: dict[frozenset[str], int] = {}
    for m1, m2, _shared, raw_gap in _eligible_pairs(members, adjacency, split_atoms=split_atoms):
        pair = frozenset((m1.camera, m2.camera))
        if raw_gap <= cfg.overlap_concurrent_max_s:
            conc[pair] = conc.get(pair, 0) + 1
            total[pair] = total.get(pair, 0) + 1
        elif raw_gap <= cfg.max_sample_s:
            total[pair] = total.get(pair, 0) + 1
    return {pair: (conc.get(pair, 0), n) for pair, n in total.items()}


def overlap_verdict(
    pair: frozenset[str],
    counts: dict[frozenset[str], tuple[int, int]],
    cfg: TransitionConfig,
) -> OverlapStats:
    """Config `not_overlap` beats config `overlap` beats the learned
    fraction (needs `min_samples`) beats the False default."""
    concurrent, total = counts.get(pair, (0, 0))
    if pair in cfg.not_overlap:
        return OverlapStats(False, "config", concurrent, total)
    if pair in cfg.overlap:
        return OverlapStats(True, "config", concurrent, total)
    if total >= cfg.min_samples and total > 0:
        return OverlapStats(
            concurrent / total >= cfg.overlap_min_fraction, "learned", concurrent, total
        )
    return OverlapStats(False, "default", concurrent, total)


def summarise(samples: list[float]) -> tuple[float, float, float] | None:
    """MAD-trim outliers (drop |x - median| > 3*MAD when MAD > 0), then
    linear-interpolated p10/p50/p90. None for an empty list."""
    if not samples:
        return None
    values = sorted(samples)
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    if mad > 0:
        trimmed = [v for v in values if abs(v - median) <= 3 * mad]
        if trimmed:
            values = sorted(trimmed)
    if not values:
        return None
    return (
        _percentile(values, 0.10),
        _percentile(values, 0.50),
        _percentile(values, 0.90),
    )


def _percentile(sorted_values: list[float], q: float) -> float:
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _stats_from_override(values: dict[str, float], samples: int) -> TransitionStats:
    return TransitionStats(
        p10=float(values.get("p10", 0.0)),
        p50=float(values.get("p50", 0.0)),
        p90=float(values.get("p90", 0.0)),
        samples=samples,
        source="config",
    )


def learn(
    conn: sqlite3.Connection,
    *,
    adjacency: Adjacency,
    cfg: TransitionConfig,
    window_s: float,
    now: float,
) -> int:
    """Scan `encounter_members` over the last `window_s` seconds, compute
    samples for every directed adjacency edge x label family, and write one
    row each into `camera_transitions` (INSERT OR REPLACE, one transaction).
    Idempotent -- a second run with unchanged inputs writes the same rows.
    Returns the number of rows written."""
    since = now - window_s
    rows = conn.execute(
        "SELECT encounter_id, atom_id, camera, start_time, end_time, labels_json "
        "FROM encounter_members WHERE start_time >= ?",
        (since,),
    ).fetchall()
    split_atoms = frozenset(
        str(r["atom_id"])
        for r in conn.execute(
            "SELECT DISTINCT atom_id FROM encounter_decisions WHERE action = 'split'"
        ).fetchall()
    )

    members = [
        MemberRow(
            encounter_id=str(r["encounter_id"]),
            atom_id=str(r["atom_id"]),
            camera=str(r["camera"]),
            start_time=float(r["start_time"]),
            end_time=float(r["end_time"]) if r["end_time"] is not None else None,
            labels=tuple(json.loads(r["labels_json"]) if r["labels_json"] else []),
        )
        for r in rows
    ]

    counts = count_concurrency(members, adjacency, cfg, split_atoms=split_atoms)
    verdicts = {
        edge: overlap_verdict(edge, counts, cfg) for edge in adjacency.edges if len(edge) == 2
    }
    overlapping = frozenset(edge for edge, v in verdicts.items() if v.overlap)
    samples = collect_samples(
        members, adjacency, cfg, split_atoms=split_atoms, skip_concurrent_for=overlapping
    )

    families = set(LABEL_FAMILIES.keys())
    for _a, _b, family in samples:
        families.add(family)

    directed_edges: list[tuple[str, str]] = []
    for edge in adjacency.edges:
        a, b = sorted(edge)
        directed_edges.append((a, b))
        directed_edges.append((b, a))

    written = 0
    for cam_a, cam_b in directed_edges:
        for family in families:
            override = _override_for(cfg, cam_a, cam_b, family)
            key = (cam_a, cam_b, family)
            sample_list = samples.get(key, [])
            if override is not None:
                stats = _stats_from_override(override, len(sample_list))
            else:
                summary = summarise(sample_list)
                if summary is not None and len(sample_list) >= cfg.min_samples:
                    p10, p50, p90 = summary
                    stats = TransitionStats(
                        p10=p10, p50=p50, p90=p90, samples=len(sample_list), source="learned"
                    )
                else:
                    stats = TransitionStats(
                        p10=float(cfg.default.get("p10", 0.0)),
                        p50=float(cfg.default.get("p50", 0.0)),
                        p90=float(cfg.default.get("p90", 0.0)),
                        samples=len(sample_list),
                        source="default",
                    )
            conn.execute(
                "INSERT INTO camera_transitions (cam_a, cam_b, family, samples, p10_s, "
                "p50_s, p90_s, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cam_a, cam_b, family) DO UPDATE SET samples = excluded.samples, "
                "p10_s = excluded.p10_s, p50_s = excluded.p50_s, p90_s = excluded.p90_s, "
                "source = excluded.source, updated_at = excluded.updated_at",
                (
                    cam_a,
                    cam_b,
                    family,
                    stats.samples,
                    stats.p10,
                    stats.p50,
                    stats.p90,
                    stats.source,
                    now,
                ),
            )
            written += 1
    for edge, verdict in verdicts.items():
        cam_a, cam_b = sorted(edge)
        conn.execute(
            "INSERT INTO camera_overlap (cam_a, cam_b, concurrent, samples, overlap, "
            "source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cam_a, cam_b) DO UPDATE SET concurrent = excluded.concurrent, "
            "samples = excluded.samples, overlap = excluded.overlap, "
            "source = excluded.source, updated_at = excluded.updated_at",
            (
                cam_a,
                cam_b,
                verdict.concurrent,
                verdict.samples,
                1 if verdict.overlap else 0,
                verdict.source,
                now,
            ),
        )
    conn.commit()
    return written


def load_overlap(conn: sqlite3.Connection) -> dict[frozenset[str], OverlapStats]:
    """Every row in `camera_overlap`, keyed by the undirected pair."""
    rows = conn.execute(
        "SELECT cam_a, cam_b, concurrent, samples, overlap, source FROM camera_overlap"
    ).fetchall()
    return {
        frozenset((str(r["cam_a"]), str(r["cam_b"]))): OverlapStats(
            overlap=bool(r["overlap"]),
            source=str(r["source"]),
            concurrent=int(r["concurrent"]),
            samples=int(r["samples"]),
        )
        for r in rows
    }


def load_transitions(conn: sqlite3.Connection) -> dict[tuple[str, str, str], TransitionStats]:
    """Every row in `camera_transitions`, for consumers (M3's linker,
    M5's UI)."""
    rows = conn.execute(
        "SELECT cam_a, cam_b, family, samples, p10_s, p50_s, p90_s, source FROM camera_transitions"
    ).fetchall()
    out: dict[tuple[str, str, str], TransitionStats] = {}
    for r in rows:
        out[(str(r["cam_a"]), str(r["cam_b"]), str(r["family"]))] = TransitionStats(
            p10=float(r["p10_s"]) if r["p10_s"] is not None else 0.0,
            p50=float(r["p50_s"]) if r["p50_s"] is not None else 0.0,
            p90=float(r["p90_s"]) if r["p90_s"] is not None else 0.0,
            samples=int(r["samples"]),
            source=str(r["source"]),
        )
    return out


def transition_config_from_settings(settings: Any) -> TransitionConfig:
    enc = settings.encounters
    return TransitionConfig(
        min_samples=enc.transition_min_samples,
        max_sample_s=enc.transition_max_sample_s,
        default=dict(enc.transition_default_s),
        overrides=dict(enc.transition_overrides),
        overlap=_pairs(enc.overlap),
        not_overlap=_pairs(enc.not_overlap),
        overlap_concurrent_max_s=enc.overlap_concurrent_max_s,
        overlap_min_fraction=enc.overlap_min_fraction,
    )


def _pairs(raw: Sequence[Sequence[str]]) -> frozenset[frozenset[str]]:
    return frozenset(frozenset(p) for p in raw if len(p) == 2 and p[0] != p[1])
