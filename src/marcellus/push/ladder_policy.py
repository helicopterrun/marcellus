"""Declarative policy for the attention ladder (`ladder.py`).

Everything that decides *how loud* a detection is lives here as data: the
subject x place base table, which reasons nudge the result up or down, which
Frigate labels are dangerous enough to reclassify as a stranger, and the fixed
level a system card (e.g. "camera offline") reports at. A policy change --
adding a dangerous-animal label, moving a zone class in the table, retuning
which reasons count as worry vs. calm -- is an edit to this file, re-validated
by `tests/test_push_ladder.py` against `fixtures/ladder_cases.json`. The
evaluator in `ladder.py` never branches on any of these values by name.
"""

from __future__ import annotations

#: Ordinal, low -> high (attention ladder v3, `docs/ladder-v3.md` §1). Index
#: arithmetic in `ladder.py` (clamp, nudge, cap, floor) all assume this order.
#:   log    -- Ignore: recorded, shown in the app only
#:   silent -- Quiet: passive card push, never a sound, kept in Notification
#:             Center (new in v3; deliberately not `quiet`, which is Glance)
#:   quiet  -- Glance: a Live Activity, no card push
#:   urgent -- Urgent: time-sensitive banner, sound per mute
#: `notify` is retired from routing: legacy input carrying it is translated to
#: `silent` at the boundary (`LEGACY_LEVEL_MAP`); system cards keep their own
#: `SYSTEM_CARD_LEVEL` path outside this tuple.
LEVELS = ("log", "silent", "quiet", "urgent")

#: The four places (v3). `private` is retired and read as `yard` wherever it
#: still appears in input (`canonical_place`).
PLACES = ("street", "yard", "doors", "off_limits")

#: Pre-v3 level words that may still arrive from legacy input, stored cards
#: or fixtures -> their v3 engine level (§6: `notify` -> `silent`).
LEGACY_LEVEL_MAP = {"notify": "silent"}


def canonical_place(place: str) -> str:
    """`private` (retired in v3) reads as `yard`; everything else as-is."""
    return "yard" if place == "private" else place


def canonical_level(level: str) -> str:
    """A level word from anywhere (stored card, legacy body) as a v3 engine
    level: `notify` -> `silent`; anything else unchanged."""
    return LEGACY_LEVEL_MAP.get(level, level)


#: subject x place -> base level, before nudges/floor/caps. The evaluator's
#: own built-in baseline (the golden suite's table); the live, user-edited
#: table comes from `policy_settings` via `set_table`. v3 translation of the
#: pre-v3 baseline: `private` merged into `yard` (louder wins), and the
#: retired `notify` cells became Glance (`quiet`) -- the Live Activity the
#: old notify level also started (`docs/ladder-v3.md` §2.1 example row).
TABLE: dict[str, dict[str, str]] = {
    "stranger": {"street": "log", "yard": "quiet", "doors": "quiet", "off_limits": "urgent"},
    "known": {"street": "log", "yard": "log", "doors": "quiet", "off_limits": "quiet"},
    "animal": {"street": "log", "yard": "quiet", "doors": "quiet", "off_limits": "quiet"},
    "thing": {"street": "log", "yard": "log", "doors": "log", "off_limits": "quiet"},
}

#: Frigate labels that reclassify `subject` as `stranger` regardless of its
#: upstream classification (a bear or skunk on the property outranks whatever
#: upstream called it). Never add coyote -- Frigate cannot ID it, so a
#: `label == "coyote"` never actually occurs.
DANGEROUS_ANIMAL_LABELS = frozenset({"bear", "skunk", "raccoon"})

#: Reasons that push the result one level up. Must be `Snapshot` field names
#: -- `ladder.py` reads them by `getattr`.
WORRY_REASONS = (
    "nobody_home", "night", "dwell_exceeded", "seen_before_still_unrecognized",
    "approaching_secure", "moving_fast",
)

#: Reasons that pull the result one level down. Must be `Snapshot` field names.
CALM_REASONS = ("known_role", "low_confidence", "no_recognition_capability", "leaving_scene")

#: Fixed level for `source == "system"` cards (camera offline, disk full,
#: test pushes, the Connection Doctor round trip) -- these have no subject or
#: place, so they never touch the table, nudge, floor, or caps. Deliberately
#: NOT one of `LEVELS` (v3 §6): system cards keep their own delivery (active
#: push with sound, `delivery.SYSTEM_CARD_APNS`), outside the ladder.
SYSTEM_CARD_LEVEL = "notify"

#: `{zone: {subject: level}}` (Elsinore Phase 4 addendum, `push/
#: policy_settings.py`'s `zone_overrides`). Empty by default -- pure base-
#: table lookup, unchanged from before this addendum existed. Checked by
#: `ladder.evaluate_ladder` before the base table, replacing (not
#: modifying) whatever the table would have said for that exact
#: `(zone, subject)` pair.
ZONE_OVERRIDES: dict[str, dict[str, str]] = {}


def set_zone_overrides(overrides: dict[str, dict[str, str]]) -> None:
    """Replace the live zone-override map. Same rebind-a-module-global
    mechanism as `set_table` -- `ladder.py` reads `policy.ZONE_OVERRIDES` as
    a module attribute at call time, so nothing there needs to change."""
    global ZONE_OVERRIDES
    ZONE_OVERRIDES = overrides


#: Subject x place cells the evaluator suppresses outright: Ignore for a
#: non-person subject (v3 §4.1 step 6; the pre-v3 `off` outcome). The
#: evaluator answers SUPPRESSED for them before nudges can raise anything.
#: Zone overrides still outrank this -- an explicit per-zone rule is the
#: user's most specific statement.
OFF_CELLS: set[tuple[str, str]] = set()


def set_off_cells(cells: set[tuple[str, str]]) -> None:
    global OFF_CELLS
    OFF_CELLS = set(cells)


def set_table(table: dict[str, dict[str, str]]) -> None:
    """Replace the base subject x place table (Elsinore Phase 4: user-
    editable routing). `ladder.py` reads `policy.TABLE` as a module
    attribute at call time, not at import time, so simply rebinding it here
    is enough -- no change to `ladder.py` itself, exactly as the policy/
    evaluation-order split this module already established intends.

    The literal `TABLE` above stays the module's own built-in default
    (and the fixture-tested baseline `tests/test_push_ladder.py` exercises)
    -- `push/policy_settings.py` owns the *live*, potentially user-edited
    table and calls this to apply it; nothing in this module reads from
    disk or knows a settings file exists.
    """
    global TABLE
    TABLE = table
