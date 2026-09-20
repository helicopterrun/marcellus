"""The v2 `/v1/push` surface (notification-experience plan §8, handoff 1-6).

Registration keeps every v1 guarantee; the four new endpoints are the app's
whole Phase-1 vocabulary.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.push.engine import PushEngine
from marcellus.push.transport import LogTransport
from marcellus.server import create_app

TOKEN = "tok-abc123"

AT_THE_DOOR: dict[str, Any] = {
    "id": "at-the-door", "name": "At the door", "tier": "interrupt",
    "cameras": ["doorbell"], "labels": ["person"], "zones": ["porch"],
    "loiter_seconds": 5, "sound": "at-the-door",
}


def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        push=PushSection(enabled=False),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    app = create_app(settings)
    app.state.push_engine = PushEngine(
        db_path=str(sidecar_db_path), transport=LogTransport(), server_id="s_test",
        frigate_base_url="",
    )
    return TestClient(app)


def _register(client: TestClient, **overrides: Any) -> Any:
    body: dict[str, Any] = {"bundle_id": "com.x", "environment": "sandbox"}
    body.update(overrides)
    return client.put(f"/v1/push/devices/{TOKEN}", json=body)


# -- registration ------------------------------------------------------------


def test_v1_registration_still_reports_schema_version_1(client: TestClient) -> None:
    r = _register(client, cameras=["doorbell"])
    assert r.status_code == 200
    body = r.json()
    assert body["registered"] is True
    assert body["schema_version"] == 1
    assert body["situations_accepted"] == 0


def test_v2_registration_accepts_the_full_section_8_record(client: TestClient) -> None:
    r = _register(
        client,
        schema_version=2,
        timezone="America/Los_Angeles",
        location={"lat": 45.51, "lon": -122.68},
        situations=[AT_THE_DOOR],
        morning_digest={"enabled": True, "hour": 7, "minute": 0},
        llm={"mode": "off"},
        live_activity_token="la-tok",
        snoozes=[{"scope": "situation:at-the-door", "until": time.time() + 900}],
    )
    assert r.status_code == 200
    assert r.json()["schema_version"] == 2
    assert r.json()["situations_accepted"] == 1


def test_registration_reports_situations_it_could_not_parse(client: TestClient) -> None:
    """A rule the sidecar silently discarded would look enabled in the app
    and never fire."""
    r = _register(client, situations=[AT_THE_DOOR, {"name": "no id here"}])
    assert r.json()["situations_accepted"] == 1


def test_unknown_fields_do_not_422(client: TestClient) -> None:
    """A newer app build must not fail against an older sidecar."""
    r = _register(client, situations=[AT_THE_DOOR], some_future_field={"a": 1})
    assert r.status_code == 200


def test_reregistering_without_snoozes_leaves_them_alone(client: TestClient) -> None:
    """The app re-registers on every launch; a launch must not cancel a
    snooze the user set an hour ago (plan's "survives app kill")."""
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    _register(client, situations=[AT_THE_DOOR])  # no `snoozes` key

    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    assert [s["scope"] for s in r.json()["active"]] == ["global"]


def test_explicit_empty_snoozes_clears_them(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "global", "until_epoch": time.time() + 900},
    )
    _register(client, situations=[AT_THE_DOOR], snoozes=[])
    r = client.delete(f"/v1/push/snooze/global?apns_token={TOKEN}")
    assert r.json()["active"] == []


# -- starter library + sounds ------------------------------------------------


def test_starter_library_has_the_four_named_starters(client: TestClient) -> None:
    r = client.get("/v1/push/situations/library")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()]
    assert ids == [
        "at-the-door", "near-my-car", "package-delivery", "unknown-vehicle-parked"
    ]


def test_starter_library_round_trips_into_a_registration(client: TestClient) -> None:
    """The library's output must be directly usable as registration input --
    the app enables a starter with one tap and edits its zones."""
    starters = client.get("/v1/push/situations/library").json()
    r = _register(client, situations=starters)
    assert r.json()["situations_accepted"] == len(starters)


def test_starter_sounds_all_exist_in_the_catalog(client: TestClient) -> None:
    catalog = {s["id"] for s in client.get("/v1/push/sounds").json()}
    for starter in client.get("/v1/push/situations/library").json():
        assert starter["sound"] in catalog


def test_sound_catalog_shape(client: TestClient) -> None:
    r = client.get("/v1/push/sounds")
    assert r.status_code == 200
    assert all(set(s) == {"id", "name"} for s in r.json())
    assert client.get("/v1/push/sounds?app_version=1.0").status_code == 200


