"""The merged outcome ladder (content design 2026-08-16): one dial per
subject x place (off/log/glance/notify/alarm) folding the old routing level
and LA-family choice together. These tests cover the settings-schema half:
derivation in both directions, "off" surviving a legacy round trip, the
evaluator's suppression, and the glance surface lookup."""

from __future__ import annotations

from marcellus.push import ladder, ladder_policy, policy_settings
from marcellus.push.ladder import SUPPRESSED, Snapshot


def _fresh(doc=None):
    """Apply a normalized doc as the active policy; restore in caller."""
    normalized = policy_settings.normalize_settings(doc or {})
    policy_settings.apply_settings(normalized)
    return normalized


def _restore():
    policy_settings.apply_settings(policy_settings.normalize_settings({}))


def test_defaults_include_outcomes_derived_from_v2_levels():
    doc = policy_settings.default_settings()
    assert doc["outcomes"]["person"]["doors"] == "notify"
    assert doc["outcomes"]["person"]["off_limits"] == "alarm"
    assert doc["outcomes"]["person"]["yard"] == "glance"   # quiet -> glance
    assert doc["outcomes"]["thing"]["street"] == "log"


def test_outcomes_are_the_authority_when_present():
    try:
        doc = _fresh({
            "outcomes": {"person": {"yard": "alarm", "street": "off"}},
        })
        # Derived levels follow the outcomes...
        assert doc["routing_table_v2"]["person"]["yard"] == "urgent"
        assert doc["routing_table_v2"]["person"]["street"] == "log"  # off renders as log
        # ...and the off cell reaches the evaluator as suppression.
        snap = Snapshot(subject="person", place="street", zone="", label="person")
        assert ladder.evaluate_ladder(snap) == SUPPRESSED
        # A non-off cell still evaluates normally.
        snap = Snapshot(subject="person", place="yard", zone="", label="person")
        assert ladder.evaluate_ladder(snap) == "urgent"
    finally:
        _restore()


def test_legacy_body_derives_outcomes_and_preserves_off():
    try:
        # A stored doc with an off cell...
        _fresh({"outcomes": {"person": {"street": "off"}}})
        # ...then an old app build PUTs the legacy shape only (street=log,
        # because that's how the legacy view renders an off cell).
        doc = policy_settings.normalize_settings({
            "routing_table_v2": {"person": {
                "street": "log", "yard": "quiet", "doors": "notify",
                "private": "notify", "off_limits": "urgent",
            }},
        })
        assert doc["outcomes"]["person"]["street"] == "off"      # preserved
        assert doc["outcomes"]["person"]["yard"] == "glance"     # quiet -> glance
        assert doc["outcomes"]["person"]["off_limits"] == "alarm"
    finally:
        _restore()


def test_legacy_level_change_clears_off():
    try:
        _fresh({"outcomes": {"person": {"street": "off"}}})
        # The old app explicitly raises the cell -- that's a real choice, not
        # a lossy rendering of off; the off must not stick.
        doc = policy_settings.normalize_settings({
            "routing_table_v2": {"person": {
                "street": "notify", "yard": "quiet", "doors": "notify",
                "private": "notify", "off_limits": "urgent",
            }},
        })
        assert doc["outcomes"]["person"]["street"] == "notify"
    finally:
        _restore()


def test_zone_override_outranks_off():
    try:
        _fresh({
            "outcomes": {"person": {"yard": "off"}},
            "zone_overrides": {"front_garden": {"person": "notify"}},
        })
        snap = Snapshot(subject="person", place="yard", zone="front_garden", label="person")
        assert ladder.evaluate_ladder(snap) == "notify"
        # Same place without the zone: suppressed.
        snap = Snapshot(subject="person", place="yard", zone="", label="person")
        assert ladder.evaluate_ladder(snap) == SUPPRESSED
    finally:
        _restore()


def test_validation_rejects_bad_outcomes():
    errors = policy_settings.validate_settings({
        "outcomes": {"person": {"yard": "shout"}, "ghost": {}},
    })
    assert any("outcomes.person.yard" in e for e in errors)
    assert any("unknown subject" in e for e in errors)


def test_outcome_for_reads_active_and_falls_back_to_levels():
    try:
        _fresh({"outcomes": {"person": {"yard": "glance"}}})
        assert policy_settings.outcome_for("person", "yard") == "glance"
    finally:
        _restore()
    # No outcomes in the active doc at all: derive from levels.
    saved = policy_settings._active
    try:
        doc = policy_settings.default_settings()
        del doc["outcomes"]
        policy_settings._active = doc
        ladder_policy.set_off_cells(set())
        assert policy_settings.outcome_for("person", "doors") == "notify"
        assert policy_settings.outcome_for("person", "yard") == "glance"
    finally:
        policy_settings._active = saved
        _restore()
