"""Unit tests for `push/live_activities.py`: family detection, glyph
mapping, and the three card-model Live Activity payload shapes (Elsinore
Phase 3)."""

from __future__ import annotations

from marcellus.push import live_activities as la
from marcellus.push.cards import CREATE, ENRICH, RESOLVE


def test_package_family():
    # V3: package is a subject of its own; the family follows the routed
    # level like the person family always has.
    assert la.should_start_activity(
        subject_kind="package", label="package", place_class="yard", level="quiet",
    ) == la.PACKAGE
    # The thing+label spelling still classifies (old applied tables).
    assert la.should_start_activity(
        subject_kind="thing", label="package", place_class="yard", level="quiet",
    ) == la.PACKAGE
    # A log-routed cell mints no activity — that's the ladder saying no.
    assert la.should_start_activity(
        subject_kind="package", label="package", place_class="street", level="log",
    ) is None


def test_bins_family_matches_bin_and_truck_labels():
    assert la.should_start_activity(
        subject_kind="bin", label="waste_bin", place_class="street", level="quiet",
    ) == la.BINS
    assert la.should_start_activity(
        subject_kind="thing", label="garbage_truck", place_class="street", level="quiet",
    ) == la.BINS


def test_openings_family_matches_door_gate_garage():
    for label in ("door", "gate", "garage"):
        assert la.should_start_activity(
            subject_kind="opening", label=label, place_class="doors", level="quiet",
        ) == la.OPENINGS


def test_person_family_follows_routing_not_place_class():
    # The routing table is the authority: a person whose card routes
    # notify/urgent gets an LA wherever they are — the old doors gate meant
    # entry zones classified Private could never start one (2026-08-14).
    for place in ("doors", "yard", "private", "street"):
        assert la.should_start_activity(
            subject_kind="stranger", label="person", place_class=place, level="notify",
        ) == la.PERSON
    assert la.should_start_activity(
        subject_kind="known", label="person", place_class="private", level="urgent",
    ) == la.PERSON
    # Logged/quiet people don't mint activities.
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="doors", level="quiet",
    ) is None
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="yard", level="log",
    ) is None


def test_person_restricted_family_off_limits():
    # Restricted is place-gated regardless of level — the user classified
    # that ground as off-limits, which is itself routing vocabulary.
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="off_limits",
    ) == la.PERSON_RESTRICTED
    assert la.should_start_activity(
        subject_kind="known", label="person", place_class="off_limits",
    ) == la.PERSON_RESTRICTED
    assert la.should_start_activity(
        subject_kind="person", label="person", place_class="off_limits",
    ) == la.PERSON_RESTRICTED


def test_non_qualifying_cards_return_none():
    assert la.should_start_activity(
        subject_kind="animal", label="dog", place_class="yard",
    ) is None
    assert la.should_start_activity(
        subject_kind="thing", label="car", place_class="street",
    ) is None
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="street",
    ) is None


def test_opening_picks_empty_is_permissive():
    # Nothing curated yet -- every opening qualifies (Elsinore Phase 4).
    assert la.should_start_activity(
        subject_kind="opening", label="garage", place_class="street", level="quiet",
        opening_picks=[], opening_ids=("garage-cam",),
    ) == la.OPENINGS


def test_opening_picks_restricts_to_curated_openings():
    assert la.should_start_activity(
        subject_kind="opening", label="garage", place_class="street", level="quiet",
        opening_picks=["front_gate"], opening_ids=("garage-cam", "garage"),
    ) is None
    assert la.should_start_activity(
        subject_kind="opening", label="gate", place_class="street", level="quiet",
        opening_picks=["front_gate"], opening_ids=("front_gate", "doorbell"),
    ) == la.OPENINGS


def test_opening_picks_do_not_affect_other_families():
    assert la.should_start_activity(
        subject_kind="package", label="package", place_class="yard", level="quiet",
        opening_picks=["front_gate"], opening_ids=("some-other-camera",),
    ) == la.PACKAGE


def test_log_routed_family_falls_back_to_catch_all_when_la_only():
    # la_only: a curated family the ladder routed to log still gets *an*
    # activity -- the catch-all -- rather than none at all.
    assert la.should_start_activity(
        subject_kind="package", label="package", place_class="street", level="log",
        catch_all=True,
    ) == la.CATCH_ALL


def test_opening_picks_mismatch_falls_back_to_catch_all_when_la_only():
    assert la.should_start_activity(
        subject_kind="opening", label="garage", place_class="street", level="quiet",
        opening_picks=["front_gate"], opening_ids=("garage-cam", "garage"),
        catch_all=True,
    ) == la.CATCH_ALL