# -- snooze ------------------------------------------------------------------


def test_snooze_and_unsnooze_round_trip(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    until = time.time() + 900
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "situation:at-the-door", "until_epoch": until},
    )
    assert r.status_code == 200 and r.json()["snoozed"] is True

    r = client.delete(f"/v1/push/snooze/situation:at-the-door?apns_token={TOKEN}")
    assert r.status_code == 200
    assert r.json()["active"] == []


def test_snooze_for_unknown_device_is_404(client: TestClient) -> None:
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": "never-registered", "scope": "global", "until_epoch": 1},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "device_not_found"


@pytest.mark.parametrize("scope", ["", "situation:", "camera:", "everything"])
def test_malformed_scope_is_rejected(client: TestClient, scope: str) -> None:
    """A scope that silences nothing while looking like it took is worse than
    an error."""
    _register(client, situations=[AT_THE_DOOR])
    r = client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": scope, "until_epoch": time.time() + 60},
    )
    assert r.status_code == 422


def test_camera_scope_survives_a_camera_name_with_punctuation(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    client.post(
        "/v1/push/snooze",
        json={"apns_token": TOKEN, "scope": "camera:back-yard", "until_epoch": time.time() + 60},
    )
    r = client.delete(f"/v1/push/snooze/camera:back-yard?apns_token={TOKEN}")
    assert r.json()["active"] == []


def test_unsnoozing_something_never_snoozed_is_still_200(client: TestClient) -> None:
    r = client.delete(f"/v1/push/snooze/global?apns_token={TOKEN}")
    assert r.status_code == 200


# -- per-situation test push -------------------------------------------------


def test_situation_test_push_fires_the_real_shape(client: TestClient) -> None:
    _register(client, situations=[AT_THE_DOOR])
    r = client.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    assert r.status_code == 200
    assert r.json() == {"sent": True, "situation_id": "at-the-door"}

    transport = client.app.state.push_engine.transport
    payload = transport.sent[-1]["payload"]
    assert payload["situation_id"] == "at-the-door"
    assert payload["aps"]["category"] == "situation.at-the-door"
    assert payload["handle"].startswith("h_")


def test_situation_test_does_not_spend_the_hourly_budget(
    client: TestClient, sidecar_db_path: Path
) -> None:
    _register(client, situations=[AT_THE_DOOR])
    for _ in range(3):
        client.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    conn = db.open_sidecar(sidecar_db_path)
    assert store.count_sends_since(
        conn, apns_token=TOKEN, situation_id="at-the-door", since=0
    ) == 0
    conn.close()


def test_situation_test_for_unregistered_situation_is_404(client: TestClient) -> None:
    """Falling back to the starter library would test a rule the device isn't
    registered with."""
    _register(client, situations=[AT_THE_DOOR])
    r = client.post("/v1/push/test/near-my-car", json={"apns_token": TOKEN})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "situation_not_found"


def test_situation_test_for_unknown_device_is_404(client: TestClient) -> None:
    r = client.post("/v1/push/test/at-the-door", json={"apns_token": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "device_not_found"


def test_situation_test_with_push_disabled_is_503(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """503, never 404: the released client reads a 404 on the test path as
    "this server is too old", which would be the wrong story entirely."""
    app = create_app(_settings(frigate_db_path, sidecar_db_path, tmp_path))
    plain = TestClient(app)  # no push_engine on app.state
    plain.put(
        f"/v1/push/devices/{TOKEN}",
        json={"bundle_id": "com.x", "environment": "sandbox", "situations": [AT_THE_DOOR]},
    )
    r = plain.post("/v1/push/test/at-the-door", json={"apns_token": TOKEN})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "push_disabled"


# -- thumbnail ---------------------------------------------------------------


def test_thumbnail_round_trip(client: TestClient, sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="ev1", review_id="r1", ttl_s=3600
    )
    store.store_thumbnail(conn, handle, b"\xff\xd8\xff-not-really-a-jpeg")
    conn.commit()
    conn.close()

    r = client.get(f"/v1/push/thumbnail/{handle}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8\xff-not-really-a-jpeg"


def test_thumbnail_miss_is_404_not_a_hang(client: TestClient) -> None:
    """The NSE delivers the alert without an image on a miss -- the visible
    push is the promise, the image is not."""
    r = client.get("/v1/push/thumbnail/h_nope")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "thumbnail_not_found"


def test_expired_handle_yields_no_thumbnail(
    client: TestClient, sidecar_db_path: Path
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="ev1", review_id="r1", ttl_s=-1
    )
    store.store_thumbnail(conn, handle, b"jpeg")
    conn.commit()
    conn.close()
    assert client.get(f"/v1/push/thumbnail/{handle}").status_code == 404


def test_thumbnail_is_exempt_from_frigate_auth(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """Unlike every other `/v1/push` route: the iOS Notification Service
    Extension fetches this one and holds no Frigate session (`auth.py`'s
    `EXEMPT_PREFIXES` docstring has the full rationale -- the handle itself,
    opaque/unguessable/short-lived, is the access control). A miss still
    404s rather than hanging or leaking whether a handle ever existed.
    """
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    settings.sidecar.require_frigate_auth = True
    authed = TestClient(create_app(settings))
    assert authed.get("/v1/push/thumbnail/h_x").status_code == 404


def test_other_push_routes_still_require_auth(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    settings.sidecar.require_frigate_auth = True
    authed = TestClient(create_app(settings))
    assert authed.get("/v1/push/handle/h_x").status_code in (401, 403)
    assert authed.get("/v1/push/situations/library").status_code in (401, 403)


def test_unknown_registration_fields_are_logged_not_swallowed(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Silently dropping a field the app believes it sent is how a missing
    `push_to_start_token` looks like an app-side bug for a day."""
    import logging

    with caplog.at_level(logging.INFO, logger="marcellus.routes.push"):
        r = _register(client, some_future_field={"a": 1}, another_one="x")
    assert r.status_code == 200
    assert "another_one, some_future_field" in caplog.text
    # Names only -- a field the sidecar doesn't understand may still be a token.
    assert "\"a\": 1" not in caplog.text


def test_registration_with_empty_situations_logs_v1_mode(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A `schema_version=2` registration with no situations is legitimate
    backward-compat -- it must still land on a log line saying so, at info
    level, not as a warning."""
    import logging

    with caplog.at_level(logging.INFO, logger="marcellus.routes.push"):
        r = _register(client, schema_version=2, cameras=["doorbell"])
    assert r.status_code == 200
    assert f"apns_token={TOKEN[:8]}" in caplog.text
    assert "uses_situations=False" in caplog.text
    for record in caplog.records:
        assert record.levelno == logging.INFO
    # Never a full token.
    assert TOKEN not in caplog.text


def test_registration_with_situations_logs_situation_mode(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="marcellus.routes.push"):
        r = _register(client, schema_version=2, situations=[AT_THE_DOOR])
    assert r.status_code == 200
    assert f"apns_token={TOKEN[:8]}" in caplog.text
    assert "uses_situations=True" in caplog.text
    assert TOKEN not in caplog.text


def test_reregistration_flipping_situations_logs_the_transition(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The edge that actually matters: a device silently moving from
    situation matching to v1 (or back) must be traceable without a live
    repro -- this is the line Thread A's four-hour trace needed."""
    import logging

    _register(client, schema_version=2, situations=[AT_THE_DOOR])
    with caplog.at_level(logging.INFO, logger="marcellus.routes.push"):
        r = _register(client, schema_version=2, situations=[])
    assert r.status_code == 200
    assert "transitioned uses_situations True -> False" in caplog.text
    assert TOKEN not in caplog.text


def test_at_the_door_is_a_present_tier_starter_that_escalates(client: TestClient) -> None:
    """Phase 2 retiers the poster-child starter: a Live Activity when someone
    walks up, a buzz only if they are still there five seconds later."""
    lib = {s["id"]: s for s in client.get("/v1/push/situations/library").json()}
    door = lib["at-the-door"]
    assert door["tier"] == "present"
    assert door["escalation"] == {
        "from_tier": "present", "to_tier": "escalated", "on": "loiter_exceeds:5",
    }
    assert door["loiter_seconds"] == 5.0


def test_every_present_starter_declares_how_it_escalates_or_stays_quiet(
    client: TestClient,
) -> None:
    """A Present starter with no escalation block never buzzes -- fine, but it
    should be a decision rather than an oversight, so this pins which is which."""
    lib = {s["id"]: s for s in client.get("/v1/push/situations/library").json()}
    escalating = {k for k, v in lib.items() if v.get("escalation")}
    assert escalating == {"at-the-door"}
    assert {k for k, v in lib.items() if v["tier"] == "present"} == {
        "at-the-door", "package-delivery", "unknown-vehicle-parked",
    }
