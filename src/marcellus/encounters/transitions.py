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
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import LABEL_FAMILIES, family_of

#: Samples clamp to [0, ...]; an m2 that started slightly before m1 ended
#: (clock skew / overlapping detections) is still a same-instant handoff.
_MIN_GAP_S = -5.0


@dataclass(frozen=True)
class TransitionStats:
    p10: float
    p50: float
    p90: float
    samples: int
    source: str  # "learned" | "config" | "default"


@dataclass(frozen=True)
class TransitionConfig:
    min_samples: int
    max_sample_s: float
    default: dict[str, float]  # {"p10":.., "p50":.., "p90":..}
    overrides: dict[str, dict[str, float]] = field(default_factory=dict)


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


def collect_samples(
    members: Sequence[MemberRow],
    adjacency: Adjacency,
    cfg: TransitionConfig,
    *,
    split_atoms: frozenset[str] = frozenset(),
) -> dict[tuple[str, str, str], list[float]]:
    """`{(cam_a, cam_b, family): [gap_seconds, ...]}` -- one sample per
    (m1, m2, shared family) eligible pair, grouped by encounter and ordered
    by `start_time`.

    A pair (m1, m2) is a sample iff: same encounter_id; m2 is the FIRST
    later member (by start_time) on a camera different from m1's, with no
    member on m2's camera in between; m1.end_time is not None (an open atom
    is skipped); `adjacency.adjacent(m1.camera, m2.camera)` and the cameras
    differ; m1 and m2 share at least one label family (one sample per shared
    family); neither atom has a human split decision; and the clamped gap
    falls in `[-5, cfg.max_sample_s]`.
    """
    by_encounter: dict[str, list[MemberRow]] = {}
    for m in members:
        by_encounter.setdefault(m.encounter_id, []).append(m)

    out: dict[tuple[str, str, str], list[float]] = {}
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
            gap = m2.start_time - m1.end_time
            if gap < _MIN_GAP_S:
                # Overlapping by more than the tolerance is co-presence
                # (two cameras seeing the same thing at once), not a
                # hand-off -- it would only teach a zero-second transition.
                continue
            gap = max(gap, 0.0)
            if gap > cfg.max_sample_s:
                continue
            for family in shared:
                out.setdefault((m1.camera, m2.camera, family), []).append(gap)
    return out


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

    samples = collect_samples(members, adjacency, cfg, split_atoms=split_atoms)

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
    conn.commit()
    return written


def load_transitions(conn: sqlite3.Connection) -> dict[tuple[str, str, str], TransitionStats]:
    """Every row in `camera_transitions`, for consumers (M3's linker,
    M5's UI)."""
    rows = conn.execute(
        "SELECT cam_a, cam_b, family, samples, p10_s, p50_s, p90_s, source "
        "FROM camera_transitions"
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
    )
