"""§8 Live Activity cadence: delta-only pushes, ≤6 for a typical 60s visit.

Tests the delta detection function directly — no real-time waits needed.
"""

from __future__ import annotations

from marcellus.push.delivery_wire import _copy, _la_has_visible_delta


def _delta(**kw):
    defaults = dict(
        mutation="enrich", prev_mutation="enrich",
        level="notify", prev_level="notify",
        primary="Person at front door", prev_primary="Person at front door",
        glyph="figure.walk", prev_glyph="figure.walk",
        current_zones=("front_door",), prev_zones=("front_door",),
        path_len=5, prev_path_len=5,
        heading="approaching", prev_heading="approaching",
    )
    defaults.update(kw)
    return _la_has_visible_delta(**defaults)


def test_no_change_suppressed():
    assert _delta() is None


def test_create_always_pushes():
    assert _delta(mutation="create") == "create"


def test_escalate_always_pushes():
    assert _delta(mutation="escalate") == "escalate"


def test_deescalate_always_pushes():
    assert _delta(mutation="deescalate") == "deescalate"


def test_resolve_always_pushes():
    assert _delta(mutation="resolve") == "resolve"


def test_level_change_pushes():
    assert _delta(level="urgent", prev_level="notify") == "level_change"


def test_text_change_pushes():
    assert _delta(primary="Person at porch") == "text_change"


def test_glyph_change_pushes():
    assert _delta(glyph="figure.wave") == "glyph_change"


def test_zone_transition_pushes():
    assert _delta(current_zones=("porch",)) == "zone_transition"


def test_heading_change_pushes():
    assert _delta(heading="leaving") == "heading_change"


def test_path_growth_pushes():
    assert _delta(path_len=8, prev_path_len=5) == "path_growth"


def test_path_growth_below_threshold_suppressed():
    assert _delta(path_len=7, prev_path_len=5) is None


def test_heading_none_suppressed():
    assert _delta(heading=None, prev_heading=None) is None


def test_60s_visit_produces_at_most_6_pushes():
    """Simulate a 60s person visit: create, 4 zone-dwell enrich steps
    (no visible delta), 1 zone transition, 1 enrichment, 1 deescalate,
    1 resolve. Only steps with visible deltas push."""
    steps = [
        dict(mutation="create"),
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich", current_zones=("porch",), prev_zones=("front_door",)),
        dict(mutation="enrich"),  # no change (zones same as last push)
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich", primary="Alex at Porch", prev_primary="Person at front door"),
        dict(mutation="deescalate"),
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="resolve"),
    ]
    pushes = sum(1 for s in steps if _delta(**{**dict(
        prev_mutation="enrich", level="notify", prev_level="notify",
        primary="Person at front door", prev_primary="Person at front door",
        glyph="figure.walk", prev_glyph="figure.walk",
        current_zones=("front_door",), prev_zones=("front_door",),
        path_len=5, prev_path_len=5,
        heading="approaching", prev_heading="approaching",
    ), **s}) is not None)
    assert pushes <= 6, f"expected ≤6 LA pushes, got {pushes}"
    assert pushes >= 4  # create + zone_transition + text_change + deescalate + resolve = 5


# -- Copy text ----------------------------------------------------------------

def test_create_secondary_omits_elapsed_zero():
    # Secondary carries what the title doesn't: the camera, never a repeat
    # of the place ("Person near Front Garden / Front Garden", 2026-08-14).
    primary, secondary = _copy("stranger", "person", "doorbell", "front_garden", 0.0)
    assert primary == "Person near Front Garden"
    assert secondary == "Doorbell camera"
    assert "0s" not in secondary


def test_enrich_secondary_includes_elapsed():
    # Friendly elapsed: sub-minute reads "just now", minutes read "N min".
    _, secondary = _copy("stranger", "person", "doorbell", "front_garden", 45.0)
    assert secondary == "Doorbell camera · just now"
    _, secondary = _copy("stranger", "person", "doorbell", "front_garden", 190.0)
    assert secondary == "Doorbell camera · 3 min"


def test_copy_prefers_frigate_friendly_name():
    # "front_entry_person" is a rule name, not a place — Frigate's
    # friendly_name wins when configured (loaded at startup from config.yml).
    from marcellus.push import policy_settings
    policy_settings._zone_display_names = {"front_entry_person": "Front Walk"}
    try:
        primary, _ = _copy("stranger", "person", "garden", "front_entry_person", 0.0)
        assert primary == "Person near Front Walk"
    finally:
        policy_settings._zone_display_names = {}


def test_copy_prefers_sidecar_zone_name_over_friendly_name():
    # The /zones-page display name (settings zone_names) outranks Frigate's
    # friendly_name — it's the user's own phrasing for notification copy.
    from marcellus.push import policy_settings
    policy_settings._zone_display_names = {"front_entry_person": "Front Walk"}
    saved = policy_settings._active
    try:
        doc = policy_settings.default_settings()
        doc["zone_names"] = {"front_entry_person": "the front path"}
        policy_settings._active = doc
        primary, _ = _copy("stranger", "person", "garden", "front_entry_person", 0.0)
        assert primary == "Person near the front path"
    finally:
        policy_settings._active = saved
        policy_settings._zone_display_names = {}


def test_secondary_empty_when_title_already_used_camera():
    # No zone: the title names the camera as a camera ("Person · Doorbell
    # camera"), so the secondary stays empty on create, elapsed-only later.
    primary, secondary = _copy("stranger", "person", "doorbell", "", 0.0)
    assert primary == "Person · Doorbell camera"
    assert secondary == ""
    _, secondary = _copy("stranger", "person", "doorbell", "", 30.0)
    assert secondary == "just now"


def test_story_leads_the_secondary():
    # Notable verbs only: when a story phrase is passed it leads the body.
    _, secondary = _copy(
        "person", "", "gate", "back_walkway", 200.0, story="still there"
    )
    assert secondary == "still there · Gate camera · 3 min"
    _, secondary = _copy("person", "", "gate", "", 0.0, story="left after 2 min")
    assert secondary == "left after 2 min"
