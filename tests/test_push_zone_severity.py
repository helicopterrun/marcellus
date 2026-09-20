"""Which of an event's zones the ladder routes on.

Frigate lists an object's zones in its own order and never reorders them, so
taking zones[0] meant a tight zone nested inside a broad one could never be the
one that decided anything. On this property `charger` (off_limits, the only one)
sits inside `parking_area`/`back_walkway`, and `zone_overrides[parking_area]
[person] = quiet` short-circuits `evaluate_ladder` before the table is reached --
so a person at the Tesla wall charger routed **quiet** while `charger` alone
routes urgent. Found by the first recorded fixture, not by any hand-written one.
"""

from __future__ import annotations

import pytest

from marcellus.push import ladder_policy
from marcellus.push.delivery_wire import (
    classify_place,
    most_severe_zone,
    snapshot_from_review,
    zone_place,
)
from marcellus.push.ladder import SUPPRESSED, Snapshot, base_level, evaluate_ladder
from marcellus.push.models import ReviewEvent

#: This property's live map, as of push_settings rev 7.
ZONE_CLASSES = {
    "alley": "street", "back_walkway": "private", "charger": "off_limits",
    "front_door": "doors", "garden": "private", "nw_49th_st": "street",
    "parking_area": "yard", "rooftop": "street", "sidewalk": "street",
}


@pytest.fixture(autouse=True)
def _policy() -> None:
    """The live overrides that make this a real problem rather than a
    hypothetical one. conftest's autouse fixture restores them."""
    ladder_policy.set_zone_overrides({
        "back_walkway": {"person": "quiet"},
        "parking_area": {"person": "quiet"},
    })
    ladder_policy.set_off_cells({("vehicle", "yard")})


def _event(*zones: str, labels: tuple[str, ...] = ("person",)) -> ReviewEvent:
    return ReviewEvent(
        review_id="r1", camera="stairway-wide", severity="alert",
        labels=labels, zones=zones,
    )


# --- base_level ----------------------------------------------------------

def test_base_level_reports_whether_an_override_decided_it() -> None:
    """An override short-circuits the rest of the ladder; a table hit does not,
    so the caller has to be able to tell them apart."""
    assert base_level("person", "yard", "parking_area") == ("quiet", True)
    assert base_level("person", "off_limits", "charger")[1] is False
    assert base_level("vehicle", "yard", "parking_area") == (SUPPRESSED, False)


# --- ranking -------------------------------------------------------------

def test_the_tight_zone_wins_over_the_one_containing_it() -> None:
    zone, place = most_severe_zone(
        ["parking_area", "charger"], subject="person", zone_classes=ZONE_CLASSES
    )
    assert (zone, place) == ("charger", "off_limits")


def test_ranking_beats_a_quieter_zones_override_not_just_its_place() -> None:
    """The load-bearing distinction. Ranking on place class alone would still
    lose here: parking_area's explicit person->quiet override outranks the base
    table, so a place-only ranking picks charger but the evaluator would then
    have keyed the override off parking_area anyway."""
    zone, place = most_severe_zone(
        ["back_walkway", "parking_area", "charger"], subject="person",
        zone_classes=ZONE_CLASSES,
    )
    assert zone == "charger"
    assert evaluate_ladder(Snapshot(subject="person", place=place, zone=zone)) == "urgent"


def test_a_person_at_the_charger_was_quiet_before_this() -> None:
    """Pins the regression itself: routing off zones[0] gives quiet."""
    old = evaluate_ladder(Snapshot(subject="person", place="yard", zone="parking_area"))
    assert old == "quiet"


def test_ties_keep_the_earliest_zone() -> None:
    """Nothing more severe on offer -> behave exactly as before this existed."""
    assert most_severe_zone(
        ["sidewalk", "alley", "nw_49th_st"], subject="person", zone_classes=ZONE_CLASSES
    ) == ("sidewalk", "street")


def test_suppressed_ranks_below_every_level() -> None:
    """A deliberate reading of "most severe wins": an off cell is the quietest
    outcome, not a veto over the other zones. A vehicle in the parking area that
    also clips the alley logs (street) instead of being suppressed (yard/off)."""
    zone, place = most_severe_zone(
        ["parking_area", "alley"], subject="vehicle", zone_classes=ZONE_CLASSES
    )
    assert (zone, place) == ("alley", "street")


def test_unmapped_zones_still_fall_back_to_the_name_heuristic() -> None:
    assert zone_place("pool", {}) == "off_limits"
    assert most_severe_zone(["porch", "pool"], subject="person", zone_classes={}) == (
        "pool", "off_limits",
    )


@pytest.mark.parametrize("zones", [(), ("",), ("", "")])
def test_no_usable_zone_is_street(zones: tuple[str, ...]) -> None:
    assert most_severe_zone(list(zones), subject="person", zone_classes=ZONE_CLASSES) == (
        "", "street",
    )


# --- the two consumers agree ---------------------------------------------

def test_snapshot_uses_one_zone_for_both_place_and_the_override_key() -> None:
    """The actual bug was these disagreeing: fixing the place class alone would
    have left the override still keyed on parking_area, and the level unchanged."""
    snapshot, subject, place = snapshot_from_review(
        _event("parking_area", "charger"), zone_classes=ZONE_CLASSES
    )
    assert (snapshot.zone, snapshot.place, place) == ("charger", "off_limits", "off_limits")
    assert subject == "person"
    assert evaluate_ladder(snapshot) == "urgent"


def test_classify_place_follows_the_same_rule() -> None:
    assert classify_place(_event("parking_area", "charger"), ZONE_CLASSES) == "off_limits"
    assert classify_place(_event("sidewalk"), ZONE_CLASSES) == "street"


def test_the_front_garden_now_outranks_the_sidewalk_it_touches() -> None:
    """Same shape, front side: garden is private (notify), sidewalk is street
    (log), and a visitor standing in the garden is in both."""
    snapshot, _, _ = snapshot_from_review(
        _event("sidewalk", "garden"), zone_classes=ZONE_CLASSES
    )
    assert snapshot.zone == "garden"
    assert evaluate_ladder(snapshot) == "notify"
