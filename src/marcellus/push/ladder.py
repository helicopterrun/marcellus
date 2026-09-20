"""The attention ladder: given a detection snapshot, how loud is it?

Pure routing logic -- no APNs, no delivery, no Live Activity, no HTTP. One
function, `evaluate_ladder`, answers a single question: which of four
attention levels (`log` < `quiet` < `notify` < `urgent`, `ladder_policy.LEVELS`)
a snapshot earns, or whether it's suppressed outright. All policy (the base
table, which reasons nudge which way, dangerous-animal labels, the system-card
level) lives in `ladder_policy.py` as data; this module is just the fixed
evaluation order that data plugs into, so a policy change is a data edit, not
a code change.

Evaluation order:

1. Muted -> suppressed, unconditionally -- beats even a system card or a
   safety exception.
2. `source == "system"` (no subject/place, e.g. "camera offline") ->
   `ladder_policy.SYSTEM_CARD_LEVEL`, bypassing everything below.
3. Safety exceptions (`audio_safety`, `ai_flagged`) -> `urgent`, bypassing the
   table, nudge, floor, and the caps in step 8 -- including for `known`
   subjects.
4. Reclassify: a dangerous-animal `label` makes `subject` a `stranger` from
   here on.
5. Zone override (Elsinore Phase 4 addendum, `ladder_policy.ZONE_OVERRIDES`):
   a user-configured `(zone, subject)` override returns directly, bypassing
   the base table *and* the nudge/floor/caps below -- "always banner this
   subject in this zone" means always, not "usually, modulated by the same
   general-purpose exceptions everything else goes through." Checked after
   safety exceptions/mute (those are hard invariants, not policy) but before
   everything the base table drives.
6. Base table lookup (`subject` x `place`).
7. One nudge: net worry vs. calm reasons moves the result at most one step.
   `animal` subjects never nudge; `known` subjects never nudge up (down is
   fine).
8. Floor: a person subject in a `child_hazard_zone` is at least `notify`.
9. Caps: `street` caps at `quiet`; an unconfirmed detector caps at `quiet`.
"""

from __future__ import annotations

from dataclasses import dataclass

from marcellus.push import ladder_policy as policy

SUPPRESSED = "suppressed"

#: Routing v2 subjects that don't exist in the v1 TABLE fall back to the
#: most-cautious v1 equivalent so the evaluator works regardless of which
#: table is loaded. The reverse mapping handles v1 subjects against a v2
#: TABLE (e.g. tests that construct Snapshots with legacy subject names).
_V2_TO_V1 = {
    "person": "stranger",
    "vehicle": "thing",
    # V3 subjects (one alerts stack, 2026-08-20): when the applied table
    # predates their rows, they route as the `thing` they used to be.
    "package": "thing",
    "bin": "thing",
    "opening": "thing",
}
_V1_TO_V2 = {"stranger": "person", "known": "person"}


@dataclass(frozen=True)
class Snapshot:
    """One detection (or system card) to route. `subject`/`place` are unused
    (leave as "") when `source == "system"`."""

    source: str = "detection"  # "detection" | "system"
    subject: str = ""  # "stranger" | "known" | "animal" | "thing"
    place: str = ""  # "street" | "yard" | "doors" | "private" | "off_limits"
    #: The raw Frigate zone name (not the place class `place` above) --
    #: looked up against `ladder_policy.ZONE_OVERRIDES` before the base
    #: table (Phase 4 addendum). "" for a detection with no zone, or a
    #: system card.
    zone: str = ""
    label: str = ""  # raw Frigate label
    nobody_home: bool = False
    night: bool = False
    dwell_exceeded: bool = False
    seen_before_still_unrecognized: bool = False
    #: Direction/speed modifiers (2026-08-15): sustained movement toward the
    #: secure area, sustained movement away, and running-pace speed — all
    #: derived from ground-plane calibration in delivery_wire/ground.py.
    approaching_secure: bool = False
    leaving_scene: bool = False
    moving_fast: bool = False
    known_role: bool = False
    low_confidence: bool = False
    no_recognition_capability: bool = False
    muted: bool = False
    audio_safety: bool = False
    ai_flagged: bool = False
    child_hazard_zone: bool = False
    detector_confirmed: bool = True


