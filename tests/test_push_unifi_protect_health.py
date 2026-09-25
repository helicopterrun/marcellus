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
from marcellus.push import unifi_protect
from marcellus.push.unifi_protect import ProtectCameraStatus, ProtectRingSubscriber
from marcellus.routes.health import _protect_health_state


async def _no_sleep(_: float) -> None:
    return None


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
async def test_poll_devices_once_retries_once_on_429_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(unifi_protect.asyncio, "sleep", _no_sleep)
    await sub.poll_devices_once()

    assert calls["n"] == 2
    assert sub.last_poll_error is None
    assert "cam-1" in sub.cameras
    await client.aclose()


@pytest.mark.asyncio
async def test_poll_devices_once_records_error_after_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sub = ProtectRingSubscriber(_settings(), _noop_on_ring, client=client)

    monkeypatch.setattr(unifi_protect.asyncio, "sleep", _no_sleep)
    await sub.poll_devices_once()

    assert sub.last_poll_error is not None
    assert sub.cameras == {}
    await client.aclose()


@pytest.mark.asyncio
async def test_device_poll_loop_survives_non_httpx_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed camera dict (or anything else non-httpx) raising out of
    `poll_devices_once`/`_fetch_meta_info` must not kill the loop task --
    it should be logged and the loop should keep running on schedule."""
    monkeypatch.setattr(unifi_protect.asyncio, "sleep", _no_sleep)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    sub = ProtectRingSubscriber(_settings(device_poll_seconds=15), _noop_on_ring, client=client)

    calls = {"n": 0}
    orig_poll = sub.poll_devices_once

    async def _flaky_poll() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("boom: malformed camera dict")
        await orig_poll()
        sub._stopped = True  # stop after the second (successful) iteration

    async def _noop_meta_info() -> None:
        return None

    monkeypatch.setattr(sub, "poll_devices_once", _flaky_poll)
    monkeypatch.setattr(sub, "_fetch_meta_info", _noop_meta_info)

    with caplog.at_level("ERROR", logger="marcellus.push.unifi_protect"):
        await sub.device_poll_loop()

    # The loop survived the TypeError on iteration 1 (logged via
    # `logger.exception`) and completed iteration 2, which succeeded and
    # cleared `last_poll_error` back to `None` -- proof the task kept
    # running rather than dying silently.
    assert calls["n"] == 2
    assert sub.last_poll_error is None
    assert any("device poll loop error" in r.message for r in caplog.records)
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
