"""Tests for LA-first delivery, person_restricted family, la_capable,
delivery setting stickiness, and feedback endpoint."""

from __future__ import annotations

import tempfile
from pathlib import Path

from marcellus.push import live_activities as la
from marcellus.push import policy_settings

# -- Settings: delivery + person_restricted ----------------------------------

def test_default_settings_include_delivery_and_person_restricted():
    defaults = policy_settings.default_settings()
    la_settings = defaults["live_activities"]
    assert la_settings["delivery"] == "la_first"
    assert la_settings["person_restricted"] is True


def test_validate_delivery_accepts_valid_values():
    for val in ("la_first", "notifications"):
        errors = policy_settings.validate_settings({
            "live_activities": {"delivery": val},
        })
        assert not errors, f"delivery={val!r} should be valid"


def test_validate_delivery_rejects_invalid():
    errors = policy_settings.validate_settings({
        "live_activities": {"delivery": "bogus"},
    })
    assert any("delivery" in e for e in errors)


def test_validate_ignores_unknown_la_keys():
    errors = policy_settings.validate_settings({
        "live_activities": {"future_field": 42, "person": True},
    })
    assert not errors


def test_normalize_delivery():
    result = policy_settings.normalize_settings({
        "live_activities": {"delivery": "notifications"},
    })
    assert result["live_activities"]["delivery"] == "notifications"


def test_normalize_delivery_absent_keeps_default():
    result = policy_settings.normalize_settings({
        "live_activities": {"person": False},
    })
    assert result["live_activities"]["delivery"] == "la_first"


def test_normalize_person_restricted_default_true():
    result = policy_settings.normalize_settings({})
    assert result["live_activities"]["person_restricted"] is True


def test_normalize_person_restricted_boolean_is_derived_not_stored():
    # Retired field (one alerts stack): an explicit False is ignored, the
    # person/off_limits outcome cell is the authority.
    result = policy_settings.normalize_settings({
        "live_activities": {"person_restricted": False},
    })
    assert result["live_activities"]["person_restricted"] is True


def test_settings_delivery_sticky(tmp_path):
    """PUT omitting delivery must not reset it."""
    settings_path = tmp_path / "push_settings.json"
    initial = policy_settings.default_settings()
    initial["live_activities"]["delivery"] = "notifications"
    policy_settings.save_settings(settings_path, initial)
    policy_settings.apply_settings(initial)

    # Simulate a PUT that omits delivery
    body = {"live_activities": {"person": True}}
    merged = policy_settings.normalize_settings(body)
    # Apply sticky logic (mirrors routes/push.py PUT handler)
    la_body = body.get("live_activities", {})
    active_la = policy_settings.get_active().get("live_activities", {})
    if la_body.get("delivery") not in ("la_first", "notifications"):
        merged["live_activities"]["delivery"] = active_la.get("delivery", "la_first")
    assert merged["live_activities"]["delivery"] == "notifications"


# -- la_capable on Device ----------------------------------------------------

def test_device_la_capable_default_true():
    from marcellus.push.models import Device
    d = Device(apns_token="tok", device_id="d1", bundle_id="com.test", environment="sandbox",
               push_to_start_token="pts")
    assert d.la_capable is True
    assert d.can_live_activity is True


def test_device_la_capable_false_blocks_la():
    from marcellus.push.models import Device
    d = Device(apns_token="tok", device_id="d1", bundle_id="com.test", environment="sandbox",
               push_to_start_token="pts", la_capable=False)
    assert d.la_capable is False
    assert d.can_live_activity is False


def test_la_capable_stored_in_db():
    from marcellus import db
    from marcellus.push import store

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sidecar.db"
        conn = db.open_sidecar(db_path)
        store.upsert_device(
            conn, apns_token="tok1", bundle_id="com.test",
            environment="sandbox", la_capable=False,
        )
        conn.commit()
        device = store.get_device(conn, "tok1")
        assert device is not None
        assert device.la_capable is False

        store.upsert_device(
            conn, apns_token="tok2", bundle_id="com.test",
            environment="sandbox",
        )
        conn.commit()
        device2 = store.get_device(conn, "tok2")
        assert device2 is not None
        assert device2.la_capable is True
        conn.close()


# -- person_restricted family ------------------------------------------------

def test_classify_family_off_limits_is_person_restricted():
    assert la.classify_family(
        subject_kind="stranger", label="person", place_class="off_limits",
    ) == la.PERSON_RESTRICTED


def test_classify_family_routed_person_is_person():
    assert la.classify_family(
        subject_kind="stranger", label="person", place_class="doors", level="notify",
    ) == la.PERSON
    assert la.classify_family(
        subject_kind="stranger", label="person", place_class="private", level="urgent",
    ) == la.PERSON
    assert la.classify_family(
        subject_kind="stranger", label="person", place_class="doors", level="quiet",
    ) is None


