"""`push.transport: auto` -- relay once something can authenticate, else mock."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from marcellus.config import PushSection, Settings
from marcellus.push.models import Device
from marcellus.push.transport import (
    AUTO_IDLE_NOTE,
    AutoTransport,
    LogTransport,
    RelayTransport,
    effective_transport,
    reset_relay_health_for_tests,
)
from marcellus.server import _build_push_transport


def _device(**kw: Any) -> Device:
    base: dict[str, Any] = dict(
        apns_token="tok1", device_id="d_abc", bundle_id="com.x", environment="sandbox"
    )
    base.update(kw)
    return Device(**base)


def _auto(relay_key: str = "") -> tuple[AutoTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    relay = RelayTransport(
        "https://relay.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        relay_key=relay_key,
    )
    return AutoTransport(relay, LogTransport(), relay_key=relay_key), seen


async def _send(t: Any, device: Device) -> Any:
    return await t.send(device, handle="h", server_id="s", severity="alert", collapse_id="c")


def test_default_transport_is_auto() -> None:
    assert PushSection().transport == "auto"
    assert PushSection(transport="AUTO").transport == "auto"
    assert PushSection(transport="relay").transport == "relay"
    with pytest.raises(ValueError):
        PushSection(transport="smoke")


def test_builder_picks_the_wrapper_only_for_auto() -> None:
    assert isinstance(_build_push_transport(Settings()), AutoTransport)
    assert isinstance(
        _build_push_transport(Settings(push=PushSection(transport="mock"))), LogTransport
    )
    assert isinstance(
        _build_push_transport(Settings(push=PushSection(transport="relay"))), RelayTransport
    )


async def test_no_credential_no_key_sends_nothing() -> None:
    reset_relay_health_for_tests()
    t, seen = _auto()
    result = await _send(t, _device())
    assert result.ok is True
    assert seen == []
    assert len(t.sent) == 1


async def test_credential_registered_later_goes_to_relay_with_bearer() -> None:
    t, seen = _auto()
    await _send(t, _device())
    assert seen == []
    # Same transport instance, same device, now with a credential: no restart.
    await _send(t, _device(relay_credential="v1.a.b"))
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == "Bearer v1.a.b"


async def test_relay_key_alone_sends_to_relay() -> None:
    t, seen = _auto(relay_key="k")
    await _send(t, _device())
    assert len(seen) == 1
    assert seen[0].headers["x-relay-key"] == "k"


async def test_explicit_relay_still_skips_a_device_with_no_auth() -> None:
    seen: list[httpx.Request] = []
    relay = RelayTransport(
        "https://relay.test",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1])
        ),
    )
    result = await _send(relay, _device())
    assert seen == []
    assert result.ok is False


def test_effective_transport_and_status_text() -> None:
    assert effective_transport("mock", "", [_device(relay_credential="v1.a.b")]) == ("mock", None)
    assert effective_transport("relay", "", []) == ("relay", None)
    mode, note = effective_transport("auto", "", [_device()])
    assert mode == "mock" and note == AUTO_IDLE_NOTE
    assert "no device has supplied a relay credential yet" in AUTO_IDLE_NOTE
    assert effective_transport("auto", "", [_device(relay_credential="v1.a.b")]) == ("relay", None)
    assert effective_transport("auto", "k", []) == ("relay", None)
