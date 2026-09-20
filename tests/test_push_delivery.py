"""Sequence golden suite for the delivery pipeline
(`fixtures/ladder/delivery_cases.json`).

Each case is an ordered list of steps -- ladder snapshots, an urgent
re-sound timer check, or a "mark handled" event -- driven through
`delivery.advance_card` (and `cards.urgent_resound_due` /
`delivery.apply_urgent_resound` for the timer) against one card key. The
expected list is 1:1 with steps: a snapshot or resound step asserts
`(mutation, level, sound, push)`; a `mark_handled` step has no expectation
(`null`); a resound check that wasn't due asserts `mutation: null`.

This is the sequence analogue of `test_push_ladder.py`'s golden suite: a
policy change here must update `delivery_cases.json` deliberately, in the
same commit, not be silently re-blessed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus.push.cards import SUPPRESSED, urgent_resound_due
from marcellus.push.delivery import advance_card, apply_urgent_resound, should_push
from marcellus.push.ladder import Snapshot, evaluate_ladder

CASES_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "ladder" / "delivery_cases.json"
)
CASES = json.loads(CASES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[f"{c['id']}-{c['name']}" for c in CASES])
def test_delivery_case(case):
    card_key = case["card_key"]
    card = None
    seen_keys: set[str] = set()

    for step, expected in zip(case["steps"], case["expected"], strict=True):
        kind = step["kind"]

        if kind == "snapshot":
            level = evaluate_ladder(Snapshot(**step["inputs"]))
            card, mutation, sound = advance_card(
                card, level, card_key=card_key, now=step["now"],
                resolved=step.get("resolved", False),
            )
            seen_keys.add(card.card_key)
            assert mutation == expected["mutation"]
            assert card.level == expected["level"]
            assert sound is expected["sound"]
            assert should_push(card.level) is expected["push"]

        elif kind == "mark_handled":
            assert expected is None
            assert card is not None
            card.handled = True
            card.handled_at = step["now"]

        elif kind == "resound_check":
            assert card is not None
            due = urgent_resound_due(
                card, now=step["now"], interval_s=step["interval_s"],
                enabled=step.get("enabled", True),
            )
            if due:
                card = apply_urgent_resound(card, now=step["now"])
                seen_keys.add(card.card_key)
                assert expected["mutation"] == "escalate"
                assert card.level == expected["level"]
                assert expected["sound"] is True
                assert expected["push"] is True
            else:
                assert expected["mutation"] is None
                assert expected["sound"] is False
                assert expected["push"] is False

        else:  # pragma: no cover - guards a typo'd fixture kind
            raise AssertionError(f"unknown step kind {kind!r}")

    # Same collapse id (apns-collapse-id) throughout a card's lifetime, even
    # across mutation and the urgent re-sound.
    assert seen_keys == {card_key}


def test_golden_delivery_suite_is_complete():
    """Guards against a silently truncated fixture file."""
    assert len(CASES) == 6


def test_simultaneous_cards_on_same_camera_stay_distinct():
    """Two subjects detected on the same camera at the same time must not
    collide on card key / collapse id, even though they share every other
    dimension (camera, place, timing)."""
    level_a = evaluate_ladder(Snapshot(subject="stranger", place="doors"))
    level_b = evaluate_ladder(Snapshot(subject="known", place="doors"))

    card_a, mutation_a, _ = advance_card(
        None, level_a, card_key="front:stranger:track-1", now=0.0,
    )
    card_b, mutation_b, _ = advance_card(
        None, level_b, card_key="front:known:track-2", now=0.0,
    )

    assert mutation_a == mutation_b == "create"
    assert card_a.card_key != card_b.card_key

    # Mutating one does not touch the other.
    card_a2, _, _ = advance_card(card_a, "urgent", card_key=card_a.card_key, now=1.0)
    assert card_a2.card_key == card_a.card_key
    assert card_b.level == level_b


def test_suppressed_level_never_pushes():
    assert should_push(SUPPRESSED) is False
