"""The attention ladder's golden suite (`fixtures/ladder/ladder_cases.json`).

Every case pins one precedence rule from `ladder.py`'s evaluation order --
the file header there cross-references which. A policy edit in
`ladder_policy.py` that changes any case's outcome must update the fixture
deliberately; this test never regenerates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus.push import ladder_policy
from marcellus.push.ladder import Snapshot, evaluate_ladder, evaluate_ladder_explained

CASES_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "ladder" / "ladder_cases.json"
CASES = json.loads(CASES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[f"{c['id']}-{c['name']}" for c in CASES])
def test_ladder_case(case):
    snapshot = Snapshot(**case["inputs"])
    assert evaluate_ladder(snapshot) == case["expected"]


def test_golden_suite_is_complete():
    """Guards against a silently truncated fixture file."""
    assert len(CASES) == 23


class TestExplainedStages:
    """One case per `LadderResult.stage` value (alerts-slice1 §A)."""

    def test_muted(self):
        result = evaluate_ladder_explained(Snapshot(muted=True, subject="stranger", place="yard"))
        assert result.stage == "muted"
        assert result.level == "suppressed"
        assert result.modifiers == ()

    def test_system(self):
        result = evaluate_ladder_explained(Snapshot(source="system"))
        assert result.stage == "system"
        assert result.level == ladder_policy.SYSTEM_CARD_LEVEL

    def test_safety_audio(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="known", place="private", audio_safety=True)
        )
        assert result.stage == "safety"
        assert result.level == "urgent"

    def test_safety_ai_flagged(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="yard", ai_flagged=True)
        )
        assert result.stage == "safety"
        assert result.level == "urgent"

    def test_zone_override(self):
        ladder_policy.set_zone_overrides({"front_door": {"stranger": "urgent"}})
        try:
            result = evaluate_ladder_explained(
                Snapshot(subject="stranger", place="yard", zone="front_door")
            )
        finally:
            ladder_policy.set_zone_overrides({})
        assert result.stage == "zone_override"
        assert result.level == "urgent"

    def test_off_cell(self):
        ladder_policy.set_off_cells({("thing", "yard")})
        try:
            result = evaluate_ladder_explained(Snapshot(subject="thing", place="yard"))
        finally:
            ladder_policy.set_off_cells(set())
        assert result.stage == "off_cell"
        assert result.level == "suppressed"

    def test_table(self):
        result = evaluate_ladder_explained(Snapshot(subject="animal", place="private"))
        assert result.stage == "table"


class TestExplainedModifiers:
    """Parametrized cases per `LadderResult.modifiers` value."""

    def test_reclass_dangerous_animal(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="animal", place="private", label="bear")
        )
        assert "reclass_dangerous_animal" in result.modifiers

    def test_nudge_up(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="doors", nobody_home=True, night=True)
        )
        assert "nudge_up" in result.modifiers

    def test_nudge_down(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="doors", leaving_scene=True, low_confidence=True)
        )
        assert "nudge_down" in result.modifiers

    def test_child_hazard_floor(self):
        # Base table for stranger/yard is "quiet" -- below notify, so the
        # floor visibly raises it.
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="yard", child_hazard_zone=True)
        )
        assert "child_hazard_floor" in result.modifiers
        assert ladder_policy.LEVELS.index(result.level) >= ladder_policy.LEVELS.index("notify")

    def test_street_cap(self):
        # The base table is always "log" on `street` for every subject, so
        # the cap only has something to bite on once the child-hazard floor
        # has already pushed the level up to "notify".
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="street", child_hazard_zone=True)
        )
        assert "child_hazard_floor" in result.modifiers
        assert "street_cap" in result.modifiers
        assert result.level == "quiet"

    def test_unconfirmed_cap(self):
        result = evaluate_ladder_explained(
            Snapshot(subject="stranger", place="private", detector_confirmed=False)
        )
        assert "unconfirmed_cap" in result.modifiers
        assert result.level == "quiet"


@pytest.mark.parametrize("case", CASES, ids=[f"{c['id']}-{c['name']}" for c in CASES])
def test_evaluate_ladder_matches_explained(case):
    """`evaluate_ladder` must always agree with `evaluate_ladder_explained`'s
    `.level` across the full golden suite -- the wrapper adds no logic of
    its own."""
    snapshot = Snapshot(**case["inputs"])
    assert evaluate_ladder(snapshot) == evaluate_ladder_explained(snapshot).level
