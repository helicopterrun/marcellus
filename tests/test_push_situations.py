"""Situation matching, dwell, and tiers (notification-experience plan §1/§2/§8).

The rule these exercise, stated once: notifications should be rare, earned,
and specific. Almost every test below is really asking "does this *not* fire",
because that is the whole point of the phase -- today's system is correct and
annoying, and the situation model exists to make the silent cases silent.
"""

from __future__ import annotations

from marcellus.push.decision import parse_object_message, parse_review_message
from marcellus.push.models import ReviewEvent
from marcellus.push.situations import (
    COLLAPSE_ID_MAX_BYTES,
    Match,
    Situation,
    TrackStore,
    build_collapse_id,
    in_time_window,
    parse_situations,
)

NOW = 1_785_000_000.0


def _event(**kw) -> ReviewEvent:
    base = dict(
        review_id="r1", camera="doorbell", severity="alert", labels=("person",),
        zones=("porch",), track_ids=("t1",), start_time=NOW,
    )
    base.update(kw)
    return ReviewEvent(**base)  # type: ignore[arg-type]


AT_THE_DOOR = Situation(
    id="at-the-door", name="At the door", cameras=("doorbell",), labels=("person",),
    zones=("porch",), loiter_seconds=5.0, audio_events=("doorbell",),
)


def _tracks_in_zone(camera: str, track_id: str, zone: str, *, since: float) -> TrackStore:
    tracks = TrackStore()
    tracks.observe_object(camera, track_id, (zone,), now=since)
    return tracks


# -- the shape of a situation ------------------------------------------------


def test_situation_parses_full_section_8_shape() -> None:
    raw = {
        "id": "at-the-door", "name": "At the door", "tier": "interrupt",
        "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
        "loiter_seconds": 5, "require_stationary": False, "sub_label_allow": [],
        "sub_label_deny": ["known"], "audio_events": ["doorbell"],
        "time_of_day": {"start_hour": 22, "end_hour": 7}, "night_tightening": True,
        "escalation": {"from_tier": "present", "to_tier": "interrupt", "on": "loiter_exceeds:5"},
        "llm_enrich": True, "detection_tier_early_fire": False, "sound": "chime",
    }
    s = Situation.from_dict(raw)
    assert s is not None
    assert (s.id, s.tier, s.loiter_seconds) == ("at-the-door", "interrupt", 5.0)
    assert s.zones == ("porch",) and s.audio_events == ("doorbell",)
    assert s.time_of_day == (22, 7)
    # Accepted and carried even though nothing reads them yet.
    assert s.night_tightening is True and s.llm_enrich is True
    # Phase 2 parses the escalation block rather than carrying the raw dict.
    assert s.escalation is not None
    assert s.escalation.kind == "loiter_exceeds" and s.escalation.threshold == 5.0
    assert s.escalation.to_tier == "interrupt"
    # Round-trips back to the §8 wire spelling for the starter library.
    assert s.to_dict()["escalation"]["on"] == "loiter_exceeds:5"


def test_situation_without_id_is_dropped_not_fatal() -> None:
    # The id keys the collapse id, the rate-limit window and the snooze scope;
    # there is nothing sane to default it to. One bad entry must not cost the
    # device its other rules.
    assert parse_situations([{"name": "nameless"}, {"id": "ok"}]) == (
        Situation.from_dict({"id": "ok"}),
    )


def test_unknown_tier_falls_back_to_interrupt() -> None:
    s = Situation.from_dict({"id": "x", "tier": "URGENT"})
    assert s is not None and s.tier == "interrupt"


# -- what fires and what doesn't --------------------------------------------


def test_time_window_wraps_across_midnight() -> None:
    # NOW is 2026-07-25 17:20 UTC -- afternoon there, 10:20 in Los Angeles.
    assert in_time_window((22, 7), NOW, "UTC") is False  # overnight window, it's 17:00
    assert in_time_window((9, 20), NOW, "UTC") is True  # daytime window
    assert in_time_window(None, NOW, "UTC") is True
    # The window is the *device's* local time, not the sidecar's (plan §7).
    assert in_time_window((16, 18), NOW, "UTC") is True
    assert in_time_window((16, 18), NOW, "America/Los_Angeles") is False


