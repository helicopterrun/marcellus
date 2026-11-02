"""Quiet hours run on the household's clock (the devices' zone), never the
host's, which is usually UTC."""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import decision_trace, policy_settings, store
from marcellus.push.delivery_wire import handle_delivery_event
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport
from marcellus.server import create_app

LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _doc(start: str, end: str, mode: str = "hold_urgent") -> dict:
    doc = policy_settings.default_settings()
    doc["quiet_hours"] = {"start": start, "end": end, "mode": mode}
    return doc


def _device(zone: str) -> Device:
    return Device(
        apns_token="tok1", device_id="d_tok1", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", min_severity="detection", timezone=zone,
    )


def test_zone_is_the_most_common_valid_device_zone() -> None:
    class D:
        def __init__(self, timezone: str) -> None:
            self.timezone = timezone

    pick = policy_settings.household_zone
    assert pick([]) is None and pick([D("")]) is None
    assert str(pick([D("Asia/Tokyo"), D("UTC"), D("UTC")])) == "UTC"
    assert str(pick([D("Not/AZone"), D("Not/AZone"), D("UTC")])) == "UTC"


def test_window_is_read_on_the_zones_clock() -> None:
    doc = _doc("22:00", "07:00")
    night = datetime.datetime(2026, 7, 10, 23, 0, tzinfo=LA).timestamp()
    noon = datetime.datetime(2026, 7, 10, 12, 0, tzinfo=LA).timestamp()
    assert policy_settings.quiet_hours_at(doc, night, LA) == (True, "hold_urgent")
    assert policy_settings.quiet_hours_at(doc, noon, LA) == (False, "")
    # 16:00 in Los Angeles is 23:00 UTC: inside on the wrong clock, the old bug.
    afternoon = datetime.datetime(2026, 7, 10, 16, 0, tzinfo=LA).timestamp()
    assert policy_settings.quiet_hours_at(doc, afternoon, LA) == (False, "")
    assert policy_settings.quiet_hours_at(doc, afternoon, datetime.timezone.utc)[0] is True


def test_window_follows_daylight_saving() -> None:
    doc = _doc("22:00", "23:00")
    # 22:30 local on both sides of the 2026-11-01 change: 05:30 and 06:30 UTC.
    summer = datetime.datetime(2026, 10, 31, 22, 30, tzinfo=LA).timestamp()
    winter = datetime.datetime(2026, 11, 2, 22, 30, tzinfo=LA).timestamp()
    assert policy_settings.quiet_hours_at(doc, summer, LA)[0] is True
    assert policy_settings.quiet_hours_at(doc, winter, LA)[0] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("zone", "held"), [("Asia/Tokyo", True), ("Pacific/Honolulu", False)])
async def test_delivery_holds_on_the_devices_clock(
    sidecar_db_path: Path, zone: str, held: bool
) -> None:
    # 00:30 in Tokyo, 05:30 the day before in Honolulu.
    now = datetime.datetime(2026, 7, 10, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo")).timestamp()
    doc = _doc("00:00", "01:00")
    doc["ladder"]["person"]["doors"] = "urgent"
    policy_settings.apply_settings(doc)
    conn = db.open_sidecar(sidecar_db_path)
    await handle_delivery_event(
        ReviewEvent(
            review_id="r1", camera="doorbell", severity="alert", labels=("person",),
            track_ids=("trk1",), zones=("front_door",),
        ),
        conn=conn, devices=[_device(zone)], transport=LogTransport(),
        config=PushSection(delivery_enabled=True), now=now,
    )
    reasons = decision_trace.recent(conn, 1)[0]["reasons"]
    assert ("quiet_hours_hold" in reasons) is held


def test_status_reports_quiet_hours_on_the_devices_clock(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(yaml.safe_dump({"cameras": {}}))
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        push=PushSection(enabled=False, push_settings_path=str(tmp_path / "push_settings.json")),
    )
    zone = ZoneInfo("Asia/Tokyo")
    local = datetime.datetime.now(zone)
    start = (local - datetime.timedelta(minutes=5)).strftime("%H:%M")
    end = (local + datetime.timedelta(minutes=5)).strftime("%H:%M")
    with TestClient(create_app(settings)) as client:
        policy_settings.apply_settings(_doc(start, end))
        conn = db.open_sidecar(sidecar_db_path)
        store.upsert_device(
            conn, apns_token="tok1", bundle_id="com.pondhouse.Elsinore",
            environment="sandbox", timezone_name="Asia/Tokyo",
        )
        conn.close()
        assert client.get("/v1/push/status").json()["quiet_hours_active"] is True
