"""Pure-Python encounter linking (docs/encounters.md). No DB, no I/O -- every
function here takes plain values in and returns plain values out, so the
whole decision surface is unit-testable without a database or an app.

An **atom** is one Frigate review segment. An **encounter** is an ordered
chain of atoms across cameras and time gaps that a person would call "one
thing" -- `decide` is the rule that says whether a new atom continues an
existing (open) encounter or starts a new one, and why.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from marcellus.encounters.adjacency import Adjacency
from marcellus.push.live_activities import BIN_LABELS, OPENING_LABELS

LABEL_FAMILIES: dict[str, frozenset[str]] = {
    "person": frozenset({"person"}),
    "vehicle": frozenset({"car", "truck", "bus", "motorcycle", "bicycle"}),
    "animal": frozenset(
        {
            "dog",
            "cat",
            "raccoon",
            "bird",
            "squirrel",
            "fox",
            "deer",
            "skunk",
            "opossum",
            "rabbit",
            "bear",
        }
    ),
    "package": frozenset({"package"}),
}

_LABEL_TO_FAMILY: dict[str, str] = {
    label: family for family, labels in LABEL_FAMILIES.items() for label in labels
}


def family_of(label: str) -> str:
    """The label family for one Frigate label, or "default" if unknown."""
    return _LABEL_TO_FAMILY.get(label, "default")


#: Every base label this codebase knows about, beyond `LABEL_FAMILIES`:
#: `push/live_activities.py`'s `BIN_LABELS`/`OPENING_LABELS` are Frigate
#: object labels too (a waste bin, a garage door), just not ones that get a
#: continuity family of their own. Reused here rather than duplicated so the
#: two lists can't drift.
KNOWN_LABELS: frozenset[str] = frozenset(_LABEL_TO_FAMILY) | BIN_LABELS | OPENING_LABELS


def normalise_labels(
    objects: Iterable[str], known_labels: Collection[str] = KNOWN_LABELS
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split Frigate's `data.objects` (or `ReviewEvent.labels`) into base
    labels and qualifiers.

    Production Frigate promotes a sub_label into `objects` alongside the
    base label, e.g. `["person", "person-verified"]`, and can also list a
    bare sub_label with no base counterpart, e.g. `["amazon"]`. Left as-is,
    `person-verified` reads as an unknown label (family "default"), which
    breaks continuity with the plain `person` atom next to it and shows as
    label-chip noise on `/encounters`.

    Rules, applied per object, first match wins:
    - equal to a known base label -> stays a label
    - `<known>-<qualifier>` (split on the FIRST hyphen only, base must be a
      known label) -> base label + qualifier `<qualifier>`
    - anything else (e.g. a bare sub_label like "amazon") -> qualifier only,
      never added to labels

    Both outputs are deduped, order-preserving (first appearance wins).
    """
    known = known_labels if isinstance(known_labels, (set, frozenset)) else set(known_labels)
    labels: list[str] = []
    qualifiers: list[str] = []
    for obj in objects:
        if obj in known:
            if obj not in labels:
                labels.append(obj)
            continue
        base, sep, qualifier = obj.partition("-")
        if sep and base in known:
            if base not in labels:
                labels.append(base)
            if qualifier not in qualifiers:
                qualifiers.append(qualifier)
        else:
            if obj not in qualifiers:
                qualifiers.append(obj)
    return tuple(labels), tuple(qualifiers)


@dataclass(frozen=True)
class Atom:
    """One Frigate review segment, reduced to what linking needs."""

    atom_id: str
    camera: str
    start_time: float
    end_time: float | None
    labels: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    sub_labels: tuple[str, ...] = ()
    severity: str = "detection"  # "alert" | "detection"


@dataclass
class OpenEncounter:
    """In-memory view of an unsealed encounter, mutated by `apply`."""

    encounter_id: str
    start_time: float
    last_end: float
    cameras: list[str] = field(default_factory=list)  # ordered distinct, first-appearance order
    labels: set[str] = field(default_factory=set)
    identities: set[str] = field(default_factory=set)
    zones: set[str] = field(default_factory=set)
    atom_ids: list[str] = field(default_factory=list)
    peak_severity: str = "detection"


@dataclass(frozen=True)
class LinkDecision:
    encounter_id: str | None  # None => start a new encounter
    # "pinned" | "identity" | "same_camera" | "shared_zone" | "adjacent" | "companion" | "new"
    reason: str
    confidence: float  # 0..1


@dataclass(frozen=True)
class LinkerConfig:
    gap_s: dict[str, float]
    max_duration_s: float
    recent_cameras: int
    min_copresence_s: float


def _allowed_gap(atom: Atom, cfg: LinkerConfig, *, identity_match: bool) -> float:
    families = {family_of(label) for label in atom.labels} or {"default"}
    allowed = max(cfg.gap_s.get(family, cfg.gap_s.get("default", 60.0)) for family in families)
    if identity_match:
        allowed *= 3.0
    return allowed


def _recent_cameras(enc: OpenEncounter, cfg: LinkerConfig) -> list[str]:
    if cfg.recent_cameras <= 0:
        return list(enc.cameras)
    return enc.cameras[-cfg.recent_cameras :]


def _effective_end(atom: Atom, now: float) -> float:
    """An atom with `end_time is None` is still active -- its review segment
    hasn't closed yet -- so it extends to `now`, not back to its own start.
    Substituting `start_time` here (the original bug) made every live
    "new"/"update" atom look instantaneous: a raccoon open on one camera for
    4 minutes would show a 240s gap to the next camera instead of 0."""
    return atom.end_time if atom.end_time is not None else now


