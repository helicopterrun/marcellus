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

#: Ordinal, low -> high. Index arithmetic in `ladder.py` (clamp, nudge, cap,
#: floor) all assume this order.
LEVELS = ("log", "quiet", "notify", "urgent")

#: subject x place -> base level, before nudges/floor/caps.
TABLE: dict[str, dict[str, str]] = {
    "stranger": {
        "street": "log", "yard": "quiet", "doors": "notify",
        "private": "notify", "off_limits": "urgent",
    },
    "known": {
        "street": "log", "yard": "log", "doors": "quiet",
        "private": "quiet", "off_limits": "quiet",
    },
    "animal": {
        "street": "log", "yard": "quiet", "doors": "quiet",
        "private": "quiet", "off_limits": "quiet",
    },
    "thing": {
        "street": "log", "yard": "log", "doors": "log",
        "private": "log", "off_limits": "quiet",
    },
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
#: etc.) -- these have no subject or place, so they never touch the table,
#: nudge, floor, or caps.
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


#: Subject x place cells the merged outcome ladder marks "off": the
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