def base_level(subject: str, place: str, zone: str) -> tuple[str, bool]:
    """The level before nudges, floors and caps: zone override, then off-cell,
    then the base table. Returns `(level, came_from_a_zone_override)` because an
    override short-circuits the rest of the ladder and a table hit does not.

    Extracted so `delivery_wire.most_severe_zone` can rank candidate zones by
    exactly the precedence the evaluator will then apply. Ranking by anything
    else -- place class alone, say -- could pick a zone that evaluates
    differently, because a quiet zone's explicit override outranks a loud
    zone's table cell.
    """
    override = policy.ZONE_OVERRIDES.get(zone, {}).get(subject)
    if override is not None:
        return override, True
    if (subject, place) in policy.OFF_CELLS:
        return SUPPRESSED, False
    if subject in policy.TABLE:
        table_subject = subject
    else:
        table_subject = _V2_TO_V1.get(subject) or _V1_TO_V2.get(subject) or subject
    return policy.TABLE[table_subject][place], False


@dataclass(frozen=True)
class LadderResult:
    """The full explanation behind a `evaluate_ladder` outcome: which single
    step fixed the base outcome (`stage`), and which steps nudged it further
    (`modifiers`, in application order). See spec `alerts-slice1` §A for the
    enum and the plain-English text each value maps to."""

    level: str
    stage: str
    modifiers: tuple[str, ...]


def evaluate_ladder_explained(snapshot: Snapshot) -> LadderResult:
    """Same evaluation order as `evaluate_ladder`, recording which stage fixed
    the base outcome and which modifiers moved it afterward."""
    if snapshot.muted:
        return LadderResult(SUPPRESSED, "muted", ())
    if snapshot.source == "system":
        return LadderResult(policy.SYSTEM_CARD_LEVEL, "system", ())
    if snapshot.audio_safety or snapshot.ai_flagged:
        return LadderResult("urgent", "safety", ())

    subject = snapshot.subject
    modifiers: list[str] = []
    if snapshot.label in policy.DANGEROUS_ANIMAL_LABELS:
        subject = "person" if "person" in policy.TABLE else "stranger"
        modifiers.append("reclass_dangerous_animal")

    override = policy.ZONE_OVERRIDES.get(snapshot.zone, {}).get(subject)
    if override is not None:
        return LadderResult(override, "zone_override", tuple(modifiers))

    if (subject, snapshot.place) in policy.OFF_CELLS:
        return LadderResult(SUPPRESSED, "off_cell", tuple(modifiers))

    if subject in policy.TABLE:
        table_subject = subject
    else:
        table_subject = _V2_TO_V1.get(subject) or _V1_TO_V2.get(subject) or subject
    level = policy.TABLE[table_subject][snapshot.place]
    stage = "table"

    levels = policy.LEVELS
    idx = levels.index(level)

    if subject != "animal":
        worry = sum(1 for r in policy.WORRY_REASONS if getattr(snapshot, r))
        calm = sum(1 for r in policy.CALM_REASONS if getattr(snapshot, r))
        step = 1 if worry > calm else -1 if calm > worry else 0
        if step == 1 and subject == "known":
            step = 0
        if step == 1:
            modifiers.append("nudge_up")
        elif step == -1:
            modifiers.append("nudge_down")
        idx = max(0, min(len(levels) - 1, idx + step))

    if subject in ("stranger", "known", "person") and snapshot.child_hazard_zone:
        notify_idx = levels.index("notify")
        if idx < notify_idx:
            modifiers.append("child_hazard_floor")
        idx = max(idx, notify_idx)

    if snapshot.place == "street":
        quiet_idx = levels.index("quiet")
        if idx > quiet_idx:
            modifiers.append("street_cap")
        idx = min(idx, quiet_idx)
    if not snapshot.detector_confirmed:
        quiet_idx = levels.index("quiet")
        if idx > quiet_idx:
            modifiers.append("unconfirmed_cap")
        idx = min(idx, quiet_idx)

    return LadderResult(levels[idx], stage, tuple(modifiers))


def evaluate_ladder(snapshot: Snapshot) -> str:
    """Return one of `ladder_policy.LEVELS`, or `SUPPRESSED`."""
    return evaluate_ladder_explained(snapshot).level
