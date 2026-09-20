from __future__ import annotations

from marcellus.push.decision import devices_for_event, matches, parse_review_message
from marcellus.push.models import Device, ReviewEvent


def _review_payload(
    *, msg_type="new", severity="alert", camera="doorbell", review_id="r1",
    objects=("person",), detections=("1785123902.717381-2joc0p",), end_time=None,
):
    after = {
        "id": review_id,
        "camera": camera,
        "severity": severity,
        "data": {"objects": list(objects), "detections": list(detections), "zones": []},
    }
    if end_time is not None:
        after["end_time"] = end_time
    return {"type": msg_type, "after": after}


def test_parse_new_alert():
    event = parse_review_message(_review_payload())
    assert event is not None
    assert event.review_id == "r1"
    assert event.camera == "doorbell"
    assert event.severity == "alert"
    assert event.labels == ("person",)
    assert event.event_id == "1785123902.717381-2joc0p"


def test_parse_update_is_actionable():
    event = parse_review_message(_review_payload(msg_type="update"))
    assert event is not None
    assert event.msg_type == "update"


def test_parse_end_is_parsed_but_not_a_push_trigger():
    """`end` is parsed through (encounters/service.py's observe_review, wired
    as PushEngine.on_review, needs it to fill in an atom's end_time) -- but
    it still finalizes a review rather than being pushed: PushEngine.
    handle_event short-circuits on msg_type == "end" before any push logic
    runs, so nothing downstream of parse_review_message changes behavior."""
    event = parse_review_message(_review_payload(msg_type="end"))
    assert event is not None
    assert event.msg_type == "end"


def test_parse_end_populates_end_time():
    event = parse_review_message(_review_payload(msg_type="end", end_time=1785123999.5))
    assert event is not None
    assert event.end_time == 1785123999.5


def test_parse_new_leaves_end_time_none():
    event = parse_review_message(_review_payload())
    assert event is not None
    assert event.end_time is None


def test_parse_missing_severity_dropped():
    payload = _review_payload()
    payload["after"]["severity"] = "unknown"
    assert parse_review_message(payload) is None


def test_parse_malformed_payload_returns_none():
    assert parse_review_message({}) is None
    assert parse_review_message({"type": "new"}) is None
    assert parse_review_message({"type": "new", "after": "not-a-dict"}) is None


def test_event_id_falls_back_to_review_id_with_no_detections():
    event = parse_review_message(_review_payload(detections=()))
    assert event is not None
    assert event.event_id == event.review_id == "r1"


def _device(**kwargs):
    defaults = dict(
        apns_token="tok1", device_id="d_abc", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", cameras=(), labels=(), min_severity="alert",
    )
    defaults.update(kwargs)
    return Device(**defaults)


def test_matches_all_cameras_all_labels():
    device = _device()
    event = ReviewEvent(review_id="r1", camera="garden", severity="alert", labels=("car",))
    assert matches(device, event)


def test_matches_camera_filter():
    device = _device(cameras=("doorbell",))
    on_camera = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    off_camera = ReviewEvent(review_id="r2", camera="garden", severity="alert")
    assert matches(device, on_camera)
    assert not matches(device, off_camera)


def test_matches_label_filter():
    device = _device(labels=("person", "car"))
    matching = ReviewEvent(review_id="r1", camera="doorbell", severity="alert", labels=("car",))
    nonmatching = ReviewEvent(review_id="r2", camera="doorbell", severity="alert", labels=("dog",))
    assert matches(device, matching)
    assert not matches(device, nonmatching)


def test_matches_severity_threshold():
    alert_only = _device(min_severity="alert")
    detection_event = ReviewEvent(review_id="r1", camera="doorbell", severity="detection")
    alert_event = ReviewEvent(review_id="r2", camera="doorbell", severity="alert")
    assert not matches(alert_only, detection_event)
    assert matches(alert_only, alert_event)

    detection_ok = _device(min_severity="detection")
    assert matches(detection_ok, detection_event)
    assert matches(detection_ok, alert_event)


def test_devices_for_event_filters_list():
    devices = [
        _device(apns_token="a", cameras=("doorbell",)),
        _device(apns_token="b", cameras=("garden",)),
        _device(apns_token="c"),
    ]
    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    matched = devices_for_event(devices, event)
    assert {d.apns_token for d in matched} == {"a", "c"}
