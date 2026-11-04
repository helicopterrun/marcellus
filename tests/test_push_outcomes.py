"""The outcome views of the v3 ladder (docs/ladder-v3.md): `ladder`
(ignore/silent/glance/urgent) is authoritative and the legacy `outcomes`
(off/log/glance/notify/alarm) and `routing_table_v2` views are derived from
it. These tests cover the settings-schema half: derivation, an older client's
legacy body translating into `ladder`, the evaluator's suppression of
ignored non-person cells, and the glance surface lookup."""

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


def test_defaults_include_outcomes_derived_from_ladder():
    doc = policy_settings.default_settings()
    assert doc["ladder"]["person"]["doors"] == "glance"
    assert doc["outcomes"]["person"]["doors"] == "glance"
    assert doc["outcomes"]["person"]["off_limits"] == "alarm"
    assert doc["outcomes"]["person"]["yard"] == "glance"
    # Ignore renders as "off" for a non-person subject, "log" for a person.
    assert doc["outcomes"]["thing"]["street"] == "off"
    assert doc["outcomes"]["person"]["street"] == "log"
    assert doc["routing_table_v2"]["thing"]["street"] == "log"


def test_ladder_is_the_authority_when_present():
    try:
        doc = _fresh({
            "ladder": {
                "person": {"yard": "urgent", "street": "ignore"},
                "vehicle": {"street": "ignore"},
            },
            # Legacy keys beside a ladder are ignored (re-derived).
            "outcomes": {"person": {"yard": "log"}},
        })
        # Derived views follow the ladder...
        assert doc["outcomes"]["person"]["yard"] == "alarm"
        assert doc["routing_table_v2"]["person"]["yard"] == "urgent"
        assert doc["routing_table_v2"]["person"]["street"] == "log"
        assert doc["outcomes"]["vehicle"]["street"] == "off"
        # ...an ignored non-person cell reaches the evaluator as suppression...
        snap = Snapshot(subject="vehicle", place="street", zone="", label="car")
        assert ladder.evaluate_ladder(snap) == SUPPRESSED
        # ...an ignored person cell is only Log (nudgeable)...
        snap = Snapshot(subject="person", place="street", zone="", label="person")
        assert ladder.evaluate_ladder(snap) == "log"
        # ...and a non-ignored cell evaluates normally.
        snap = Snapshot(subject="person", place="yard", zone="", label="person")
        assert ladder.evaluate_ladder(snap) == "urgent"
    finally:
        _restore()


def test_legacy_body_translates_into_ladder_and_derives_outcomes():
    try:
        # An old app build PUTs the legacy shape only.
        doc = policy_settings.normalize_settings({
            "routing_table_v2": {
                "person": {
                    "street": "log", "yard": "quiet", "doors": "notify",
                    "private": "notify", "off_limits": "urgent",
                },
                "vehicle": {
                    "street": "log", "yard": "quiet", "doors": "quiet",
                    "private": "quiet", "off_limits": "quiet",
                },
            },
        })
        person = doc["ladder"]["person"]
        assert person["street"] == "ignore"
        assert person["yard"] == "glance"        # yard quiet vs private notify: louder wins
        assert person["doors"] == "silent"       # notify -> silent
        assert person["off_limits"] == "urgent"
        assert doc["outcomes"]["person"]["street"] == "log"     # person ignore
        assert doc["outcomes"]["person"]["doors"] == "notify"   # silent reads back as notify
        assert doc["outcomes"]["person"]["yard"] == "glance"
        assert doc["outcomes"]["person"]["off_limits"] == "alarm"
        # A non-person log cell is Ignore, which renders as off.
        assert doc["ladder"]["vehicle"]["street"] == "ignore"
        assert doc["outcomes"]["vehicle"]["street"] == "off"
        assert doc["outcomes"]["vehicle"]["yard"] == "glance"   # quiet -> glance
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
            "ladder": {"vehicle": {"yard": "ignore"}},
            "zone_overrides": {"front_garden": {"vehicle": "silent"}},
        })
        snap = Snapshot(subject="vehicle", place="yard", zone="front_garden", label="car")
        assert ladder.evaluate_ladder(snap) == "silent"
        # Same place without the zone: suppressed.
        snap = Snapshot(subject="vehicle", place="yard", zone="", label="car")
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
        _fresh({"ladder": {"person": {"yard": "glance", "doors": "silent"}}})
        assert policy_settings.outcome_for("person", "yard") == "glance"
        assert policy_settings.outcome_for("person", "doors") == "notify"  # silent
    finally:
        _restore()
    # No ladder or outcomes in the active (pre-v3) doc at all: derive from levels.
    saved = policy_settings._active
    try:
        doc = policy_settings.default_settings()
        del doc["ladder"]
        del doc["outcomes"]
        policy_settings._active = doc
        ladder_policy.set_off_cells(set())
        assert policy_settings.outcome_for("person", "doors") == "glance"
        assert policy_settings.outcome_for("person", "yard") == "glance"
    finally:
        policy_settings._active = saved
        _restore()
