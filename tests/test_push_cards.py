"""Unit tests for the pure card model (`push/cards.py`): mutation
classification and sound accounting, independent of the DB or transport.
"""

from __future__ import annotations

from marcellus.push.cards import (
    CREATE,
    DEESCALATE,
    ENRICH,
    ESCALATE,
    RESOLVE,
    SUPPRESSED,
    Card,
    classify_mutation,
    should_sound,
    urgent_resound_due,
)


def make_card(level: str, **kwargs) -> Card:
    defaults = dict(card_key="k", created_at=0.0, updated_at=0.0, state_since_at=0.0)
    defaults.update(kwargs)
    return Card(level=level, **defaults)


def test_classify_no_existing_card_is_create():
    assert classify_mutation(None, "notify") == CREATE


def test_classify_suppressed_beats_everything():
    card = make_card("urgent", sound_count=2)
    assert classify_mutation(card, SUPPRESSED) == SUPPRESSED
    assert classify_mutation(None, SUPPRESSED) == SUPPRESSED


def test_classify_closed_card_is_create_again():
    card = make_card("log", closed=True)
    assert classify_mutation(card, "quiet") == CREATE


def test_classify_same_level_new_facts_is_enrich():
    card = make_card("quiet")
    assert classify_mutation(card, "quiet") == ENRICH


def test_classify_higher_level_is_escalate():
    card = make_card("quiet")
    assert classify_mutation(card, "urgent") == ESCALATE


def test_classify_lower_level_is_deescalate():
    card = make_card("urgent")
    assert classify_mutation(card, "quiet") == DEESCALATE


def test_classify_drop_to_log_is_deescalate_not_resolve():
    # A level drop is not, by itself, "the subject is gone" -- that is a
    # distinct explicit signal (`resolved=True`), since a `thing` can never
    # even reach `log` off the table alone at a non-street place.
    card = make_card("notify")
    assert classify_mutation(card, "log") == DEESCALATE


def test_classify_resolved_flag_wins_over_level_comparison():
    card = make_card("quiet")
    assert classify_mutation(card, "quiet", resolved=True) == RESOLVE
    assert classify_mutation(card, "urgent", resolved=True) == RESOLVE


def test_classify_mute_beats_resolved():
    card = make_card("urgent")
    assert classify_mutation(card, SUPPRESSED, resolved=True) == SUPPRESSED


def test_should_sound_on_create_at_notify():
    card = make_card("notify")
    assert should_sound(card, CREATE, "notify") is True


def test_should_sound_never_true_for_quiet():
    card = make_card("quiet")
    assert should_sound(card, CREATE, "quiet") is False
    assert should_sound(card, ESCALATE, "quiet") is False


def test_should_sound_false_for_enrich_deescalate_resolve():
    card = make_card("notify")
    assert should_sound(card, ENRICH, "notify") is False
    assert should_sound(card, DEESCALATE, "quiet") is False
    assert should_sound(card, RESOLVE, "log") is False


def test_should_sound_respects_budget_of_two():
    card = make_card("notify", sound_count=2)
    assert should_sound(card, ESCALATE, "urgent") is False


def test_should_sound_quiet_create_does_not_spend_budget():
    # A quiet create never sounds (should_sound returns False), so a card
    # that starts quiet and later escalates twice past it can still sound
    # twice: budget is spent on emitted sounds, not on beats.
    card = make_card("quiet", sound_count=0)
    assert should_sound(card, CREATE, "quiet") is False


def test_urgent_resound_requires_urgent_unhandled_and_elapsed():
    card = make_card("urgent", last_sound_at=0.0)
    assert urgent_resound_due(card, now=119.0, interval_s=120.0, enabled=True) is False
    assert urgent_resound_due(card, now=120.0, interval_s=120.0, enabled=True) is True


def test_urgent_resound_disabled_by_config():
    card = make_card("urgent", last_sound_at=0.0)
    assert urgent_resound_due(card, now=200.0, interval_s=120.0, enabled=False) is False


def test_urgent_resound_not_for_notify():
    card = make_card("notify", last_sound_at=0.0)
    assert urgent_resound_due(card, now=200.0, interval_s=120.0, enabled=True) is False


def test_urgent_resound_repeats_up_to_max():
    card = make_card("urgent", last_sound_at=0.0, resound_count=1)
    assert urgent_resound_due(card, now=999.0, interval_s=120.0, enabled=True) is True


def test_urgent_resound_stops_at_max():
    card = make_card("urgent", last_sound_at=0.0, resound_count=5)
    assert urgent_resound_due(card, now=999.0, interval_s=120.0, enabled=True) is False


def test_urgent_resound_stops_once_handled():
    card = make_card("urgent", last_sound_at=0.0, handled=True)
    assert urgent_resound_due(card, now=200.0, interval_s=120.0, enabled=True) is False