def test_eligible_family_wins_over_catch_all():
    # la_only on, but the family is eligible -- the native family is used,
    # not the catch-all.
    assert la.should_start_activity(
        subject_kind="package", label="package", place_class="yard", level="quiet",
        catch_all=True,
    ) == la.PACKAGE


def test_glyph_for_resolve_wins_over_family():
    for family in la.FAMILIES:
        assert la.glyph_for(
            family, subject_kind="thing", label="package", mutation=RESOLVE,
        ) == "checkmark.circle.fill"


def test_glyph_for_person_known_vs_stranger():
    assert la.glyph_for(
        la.PERSON, subject_kind="known", label="person", mutation=ENRICH,
    ) == "figure.wave"
    assert la.glyph_for(
        la.PERSON, subject_kind="stranger", label="person", mutation=ENRICH,
    ) == "figure.walk"


def test_glyph_for_openings_by_label():
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="garage", mutation=ENRICH,
    ) == "door.garage.open.trianglebadge.exclamationmark"
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="gate", mutation=ENRICH,
    ) == "pedestrian.gate.open"
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="door", mutation=ENRICH,
    ) == "door.left.hand.open"


def test_glyph_for_package_and_bins():
    assert la.glyph_for(
        la.PACKAGE, subject_kind="thing", label="package", mutation=CREATE,
    ) == "shippingbox.fill"
    assert la.glyph_for(
        la.BINS, subject_kind="thing", label="waste_bin", mutation=CREATE,
    ) == "trash.fill"


def _content_state(**overrides):
    base = dict(
        level="notify", mutation="create", glyph="figure.walk",
        primary="Person at front door", secondary="Just now", elapsed_seconds=0,
        card_key="doorbell:stranger:1786235300-aywxqj",
        thumbnail_handle="h_OC_02EpiY-Q", thumbnail_revision=0,
    )
    base.update(overrides)
    return la.build_content_state(**base)


def test_build_content_state_field_names_are_snake_case_and_exact():
    state = _content_state()
    assert set(state) == {
        "level", "mutation", "glyph", "primary", "secondary", "elapsed_seconds",
        "deep_link_card_key", "thumbnail_handle", "thumbnail_revision",
    }
    assert state["deep_link_card_key"] == "doorbell:stranger:1786235300-aywxqj"


def test_build_content_state_story_started_ts_is_additive():
    assert "story_started_ts" not in _content_state()
    state = _content_state(story_started_ts=1786235300.5, state_since_ts=1786235340.0)
    assert state["story_started_ts"] == 1786235300.5
    assert state["state_since_ts"] == 1786235340.0


def test_build_la_start_payload_shape():
    state = _content_state()
    payload = la.build_la_start_payload(
        content_state=state, family="person", camera="doorbell",
        track_id="1786235300-aywxqj", card_key="doorbell:stranger:1786235300-aywxqj",
        now=1786235302, sound="at-the-door.caf",
    )
    aps = payload["aps"]
    assert aps["timestamp"] == 1786235302
    assert aps["event"] == "start"
    assert aps["content-state"] == state
    assert aps["attributes-type"] == "ElsinoreActivityAttributes"
    assert aps["alert"] == {
        "title": "Person at front door", "body": "Just now", "sound": "at-the-door.caf"
    }
    assert aps["attributes"] == {
        "card_key": "doorbell:stranger:1786235300-aywxqj",
        "family": "person",
        "camera": "doorbell",
        "track_id": "1786235300-aywxqj",
    }


def test_build_la_start_payload_alert_required():
    """iOS rejects push-to-start without aps.alert."""
    state = _content_state()
    payload = la.build_la_start_payload(
        content_state=state, family="package", camera="yard",
        track_id="t1", card_key="yard:thing:t1", now=1000,
    )
    alert = payload["aps"]["alert"]
    assert isinstance(alert, dict)
    assert alert["title"]
    assert alert["body"]
    assert "sound" not in payload["aps"]


def test_build_la_start_payload_no_sound_when_omitted():
    state = _content_state()
    payload = la.build_la_start_payload(
        content_state=state, family="person", camera="doorbell",
        track_id="t1", card_key="k", now=1000,
    )
    assert "sound" not in payload["aps"]