def test_person_restricted_glyph():
    assert la.glyph_for(
        la.PERSON_RESTRICTED, subject_kind="stranger", label="person", mutation="enrich",
    ) == "figure.walk"
    assert la.glyph_for(
        la.PERSON_RESTRICTED, subject_kind="known", label="person", mutation="enrich",
    ) == "figure.wave"


# -- Category audit ----------------------------------------------------------

def test_card_push_includes_category():
    from marcellus.push.cards import CREATE, Card
    from marcellus.push.delivery import build_card_payload

    card = Card(card_key="test:stranger:t1", level="notify", created_at=1000.0, updated_at=1000.0)
    payload = build_card_payload(
        card, CREATE, sound=True, subject_kind="stranger", place_class="doors",
        label="person", camera="doorbell", zone_name="front_door",
        glyph="figure.walk", primary="Person", secondary="Front Door",
        event_ts=1000.0,
    )
    assert payload["aps"]["category"] == "card.notify"


def test_card_push_category_matches_level():
    from marcellus.push.cards import ESCALATE, Card
    from marcellus.push.delivery import build_card_payload

    card = Card(card_key="test:stranger:t1", level="urgent", created_at=1000.0, updated_at=1000.0)
    payload = build_card_payload(
        card, ESCALATE, sound=True, subject_kind="stranger", place_class="doors",
        label="person", camera="doorbell", zone_name="front_door",
        glyph="figure.walk", primary="Person", secondary="Front Door",
        event_ts=1000.0,
    )
    assert payload["aps"]["category"] == "card.urgent"


# -- Escalation sound -------------------------------------------------------

def test_escalation_sound_default_urgent():
    from marcellus.push.delivery import sound_name_for_card
    assert sound_name_for_card("urgent", "stranger", "person") == "urgent.caf"


def test_escalation_sound_custom():
    from marcellus.push.delivery import sound_name_for_card
    assert sound_name_for_card("urgent", "stranger", "person",
                               escalation_sound="at-the-door") == "at-the-door.caf"


def test_escalation_sound_non_urgent_unaffected():
    from marcellus.push.delivery import sound_name_for_card
    assert sound_name_for_card("notify", "stranger", "person",
                               escalation_sound="siren") == "at-the-door.caf"
    assert sound_name_for_card("notify", "thing", "package",
                               escalation_sound="siren") == "package-delivery.caf"


def test_escalation_sound_in_card_payload():
    from marcellus.push.cards import ESCALATE, Card
    from marcellus.push.delivery import build_card_payload

    card = Card(card_key="test:stranger:t1", level="urgent", created_at=1000.0, updated_at=1000.0)
    payload = build_card_payload(
        card, ESCALATE, sound=True, subject_kind="stranger", place_class="doors",
        label="person", camera="doorbell", zone_name="front_door",
        glyph="figure.walk", primary="Person", secondary="Front Door",
        event_ts=1000.0, escalation_sound="siren",
    )
    assert payload["aps"]["sound"] == "siren.caf"


def test_escalation_sound_settings_default():
    defaults = policy_settings.default_settings()
    assert defaults["escalation_sound"] == "urgent"


def test_escalation_sound_settings_normalize():
    result = policy_settings.normalize_settings({"escalation_sound": "at-the-door"})
    assert result["escalation_sound"] == "at-the-door"


def test_escalation_sound_settings_absent_keeps_default():
    result = policy_settings.normalize_settings({})
    assert result["escalation_sound"] == "urgent"


# -- Interruption-level mapping (pin all four) --------------------------------

def _payload_for_level(level):
    from marcellus.push.cards import CREATE, Card
    from marcellus.push.delivery import build_card_payload
    card = Card(card_key="test:stranger:t1", level=level, created_at=1000.0, updated_at=1000.0)
    return build_card_payload(
        card, CREATE, sound=True, subject_kind="stranger", place_class="doors",
        label="person", camera="doorbell", zone_name="front_door",
        glyph="figure.walk", primary="P", secondary="S", event_ts=1000.0,
    )


def test_interruption_level_urgent_is_time_sensitive():
    assert _payload_for_level("urgent")["aps"]["interruption-level"] == "time-sensitive"


def test_interruption_level_notify_is_active():
    assert _payload_for_level("notify")["aps"]["interruption-level"] == "active"


def test_interruption_level_quiet_is_passive():
    assert _payload_for_level("quiet")["aps"]["interruption-level"] == "passive"


def test_interruption_level_log_does_not_push():
    from marcellus.push.delivery import should_push
    assert should_push("log") is False
