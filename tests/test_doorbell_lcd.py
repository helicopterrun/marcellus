"""M-2: doorbell LCD replies (configurable slots + custom text) and ring
snapshot proxy.

Covers: `LcdPreset` config validation, `GET/POST /v1/doorbell/{camera}/lcd*`,
`GET /v1/doorbell/{camera}/snapshot`, `doorbell_slots` round-trip on
`/v1/push/devices/{token}`, and `doorbell.build_ring_payload`/`handle_ring`'s
`lcd_slots`/`custom_reply` resolution.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from marcellus import db
from marcellus.config import (
    FrigateSection,
    LcdPreset,
    Settings,
    SidecarSection,
    UnifiProtectSection,
)
from marcellus.push import doorbell, store
from marcellus.push.transport import LogTransport
from marcellus.push.unifi_protect import ProtectCameraStatus, ProtectRingSubscriber
from marcellus.server import create_app

# -- config: LcdPreset validation -------------------------------------------


def test_default_lcd_presets_load() -> None:
    section = UnifiProtectSection()
    assert set(section.lcd_presets) == {"leave_package", "be_right_there", "do_not_disturb"}
    assert section.lcd_presets["be_right_there"].text == "BE RIGHT THERE"
    assert section.custom_reply_duration_s == 120
    assert section.custom_reply_max_chars == 30
    assert section.image_duration_s == 300
    assert section.ring_snapshot == "protect"


def test_custom_message_without_text_rejected() -> None:
    with pytest.raises(ValidationError):
        LcdPreset(type="CUSTOM_MESSAGE", duration_s=60, title="Bad")


def test_lcd_preset_duration_bounds() -> None:
    with pytest.raises(ValidationError):
        LcdPreset(type="DO_NOT_DISTURB", duration_s=0, title="Bad")
    with pytest.raises(ValidationError):
        LcdPreset(type="DO_NOT_DISTURB", duration_s=90000, title="Bad")


def test_custom_reply_max_chars_bounds() -> None:
    with pytest.raises(ValidationError):
        UnifiProtectSection(custom_reply_max_chars=0)
    with pytest.raises(ValidationError):
        UnifiProtectSection(custom_reply_max_chars=65)


# -- app/client fixtures -----------------------------------------------------


@pytest.fixture
def _base_settings_kwargs(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> dict:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return {
        "frigate": FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        "sidecar": SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    }


def _client_with_subscriber(
    _base_settings_kwargs: dict,
    *,
    has_lcd: bool = True,
    handler=None,
    unifi_protect_kwargs: dict | None = None,
) -> tuple[TestClient, ProtectRingSubscriber]:
    section = UnifiProtectSection(
        enabled=True,
        console_url="https://console.test",
        api_key="k",
        cameras={"cam-1": "front_door"},
        **(unifi_protect_kwargs or {}),
    )
    settings = Settings(**_base_settings_kwargs, unifi_protect=section)
    client = TestClient(create_app(settings))
    transport = (
        httpx.MockTransport(handler)
        if handler
        else httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    async_client = httpx.AsyncClient(transport=transport)
    sub = ProtectRingSubscriber(section, lambda _r: asyncio.sleep(0), client=async_client)
    sub.cameras["cam-1"] = ProtectCameraStatus(
        protect_id="cam-1",
        frigate_camera="front_door",
        name="Front Door",
        model="doorbell",
        state="CONNECTED",
        has_lcd=has_lcd,
        checked_at=time.time(),
    )
    client.app.state.protect_subscriber = sub  # type: ignore[attr-defined]
    return client, sub


# -- options route ------------------------------------------------------------


def test_options_route_with_lcd(_base_settings_kwargs: dict) -> None:
    client, sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    sub.animations = [
        {"id": "image:cat.png", "title": "Cat", "name": "cat.png"},
    ]
    r = client.get("/v1/doorbell/front_door/lcd/options")
    assert r.status_code == 200
    body = r.json()
    assert body["camera"] == "front_door"
    assert body["has_lcd"] is True
    ids = [o["id"] for o in body["options"]]
    assert ids[:3] == ["leave_package", "be_right_there", "do_not_disturb"]
    assert ids[-1] == "image:cat.png"
    image_opt = body["options"][-1]
    assert image_opt["kind"] == "image"
    assert image_opt["title"] == "Cat"
    assert image_opt["type"] == "IMAGE"


def test_options_route_without_lcd(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=False)
    r = client.get("/v1/doorbell/front_door/lcd/options")
    assert r.status_code == 409


def test_options_route_unknown_camera(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.get("/v1/doorbell/nope/lcd/options")
    assert r.status_code == 404


def test_options_route_disabled(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    r = client.get("/v1/doorbell/front_door/lcd/options")
    assert r.status_code == 404


# -- action route ---------------------------------------------------------


def test_action_route_preset_leave_package(_base_settings_kwargs: dict) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "cam-1"})

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    r = client.post("/v1/doorbell/front_door/lcd", json={"option_id": "leave_package"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["applied"]["type"] == "LEAVE_PACKAGE_AT_DOOR"
    assert body["applied"]["text"] is None
    assert b"LEAVE_PACKAGE_AT_DOOR" in seen["body"]


def test_action_route_preset_custom_message(_base_settings_kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cam-1"})

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    r = client.post("/v1/doorbell/front_door/lcd", json={"option_id": "be_right_there"})
    assert r.status_code == 200
    assert r.json()["applied"]["text"] == "BE RIGHT THERE"


def test_action_route_image_option(_base_settings_kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cam-1"})

    client, sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    sub.animations = [{"id": "image:cat.png", "title": "Cat", "name": "cat.png"}]
    r = client.post("/v1/doorbell/front_door/lcd", json={"option_id": "image:cat.png"})
    assert r.status_code == 200
    body = r.json()
    assert body["applied"]["type"] == "IMAGE"
    assert body["applied"]["text"] == "cat.png"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("  be   right   there  ", "BE RIGHT THERE"),
        ("hello!", "HELLO!"),
    ],
)
def test_action_route_custom_text_normalization(
    _base_settings_kwargs: dict, text: str, expected: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cam-1"})

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    r = client.post("/v1/doorbell/front_door/lcd", json={"custom_text": text})
    assert r.status_code == 200
    assert r.json()["applied"]["text"] == expected


def test_action_route_custom_text_empty_rejected(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.post("/v1/doorbell/front_door/lcd", json={"custom_text": "   "})
    assert r.status_code == 400


def test_action_route_custom_text_too_long_rejected(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.post("/v1/doorbell/front_door/lcd", json={"custom_text": "x" * 40})
    assert r.status_code == 400


def test_action_route_custom_text_bad_chars_rejected(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.post("/v1/doorbell/front_door/lcd", json={"custom_text": "hi @home #1"})
    assert r.status_code == 400


def test_action_route_both_fields_rejected(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.post(
        "/v1/doorbell/front_door/lcd",
        json={"option_id": "leave_package", "custom_text": "hi"},
    )
    assert r.status_code == 400


def test_action_route_neither_field_rejected(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True)
    r = client.post("/v1/doorbell/front_door/lcd", json={})
    assert r.status_code == 400


def test_action_route_no_lcd_409(_base_settings_kwargs: dict) -> None:
    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=False)
    r = client.post("/v1/doorbell/front_door/lcd", json={"option_id": "leave_package"})
    assert r.status_code == 409


def test_action_route_console_failure_502(_base_settings_kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    r = client.post("/v1/doorbell/front_door/lcd", json={"option_id": "leave_package"})
    assert r.status_code == 502
    assert r.json()["detail"]["ok"] is False


def test_action_route_writes_doorbell_action_row(
    _base_settings_kwargs: dict, sidecar_db_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cam-1"})

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    client.post("/v1/doorbell/front_door/lcd", json={"option_id": "leave_package"})

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client2, _sub2 = _client_with_subscriber(
        _base_settings_kwargs, has_lcd=True, handler=bad_handler
    )
    client2.post("/v1/doorbell/front_door/lcd", json={"option_id": "leave_package"})

    conn = db.open_sidecar(str(sidecar_db_path))
    rows = conn.execute("SELECT ok FROM doorbell_actions ORDER BY id").fetchall()
    conn.close()
    assert [bool(r["ok"]) for r in rows] == [True, False]


# -- retry / spacing behavior -------------------------------------------------


@pytest.mark.asyncio
async def test_set_lcd_message_retries_once_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    from marcellus.push import unifi_protect as up_mod

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"id": "cam-1"})

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(up_mod.asyncio, "sleep", _fake_sleep)

    section = UnifiProtectSection(
        enabled=True,
        console_url="https://console.test",
        api_key="k",
        cameras={"cam-1": "front_door"},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(section, lambda _r: asyncio.sleep(0), client=client)
    result = await sub.set_lcd_message("cam-1", {"type": "DO_NOT_DISTURB", "resetAt": 123})
    assert result == {"id": "cam-1"}
    assert calls["n"] == 2
    assert 2.0 in sleeps
    await client.aclose()


@pytest.mark.asyncio
async def test_set_lcd_message_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rapid calls end up spaced >=1s apart -- assert via the sleep call,
    not a real wall-clock wait."""
    from marcellus.push import unifi_protect as up_mod

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "cam-1"})

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(up_mod.asyncio, "sleep", _fake_sleep)

    section = UnifiProtectSection(
        enabled=True,
        console_url="https://console.test",
        api_key="k",
        cameras={"cam-1": "front_door"},
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(section, lambda _r: asyncio.sleep(0), client=client)
    await sub.set_lcd_message("cam-1", {"type": "DO_NOT_DISTURB", "resetAt": 1})
    await sub.set_lcd_message("cam-1", {"type": "DO_NOT_DISTURB", "resetAt": 2})
    # The second call must have waited out the remainder of the 1s spacing
    # window (mocked, so this asserts the *code path*, not real elapsed time).
    assert any(s > 0 for s in sleeps)
    await client.aclose()


# -- slots round-trip on /v1/push/devices -------------------------------------


def test_doorbell_slots_round_trip(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    token = "tok-slots-1"
    r = client.put(
        f"/v1/push/devices/{token}",
        json={
            "bundle_id": "com.x",
            "environment": "sandbox",
            "doorbell_slots": ["a", "b", "c"],
        },
    )
    assert r.status_code == 200
    r2 = client.get(f"/v1/push/devices/{token}")
    assert r2.json()["doorbell_slots"] == ["a", "b", "c"]


def test_doorbell_slots_wrong_length_rejected(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    for bad in ([], ["a"], ["a", "b"], ["a", "b", "c", "d"]):
        r = client.put(
            "/v1/push/devices/tok-bad",
            json={"bundle_id": "com.x", "environment": "sandbox", "doorbell_slots": bad},
        )
        assert r.status_code == 400, bad


def test_doorbell_slots_null_clears_to_default(_base_settings_kwargs: dict) -> None:
    settings = Settings(**_base_settings_kwargs)
    client = TestClient(create_app(settings))
    token = "tok-slots-2"
    client.put(
        f"/v1/push/devices/{token}",
        json={"bundle_id": "com.x", "environment": "sandbox", "doorbell_slots": ["a", "b", "c"]},
    )
    r = client.put(
        f"/v1/push/devices/{token}",
        json={"bundle_id": "com.x", "environment": "sandbox", "doorbell_slots": None},
    )
    assert r.status_code == 200
    r2 = client.get(f"/v1/push/devices/{token}")
    assert r2.json()["doorbell_slots"] is None


# -- payload slot resolution --------------------------------------------------


def test_resolve_lcd_slots_drops_unknown_keeps_original_position() -> None:
    presets = {"a": LcdPreset(type="DO_NOT_DISTURB", duration_s=60, title="A")}
    animations = [{"id": "image:x.png", "title": "X", "name": "x.png"}]
    resolved = doorbell.resolve_lcd_slots(
        ("a", "unknown", "image:x.png"), presets=presets, animations=animations
    )
    # "unknown" is slot 2 and drops out -- the third configured id must keep
    # its original position (slot: 3), not be renumbered down to slot 2.
    assert [r["slot"] for r in resolved] == [1, 3]
    assert [r["id"] for r in resolved] == ["a", "image:x.png"]


def test_resolve_lcd_slots_gap_in_middle_preserved() -> None:
    presets = {
        "valid1": LcdPreset(type="DO_NOT_DISTURB", duration_s=60, title="Valid 1"),
        "valid3": LcdPreset(type="DO_NOT_DISTURB", duration_s=60, title="Valid 3"),
    }
    resolved = doorbell.resolve_lcd_slots(
        ("valid1", "unknown-id", "valid3"), presets=presets, animations=[]
    )
    assert [(r["slot"], r["id"]) for r in resolved] == [
        (1, "valid1"),
        (3, "valid3"),
    ]


def test_build_ring_payload_without_lcd_omits_keys() -> None:
    payload = doorbell.build_ring_payload(
        frigate_camera="front_door",
        media=None,
        protect_event_id="evt-1",
        has_lcd=False,
    )
    assert "lcd_slots" not in payload["doorbell"]
    assert "custom_reply" not in payload["doorbell"]


def test_build_ring_payload_with_lcd_includes_keys() -> None:
    payload = doorbell.build_ring_payload(
        frigate_camera="front_door",
        media=None,
        protect_event_id="evt-1",
        has_lcd=True,
        lcd_slots=[{"slot": 1, "id": "leave_package", "title": "Leave package"}],
        custom_reply_max_chars=30,
        custom_reply_duration_s=120,
    )
    assert payload["doorbell"]["lcd_slots"] == [
        {"slot": 1, "id": "leave_package", "title": "Leave package"}
    ]
    assert payload["doorbell"]["custom_reply"] == {"max_chars": 30, "duration_s": 120}
    assert payload["aps"]["category"] == "doorbell.ring"


def test_handle_ring_populates_lcd_slots_end_to_end(tmp_path: Path) -> None:
    conn = db.open_sidecar(str(tmp_path / "sidecar.db"))
    store.upsert_device(
        conn, apns_token="tok-1", bundle_id="com.x", environment="sandbox", cameras=[], labels=[]
    )
    conn.commit()
    doorbell.reset_ring_dedup_for_tests()
    transport = LogTransport()
    presets = {
        "leave_package": LcdPreset(
            type="LEAVE_PACKAGE_AT_DOOR", duration_s=1800, title="Leave package"
        ),
    }
    outcome = asyncio.run(
        doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
            has_lcd=True,
            lcd_presets=presets,
            animations=[],
        )
    )
    assert outcome.sent == 1
    sent_payload = transport.sent[0]["payload"]
    assert sent_payload["doorbell"]["lcd_slots"][0]["id"] == "leave_package"
    assert "custom_reply" in sent_payload["doorbell"]


def test_handle_ring_snapshot_prewarm_bounded_on_dead_console(tmp_path: Path) -> None:
    """Fix 2 (review blocker): a Protect fetcher that hangs must not delay
    the ring send past `snapshot_prewarm_timeout_s` -- the send completes
    with the handle minted but no prewarmed bytes, not an exception."""
    conn = db.open_sidecar(str(tmp_path / "sidecar.db"))
    store.upsert_device(
        conn, apns_token="tok-1", bundle_id="com.x", environment="sandbox", cameras=[], labels=[]
    )
    conn.commit()
    doorbell.reset_ring_dedup_for_tests()
    transport = LogTransport()

    async def _hanging_fetcher(_protect_camera_id: str) -> bytes | None:
        await asyncio.sleep(10.0)
        return b"never"

    async def _run() -> doorbell.RingOutcome:
        return await doorbell.handle_ring(
            protect_camera_id="cam-1",
            protect_event_id="evt-1",
            cameras={"cam-1": "front_door"},
            conn=conn,
            transport=transport,
            frigate_base_url="http://frigate.test:5000",
            external_base_url="https://push.test",
            situation_handle_ttl_s=3600.0,
            ring_dedup_seconds=20.0,
            ring_snapshot="protect",
            protect_snapshot_fetcher=_hanging_fetcher,
            snapshot_prewarm_timeout_s=0.05,
            frigate_fallback_timeout_s=0.05,
        )

    start = time.monotonic()
    outcome = asyncio.run(asyncio.wait_for(_run(), timeout=2.0))
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
    assert outcome.sent == 1


# -- snapshot proxy ------------------------------------------------------


def test_snapshot_proxy_console_success(_base_settings_kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8\xff\xd9fakejpeg")

    client, _sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)
    r = client.get("/v1/doorbell/front_door/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content == b"\xff\xd8\xff\xd9fakejpeg"


def test_snapshot_proxy_falls_back_to_frigate(
    _base_settings_kwargs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client, sub = _client_with_subscriber(_base_settings_kwargs, has_lcd=True, handler=handler)

    async def _fake_fetch_thumbnail(*args, **kwargs) -> bytes:
        return b"frigate-bytes"

    monkeypatch.setattr("marcellus.routes.protect.fetch_thumbnail", _fake_fetch_thumbnail)
    r = client.get("/v1/doorbell/front_door/snapshot")
    assert r.status_code == 200
    assert r.content == b"frigate-bytes"
