from __future__ import annotations

import httpx

from marcellus.push.models import Device
from marcellus.push.transport import LogTransport, RelayTransport


def _device(**kwargs):
    defaults = dict(
        apns_token="tok1", device_id="d_abc", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox",
    )
    defaults.update(kwargs)
    return Device(**defaults)


async def test_log_transport_records_send_and_succeeds():
    transport = LogTransport()
    result = await transport.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is True
    assert result.unregistered is False
    assert len(transport.sent) == 1
    record = transport.sent[0]
    assert record["handle"] == "h_1"
    assert record["server_id"] == "s1"
    assert record["severity"] == "alert"
    # No content-bearing field ever reaches the transport.
    assert "camera" not in record
    assert "label" not in record


async def test_relay_transport_situation_matches_the_relays_wire_contract():
    """Exactly the four keys `validateSituation` requires in
    elsinore-push-relay 4278bdf -- no `bundle_id`, no `headers` block. The
    relay sets apns-topic/push-type/priority itself; it contributes routing
    and the collapse id and nothing else."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"detail": "ok"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    aps = {"aps": {"alert": {"title": "At the door", "body": "Person, 6s"}},
           "situation_id": "at-the-door", "handle": "h_1"}
    result = await relay.send_situation(
        _device(apns_token="tokXYZ", environment="prod"),
        payload=aps, collapse_id="at-the-door:t1",
    )
    assert result.ok is True
    assert captured["url"] == "https://relay.example.test/v1/relay/situation"
    body = captured["json"]
    assert set(body.keys()) == {
        "device_token", "environment", "apns-collapse-id", "payload",
    }
    # `prod` is this codebase's spelling; the relay's wire API wants
    # `production` and 422s anything else.
    assert body["environment"] == "production"
    assert body["apns-collapse-id"] == "at-the-door:t1"
    assert body["payload"] == aps  # forwarded verbatim


async def test_relay_transport_situation_surfaces_a_dead_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"detail": "Unregistered"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send_situation(
        _device(), payload={"aps": {}}, collapse_id="s:t"
    )
    assert result.ok is False and result.unregistered is True


async def test_relay_transport_situation_reports_a_rejected_payload():
    """The relay 422s a bad device token with a readable reason; that reason
    has to reach the logs, not be swallowed. This 422 case is token-specific
    (the relay's own hex/length validation), so it is a permanent,
    never-retried rejection -- same as 410/400 -- and the caller prunes the
    device rather than retrying it on every future send."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "device_token must be hex"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send_situation(
        _device(), payload={"aps": {}}, collapse_id="s:t"
    )
    assert result.ok is False and result.unregistered is True
    # `error` is now the same terse "HTTP <code>" shape as the 410/400 path
    # (same category, same handling) -- the full relay-provided reason still
    # reaches the logs via the `logger.warning` call above.
    assert result.error == "HTTP 422"


async def test_relay_transport_situation_non_token_422_not_pruned():
    """A 422 that names a payload/code bug -- not the device token -- must
    NOT prune the device. Pruning here would delete every device on a single
    sidecar bug (e.g. a bad `environment` value or an over-long field), which
    is exactly the over-prune regression Fix A introduced and this test
    locks out."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "handle too long"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send_situation(
        _device(), payload={"aps": {}}, collapse_id="s:t"
    )
    assert result.ok is False
    assert result.unregistered is False


async def test_relay_transport_posts_minimal_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(apns_token="tokXYZ"),
        handle="h_9k2m4p7q", server_id="a1b2c3", severity="alert", collapse_id="review-1",
    )
    assert result.ok is True
    assert captured["url"] == "https://relay.example.test/v1/relay/push"
    payload = captured["json"]
    assert set(payload.keys()) == {
        "device_token", "environment", "handle", "server_id", "severity", "apns-collapse-id",
    }
    assert payload["device_token"] == "tokXYZ"
    assert payload["environment"] == "sandbox"
    assert payload["handle"] == "h_9k2m4p7q"
    assert payload["server_id"] == "a1b2c3"
    assert payload["severity"] == "alert"
    assert payload["apns-collapse-id"] == "review-1"
    await relay.aclose()


async def test_relay_transport_410_marks_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"reason": "Unregistered"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is True
    await relay.aclose()


async def test_relay_transport_400_bad_token_marks_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"reason": "BadDeviceToken"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.unregistered is True


async def test_relay_transport_other_error_not_unregistered():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is False


async def test_relay_transport_connection_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send(
        _device(), handle="h_1", server_id="s1", severity="alert", collapse_id="r1"
    )
    assert result.ok is False
    assert result.unregistered is False


async def test_log_transport_records_a_test_send() -> None:
    transport = LogTransport()
    result = await transport.send_test(_device(environment="prod"))
    assert result.ok is True
    record = transport.sent[-1]
    assert record["test"] is True
    assert record["environment"] == "prod", "environment routing is never bypassed"
    # A test push carries no handle: there is nothing for the NSE to redeem.
    assert "handle" not in record


async def test_relay_transport_test_send_posts_only_token_and_environment() -> None:
    """The test payload is a fixed literal alert with no `handle` and no
    `mutable-content`, so it cannot go through /v1/relay/push -- that route
    validates `handle` as required."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = __import__("json").loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send_test(_device(apns_token="tokXYZ", environment="prod"))

    assert result.ok is True
    assert captured["url"] == "https://relay.example.test/v1/relay/test"
    # `prod` is translated to the relay's `production` spelling on the way out.
    assert captured["json"] == {"device_token": "tokXYZ", "environment": "production"}


async def test_relay_transport_test_send_410_marks_unregistered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, text="Unregistered")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    result = await relay.send_test(_device())
    assert result.ok is False
    assert result.unregistered is True, "a dead token is dead however it was discovered"


async def test_relay_transport_translates_prod_to_production() -> None:
    """The sidecar's API, DB constraint and spec §1 all say `prod`; the relay's
    wire API says `production` and 422s anything else. Without translating at
    this boundary, no production device could ever be notified."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(__import__("json").loads(request.content)["environment"])
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    relay = RelayTransport("https://relay.example.test", client=client)
    await relay.send(
        _device(environment="prod"),
        handle="h_1", server_id="s1", severity="alert", collapse_id="r1",
    )
    await relay.send_test(_device(environment="prod"))
    await relay.send_test(_device(environment="sandbox"))

    assert seen == ["production", "production", "sandbox"], (
        "both routes must translate, and sandbox must pass through untouched"
    )