def test_unusable_timezone_does_not_drop_the_push() -> None:
    """A bad IANA name is a registration bug, not a reason to go silent."""
    assert in_time_window((0, 24), NOW, "Mars/Olympus") is True


# -- parsing the wire --------------------------------------------------------


def test_review_message_parse_carries_zones_tracks_and_audio() -> None:
    payload = {
        "type": "new",
        "after": {
            "id": "r1", "camera": "doorbell", "severity": "alert",
            "start_time": NOW,
            "data": {
                "detections": ["ev-1", "ev-2"], "objects": ["person"],
                "zones": ["porch"], "audio": ["doorbell"], "sub_labels": [],
            },
        },
    }
    event = parse_review_message(payload)
    assert event is not None
    assert event.track_ids == ("ev-1", "ev-2")
    assert event.zones == ("porch",) and event.audio == ("doorbell",)
    assert event.event_id == "ev-1" and event.start_time == NOW


def test_object_message_parse_reads_live_occupancy() -> None:
    """Shape verified against this deployment's live broker, 2026-08-05."""
    payload = {
        "type": "update",
        "after": {
            "id": "1785949885.708056-zkh5js", "camera": "alley-wide", "label": "car",
            "current_zones": ["parking_spot"], "entered_zones": ["parking_spot", "alley"],
            "stationary": True, "sub_label": None,
        },
    }
    obj = parse_object_message(payload)
    assert obj is not None
    assert obj.current_zones == ("parking_spot",)
    assert obj.entered_zones == ("parking_spot", "alley")
    assert obj.stationary is True and obj.msg_type == "update"


def test_object_message_sub_label_may_be_a_name_score_pair() -> None:
    payload = {
        "type": "update",
        "after": {"id": "t1", "camera": "c", "sub_label": ["alice", 0.9]},
    }
    obj = parse_object_message(payload)
    assert obj is not None and obj.sub_label == "alice"


# -- collapse ids ------------------------------------------------------------


def test_collapse_id_is_situation_then_track() -> None:
    assert build_collapse_id("at-the-door", "1785949902.99235-kqbwe9") == (
        "at-the-door:1785949902.99235-kqbwe9"
    )


def test_a_long_situation_id_gives_way_so_the_track_id_survives() -> None:
    """APNs truncates at 64 bytes and so does the relay. Losing the tail
    would make two people at the door collapse into one notification."""
    long_id = "a-situation-name-somebody-typed-out-in-full-because-they-could"
    track_a, track_b = "1785949902.99235-kqbwe9", "1785949902.99235-zzzzzz"

    a = build_collapse_id(long_id, track_a)
    b = build_collapse_id(long_id, track_b)
    assert len(a.encode()) <= COLLAPSE_ID_MAX_BYTES
    assert a.endswith(track_a) and b.endswith(track_b)
    assert a != b  # still two notifications, not one replacing the other


def test_collapse_id_is_stable_across_a_tracks_updates() -> None:
    """Whatever it trims to, it must trim to the same thing every time -- an
    unstable collapse id stacks instead of replacing."""
    m = Match(situation=Situation(id="x" * 90, name="X"), track_id="t1", dwell_s=1, label="person",
              zone="porch")
    assert m.collapse_id == m.collapse_id
    assert len(m.collapse_id.encode()) <= COLLAPSE_ID_MAX_BYTES


# -- the store's own housekeeping -------------------------------------------


def test_reaper_drops_stale_tracks() -> None:
    tracks = TrackStore()
    tracks.observe_object("doorbell", "t1", ("porch",), now=NOW)
    tracks.observe_object("doorbell", "t2", ("porch",), now=NOW + 599)
    assert tracks.reap(now=NOW + 700) == 1
    assert len(tracks) == 1


def test_clear_wipes_everything_for_a_frigate_restart() -> None:
    tracks = TrackStore()
    tracks.observe_object("doorbell", "t1", ("porch",), now=NOW)
    tracks.clear()
    assert len(tracks) == 0