def test_build_la_update_payload_shape():
    state = _content_state(mutation="enrich", level="quiet")
    payload = la.build_la_update_payload(content_state=state, now=1786235310)
    assert payload["aps"]["timestamp"] == 1786235310
    assert payload["aps"]["event"] == "update"
    assert payload["aps"]["content-state"] == state
    assert "attributes" not in payload["aps"]
    assert "attributes-type" not in payload["aps"]


def test_build_la_end_payload_dismissal_is_timestamp_plus_thirty():
    state = _content_state(mutation="resolve", thumbnail_handle=None)
    payload = la.build_la_end_payload(content_state=state, now=1786235329)
    aps = payload["aps"]
    assert aps["event"] == "end"
    assert aps["timestamp"] == 1786235329
    assert aps["dismissal-date"] == 1786235359
    assert aps["relevance-score"] == 0.75  # default level is "notify"
    assert "thumbnail_handle" not in aps["content-state"]


# -- §8 instrument fields ---------------------------------------------------

def test_content_state_with_all_s8_fields():
    """Target payload 1: silent update with all §8 fields."""
    state = _content_state(
        level="notify", mutation="enrich",
        state_since_ts=1786337290.0,
        motion={"heading": "approaching", "speed_label": "walking"},
        zones={"ladder": ["Street", "Path", "Porch"], "current_index": 1},
        path={"points": [[0.05, 0.90], [0.15, 0.84], [0.24, 0.78],
                          [0.36, 0.68], [0.47, 0.56], [0.56, 0.44]]},
    )
    assert state["state_since_ts"] == 1786337290.0
    assert state["motion"] == {"heading": "approaching", "speed_label": "walking"}
    assert state["zones"]["ladder"] == ["Street", "Path", "Porch"]
    assert state["zones"]["current_index"] == 1
    assert len(state["path"]["points"]) == 6
    assert state["elapsed_seconds"] == 0  # still present


def test_content_state_s8_fields_omitted_when_none():
    """Legacy shape: no §8 fields when not provided."""
    state = _content_state()
    for key in ("state_since_ts", "motion", "zones", "path"):
        assert key not in state


def test_content_state_motion_without_speed():
    """Target payload 2: stationary, no speed_label."""
    state = _content_state(
        motion={"heading": "stationary"},
        state_since_ts=1786337290.0,
    )
    assert state["motion"] == {"heading": "stationary"}
    assert "speed_label" not in state["motion"]


def test_content_state_motion_carries_distance_ft():
    """World-model distance rides in motion verbatim (additive contract:
    the app treats a missing key as don't-render)."""
    state = _content_state(motion={"heading": "approaching", "distance_ft": 30})
    assert state["motion"]["distance_ft"] == 30


def test_content_state_size_under_budget():
    state = _content_state(
        level="urgent", mutation="escalate", glyph="figure.stand",
        primary="Still at Front Door", secondary="Front Door · 2m",
        elapsed_seconds=126,
        state_since_ts=1786337290.0,
        motion={"heading": "approaching", "speed_label": "running", "distance_ft": 100},
        zones={"ladder": ["Street", "Path", "Porch", "Yard", "Private"], "current_index": 2},
        path={"points": [[round(i * 0.03, 2), round(0.9 - i * 0.02, 2)] for i in range(30)]},
    )
    import json
    size = len(json.dumps(state, separators=(",", ":")).encode())
    assert size < 4096


# -- Path downsampling -------------------------------------------------------

def test_downsample_path_preserves_first_and_last():
    raw = [[i * 0.01, i * 0.02] for i in range(50)]
    result = la.downsample_path(raw, max_points=10)
    assert len(result) == 10
    assert result[0] == [0.0, 0.0]
    assert result[-1] == [round(49 * 0.01, 2), round(49 * 0.02, 2)]


def test_downsample_path_max_30_points():
    raw = [[i * 0.005, i * 0.005] for i in range(100)]
    result = la.downsample_path(raw)
    assert len(result) <= 30


def test_downsample_path_coordinates_in_range():
    raw = [[-0.1, 1.5], [0.5, 0.5], [2.0, -0.3]]
    result = la.downsample_path(raw)
    for x, y in result:
        assert 0.0 <= x <= 1.0
        assert 0.0 <= y <= 1.0


def test_downsample_path_two_decimal_precision():
    raw = [[0.12345, 0.67891]]
    result = la.downsample_path(raw)
    assert result == [[0.12, 0.68]]


def test_downsample_path_passthrough_when_under_limit():
    raw = [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]
    result = la.downsample_path(raw)
    assert len(result) == 3


def test_downsample_path_empty():
    assert la.downsample_path([]) == []
