"""M-1: device-poll parsing, retry-once, and `/healthz`'s `unifi_protect`
state machine (`routes/health.py._protect_health_state`).

`test_push_unifi_protect.py` covers the pure ring-event parser; this file
covers the newer device-health poll and the health-check logic that reads
its output.
"""

from __future__ import annotations

import time

import httpx
import pytest

from marcellus.config import UnifiProtectSection
from marcellus.push.unifi_protect import ProtectCameraStatus, ProtectRingSubscriber
from marcellus.routes.health import _protect_health_state


def _settings(**overrides: object) -> UnifiProtectSection:
    base: dict[str, object] = {
        "enabled": True,
        "console_url": "https://console.test",
        "api_key": "key123",
        "cameras": {"cam-1": "front_door", "cam-2": "side_yard"},
    }
    base.update(overrides)
    return UnifiProtectSection(**base)  # type: ignore[arg-type]


async def _noop_on_ring(_: object) -> None:
    return None


@pytest.mark.asyncio
async def test_poll_devices_once_parses_mapped_cameras_and_null_lcd() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-KEY"] == "key123"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "cam-1",
                    "name": "Front Door",
                    "modelKey": "camera",
                    "state": "CONNECTED",
                    "lcdMessage": {"text": "hi"},
                },
                {
                    # Dahua-style camera: reports lcdMessage as null.
                    "id": "cam-2",
                    "name": "Side Yard",
                    "modelKey": "camera",
                    "state": "DISCONNECTED",
                    "lcdMessage": None,
                },
                {
                    # Not in unifi_protect.cameras -- must be skipped.
                    "id": "cam-unmapped",
                    "name": "Garage",
                    "state": "CONNECTED",
                },
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(_settings(), _noop_on_ring, client=client)
    await sub.poll_devices_once()

    assert sub.last_poll_error is None
    assert sub.last_poll_at is not None
    assert set(sub.cameras) == {"cam-1", "cam-2"}
    front = sub.cameras["cam-1"]
    assert isinstance(front, ProtectCameraStatus)
    assert front.has_lcd is True
    assert front.state == "CONNECTED"
    side = sub.cameras["cam-2"]
    assert side.has_lcd is False
    assert side.state == "DISCONNECTED"

    status = sub.status()
    assert status["mapped_cameras_connected"] is False  # cam-2 is DISCONNECTED
    assert len(status["cameras"]) == 2

    await client.aclose()


@pytest.mark.asyncio
async def test_poll_devices_once_retries_once_on_429_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json=[{"id": "cam-1", "name": "Front Door", "state": "CONNECTED"}],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(_settings(), _noop_on_ring, client=client)

    async def _no_sleep(_: float) -> None:
        return None

    import asyncio

    orig_sleep = asyncio.sleep
    asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await sub.poll_devices_once()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]

    assert calls["n"] == 2
    assert sub.last_poll_error is None
    assert "cam-1" in sub.cameras
    await client.aclose()


@pytest.mark.asyncio
async def test_poll_devices_once_records_error_after_retry_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(_settings(), _noop_on_ring, client=client)

    async def _no_sleep(_: float) -> None:
        return None

    import asyncio

    orig_sleep = asyncio.sleep
    asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await sub.poll_devices_once()
    finally:
        asyncio.sleep = orig_sleep  # type: ignore[assignment]

    assert sub.last_poll_error is not None
    assert sub.cameras == {}
    await client.aclose()


class _FakeSubscriber:
    def __init__(self, *, disconnected_since: float | None, **status_overrides: object) -> None:
        self.disconnected_since = disconnected_since
        self._status = {
            "connected": True,
            "last_poll_at": time.time(),
            "last_poll_error": None,
            "mapped_cameras_connected": True,
            "last_ring_at": None,
        }
        self._status.update(status_overrides)

    def status(self) -> dict[str, object]:
        return self._status


def test_protect_health_ok() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=None)
    assert _protect_health_state(sub, now) == "ok"


def test_protect_health_down_after_120s_disconnected() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=now - 200, connected=False)
    assert _protect_health_state(sub, now) == "down"


def test_protect_health_degraded_when_recently_disconnected() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=now - 10, connected=False)
    assert _protect_health_state(sub, now) == "degraded"


def test_protect_health_degraded_when_camera_not_connected() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=None, mapped_cameras_connected=False)
    assert _protect_health_state(sub, now) == "degraded"


def test_protect_health_degraded_when_never_polled() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=None, last_poll_at=None)
    assert _protect_health_state(sub, now) == "degraded"


def test_protect_health_degraded_when_poll_error() -> None:
    now = time.time()
    sub = _FakeSubscriber(disconnected_since=None, last_poll_error="boom")
    assert _protect_health_state(sub, now) == "degraded"
