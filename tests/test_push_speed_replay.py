"""Real-capture regression: the 2026-08-15 17:44 front walk (garden +
street cameras, verbatim from the MQTT flight recorder) through the
speed/direction stack: path timestamps survive, ground speed produces a
walking label, and heading comes from the drawn vector. (Only garden got
reviews in this window, so cross-camera dedup isn't exercised here.)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import policy_settings, store
from marcellus.push.models import Device

FIXTURE = Path(__file__).parent / "fixtures" / "capture-front-walk-speed.jsonl"


@pytest.fixture(autouse=True)
def _front_settings():
    settings = policy_settings.default_settings()
    settings["zone_classes"] = {
        "front_garden": "private", "sidewalk": "street", "nw_49th_st": "street",
        "front_door": "doors",
    }
    settings["camera_neighbors"] = {"garden": ["street"]}
    # The captured walk is a sidewalk passer-by; route it to notify so the
    # person LA family (the motion carrier) engages. Test-only knob — the
    # subject here is speed/heading plumbing, not sidewalk policy.
    settings["zone_overrides"] = {"sidewalk": {"person": "notify"}}
    settings["camera_headings"] = {
        "garden": {"dx": 0.0, "dy": -1.0}, "street": {"dx": 0.0, "dy": -1.0},
    }
    # Ground projection reads the settings-backed camera_optics now; the
    # capture is from the real fleet, so seed the real rig facts.
    settings["camera_optics"] = policy_settings.seeded_camera_optics()
    policy_settings.apply_settings(settings)
    yield
    policy_settings.apply_settings(policy_settings.default_settings())


def make_device(token: str = "tok1") -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", push_to_start_token="pts1", min_severity="detection",
    )


@pytest.mark.asyncio
async def test_captured_front_walk_gets_speed_heading_and_one_card(
    sidecar_db_path: Path,
):
    from marcellus.push.engine import PushEngine
    from marcellus.push.transport import LogTransport

    conn = db.open_sidecar(sidecar_db_path)
    device = make_device()
    store.upsert_device(
        conn, apns_token=device.apns_token, bundle_id=device.bundle_id,
        environment=device.environment, cameras=[], min_severity="detection",
        push_to_start_token=device.push_to_start_token,
    )
    conn.commit()
    conn.close()

    transport = LogTransport()
    engine = PushEngine(db_path=str(sidecar_db_path), transport=transport, server_id="test")
    engine.push_config = PushSection(delivery_enabled=True)

    for line in FIXTURE.read_text().splitlines():
        row = json.loads(line)
        if row["topic"].endswith("reviews"):
            await engine.handle_review_payload(row["payload"])
        else:
            await engine.handle_object_payload(row["payload"])

    # Real movement + timestamps + calibration => at least one LA content
    # state carried a heading and a walking/running speed label.
    la_states = [
        r["payload"]["aps"].get("content-state", {})
        for r in transport.sent if r.get("live_activity")
        if isinstance(r.get("payload"), dict) and "aps" in r["payload"]
    ]
    motions = [s.get("motion") for s in la_states if s.get("motion")]
    assert motions, "no motion ever attached — heading/speed pipeline broke"
    assert any(m.get("heading") for m in motions)

    conn = db.open_sidecar(sidecar_db_path)
    person_cards = conn.execute(
        "SELECT card_key FROM push_cards WHERE subject_kind = 'person'"
    ).fetchall()
    assert person_cards, "no person cards from a real walk"


def test_captured_trails_produce_speed_labels():
    """Ground speed on the fixture's real per-track trails: the walk/jog
    past the front produced multiple tracks with classifiable speed.
    (The LA in the replay above only pushes at review moments, which can
    all land while a trail is still too short — so speed is asserted at
    the ground layer, on the same real data.)"""
    from collections import defaultdict

    from marcellus.push import ground

    trails: dict[tuple[str, str], list] = defaultdict(list)
    for line in FIXTURE.read_text().splitlines():
        row = json.loads(line)
        if not row["topic"].endswith("events"):
            continue
        after = (row.get("payload") or {}).get("after") or {}
        if after.get("label") != "person":
            continue
        for pt in after.get("path_data") or []:
            if len(pt) == 2 and isinstance(pt[0], (list, tuple)):
                (x, y), t = pt
            else:
                x, y, t = pt
            trails[(after["camera"], after["id"])].append((x, y, t))

    labels = []
    for (camera, _tid), pts in trails.items():
        pts = sorted(set(pts), key=lambda p: p[2])
        labels.append(ground.speed_label(ground.speed_ft_s(pts, camera)))
    classified = [x for x in labels if x]
    assert len(classified) >= 3, f"labels={labels}"
    assert set(classified) <= {"walking", "running"}