def _span_overlap_s(atom: Atom, enc: OpenEncounter, now: float) -> float:
    atom_end = _effective_end(atom, now)
    lo = max(atom.start_time, enc.start_time)
    hi = min(atom_end, enc.last_end)
    return hi - lo


def _candidate_decision(
    atom: Atom, enc: OpenEncounter, adjacency: Adjacency, cfg: LinkerConfig, now: float
) -> LinkDecision | None:
    """Best decision linking `atom` onto `enc`, or None if `enc` isn't a
    candidate at all (hard-rejected or no rule matches)."""
    # -- Hard rejects --
    if atom.start_time - enc.start_time > cfg.max_duration_s:
        return None
    if atom.sub_labels and enc.identities and not (set(atom.sub_labels) & enc.identities):
        return None

    identity_match = bool(set(atom.sub_labels) & enc.identities)
    gap = atom.start_time - enc.last_end
    allowed_gap = _allowed_gap(atom, cfg, identity_match=identity_match)

    best: LinkDecision | None = None

    # -- Continuity: needs a shared label family and the gap within allowance. --
    atom_families = {family_of(label) for label in atom.labels}
    enc_families = {family_of(label) for label in enc.labels}
    if atom_families & enc_families and gap <= allowed_gap:
        recent = _recent_cameras(enc, cfg)
        if identity_match:
            best = LinkDecision(enc.encounter_id, "identity", 0.95)
        elif atom.camera in recent:
            best = LinkDecision(enc.encounter_id, "same_camera", 0.9)
        elif set(atom.zones) & enc.zones:
            best = LinkDecision(enc.encounter_id, "shared_zone", 0.8)
        elif any(adjacency.adjacent(atom.camera, cam) for cam in recent):
            best = LinkDecision(enc.encounter_id, "adjacent", 0.6)

    # -- Companionship: no shared family required. --
    recent = _recent_cameras(enc, cfg)
    if _span_overlap_s(atom, enc, now) >= cfg.min_copresence_s and (
        atom.camera in recent or any(adjacency.adjacent(atom.camera, cam) for cam in recent)
    ):
        companion = LinkDecision(enc.encounter_id, "companion", 0.7)
        if best is None or companion.confidence > best.confidence:
            best = companion

    return best


def decide(
    atom: Atom,
    open_encounters: Sequence[OpenEncounter],
    adjacency: Adjacency,
    cfg: LinkerConfig,
    *,
    now: float,
    pinned_to: str | None = None,
    split_from: frozenset[str] = frozenset(),
) -> LinkDecision:
    if pinned_to is not None:
        for enc in open_encounters:
            if enc.encounter_id == pinned_to:
                return LinkDecision(enc.encounter_id, "pinned", 1.0)

    candidates = [enc for enc in open_encounters if enc.encounter_id not in split_from]

    decisions: list[tuple[LinkDecision, float]] = []
    for enc in candidates:
        decision = _candidate_decision(atom, enc, adjacency, cfg, now)
        if decision is not None:
            gap = atom.start_time - enc.last_end
            decisions.append((decision, abs(gap)))

    if not decisions:
        return LinkDecision(None, "new", 1.0)

    decisions.sort(key=lambda pair: (-pair[0].confidence, pair[1]))
    return decisions[0][0]


def apply(enc: OpenEncounter, atom: Atom, now: float) -> None:
    """Mutate `enc` in place to fold `atom` into it."""
    enc.last_end = max(enc.last_end, _effective_end(atom, now))
    if atom.camera not in enc.cameras:
        enc.cameras.append(atom.camera)
    enc.labels |= set(atom.labels)
    enc.identities |= set(atom.sub_labels)
    enc.zones |= set(atom.zones)
    if atom.atom_id not in enc.atom_ids:
        enc.atom_ids.append(atom.atom_id)
    if atom.severity == "alert":
        enc.peak_severity = "alert"


def fold(
    atoms: Iterable[Atom],
    adjacency: Adjacency,
    cfg: LinkerConfig,
    *,
    now: float,
    pinned: Mapping[str, str] | None = None,
    split: Mapping[str, frozenset[str]] | None = None,
) -> list[OpenEncounter]:
    """Run `decide` + `apply` over `atoms` sorted by `start_time`.

    `now` is the reference "current time" used to treat any still-open atom
    (`end_time is None`) as active up to that moment rather than
    instantaneous at its own start -- callers processing historical atoms
    (e.g. a fixed-point idempotency test) should pass a `now` at or after the
    latest atom's start/end, same as a live caller would pass the real
    current time.

    `pinned`/`split` map an atom id to a forced encounter id / a set of
    encounter ids that atom must never join, mirroring the `encounter_decisions`
    table's pin/split rows. Used directly by tests and by the reconciler.
    """
    pinned = pinned or {}
    split = split or {}
    open_encounters: list[OpenEncounter] = []
    for atom in sorted(atoms, key=lambda a: a.start_time):
        decision = decide(
            atom,
            open_encounters,
            adjacency,
            cfg,
            now=now,
            pinned_to=pinned.get(atom.atom_id),
            split_from=split.get(atom.atom_id, frozenset()),
        )
        if decision.encounter_id is None:
            enc = OpenEncounter(
                encounter_id=uuid.uuid4().hex,
                start_time=atom.start_time,
                last_end=_effective_end(atom, now),
                cameras=[atom.camera],
                labels=set(atom.labels),
                identities=set(atom.sub_labels),
                zones=set(atom.zones),
                atom_ids=[atom.atom_id],
                peak_severity=atom.severity,
            )
            open_encounters.append(enc)
        else:
            enc = next(e for e in open_encounters if e.encounter_id == decision.encounter_id)
            apply(enc, atom, now)
    return open_encounters
