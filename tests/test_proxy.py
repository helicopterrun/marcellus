"""Tests for the transparent Frigate reverse proxy (routes/proxy.py).

Covers docs/scrub-cache-and-proxy-spec.md §6: Range/Authorization forwarding,
206/401/404 mirroring, traversal rejection, set-cookie/etag relay.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app


class _StubResponse:
    def __init__(self, status_code: int, headers: Any, body: bytes) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._body = body

    async def aiter_raw(self) -> Any:
        yield self._body

    async def aclose(self) -> None:
        return None


class _StubAsyncClient:
    """Stand-in for httpx.AsyncClient that records the outgoing request and
    returns a canned response, so tests don't hit the network."""

    last_request: dict[str, Any] = {}
    next_response: _StubResponse | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        req = {"method": method, "url": url, **kwargs}
        _StubAsyncClient.last_request = req
        return req

    async def send(self, req: dict[str, Any], stream: bool = False) -> _StubResponse:
        assert _StubAsyncClient.next_response is not None
        return _StubAsyncClient.next_response

    async def aclose(self) -> None:
        return None


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url="http://frigate.test:8971",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=True),
    )
    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _stub_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)


def test_forwards_range_and_authorization_and_mirrors_206(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        206,
        {"content-type": "video/mp4", "content-range": "bytes 0-99/200", "accept-ranges": "bytes"},
        b"chunk",
    )
    r = client.get(
        "/vod/doorbell/index.m3u8",
        headers={"Range": "bytes=0-99", "Authorization": "Bearer abc"},
    )
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-99/200"
    fwd = {k.lower(): v for k, v in _StubAsyncClient.last_request["headers"].items()}
    assert fwd["range"] == "bytes=0-99"
    assert fwd["authorization"] == "Bearer abc"


def test_mirrors_401_and_relays_www_authenticate(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        401, {"www-authenticate": 'Basic realm="frigate"'}, b""
    )
    r = client.get("/api/config")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="frigate"'


def test_mirrors_404(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(404, {}, b"")
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404


def test_relays_set_cookie_and_etag(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        200, {"set-cookie": "session=xyz; Path=/", "etag": '"abc123"'}, b"{}"
    )
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.headers["set-cookie"] == "session=xyz; Path=/"
    assert r.headers["etag"] == '"abc123"'


def test_forwards_cookie_header(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "application/json"}, b"{}")
    r = client.get("/api/config", headers={"Cookie": "session=abc"})
    assert r.status_code == 200
    fwd = {k.lower(): v for k, v in _StubAsyncClient.last_request["headers"].items()}
    assert fwd["cookie"] == "session=abc"


def test_forwards_method_pass_through_for_post(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "application/json"}, b"{}")
    r = client.post("/api/reviews/viewed", json={"ids": ["e1"]})
    assert r.status_code == 200
    assert _StubAsyncClient.last_request["method"] == "POST"


def test_traversal_rejected(client: TestClient) -> None:
    _StubAsyncClient.next_response = None  # if the guard is bypassed, .send() will AssertionError
    # %2e%2e survives client-side URL normalization (unlike a literal "..",
    # which httpx collapses before the request ever leaves the test client),
    # so this actually exercises the proxy's own traversal guard.
    r = client.get("/api/%2e%2e/%2e%2e/etc/passwd", follow_redirects=False)
    assert r.status_code == 400


def test_proxy_disabled_returns_404(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=fake_config, db_path=frigate_db_path),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=False),
    )
    c = TestClient(create_app(settings))
    r = c.get("/api/config")
    assert r.status_code == 404


def test_gzipped_body_is_relayed_raw_with_matching_length(client: TestClient) -> None:
    """The body must stay in the encoding its `content-length` describes.

    httpx decodes `content-encoding` when you iterate the decoded stream, so
    forwarding the upstream length beside a decoded body described the wrong
    number of bytes for every gzipped Frigate response (i.e. all of /api/*).
    """
    import gzip

    payload = b'{"cameras": ["doorbell", "alley-overview"]}'
    body = gzip.compress(payload)
    _StubAsyncClient.next_response = _StubResponse(
        200,
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": str(len(body)),
        },
        body,
    )
    r = client.get("/api/config", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(body))
    assert r.json() == {"cameras": ["doorbell", "alley-overview"]}
    # The client's own negotiation travels upstream with the raw relay.
    fwd = {k.lower(): v for k, v in _StubAsyncClient.last_request["headers"].items()}
    assert fwd["accept-encoding"] == "gzip"


def test_multiple_set_cookie_headers_are_relayed_separately(client: TestClient) -> None:
    """Frigate's login sets more than one cookie; reading them off the header
    mapping comma-joined them into a single malformed value."""
    _StubAsyncClient.next_response = _StubResponse(
        200,
        [
            ("content-type", "application/json"),
            ("set-cookie", "frigate_token=abc; Path=/; HttpOnly"),
            ("set-cookie", "frigate_refresh=def; Path=/; HttpOnly"),
        ],
        b"{}",
    )
    r = client.get("/api/login")
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert cookies[0].startswith("frigate_token=abc")
    assert cookies[1].startswith("frigate_refresh=def")


def test_location_header_is_relayed_on_redirect(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(302, {"location": "/login"}, b"")
    r = client.get("/api/whatever", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_options_is_proxied(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(204, {"allow": "GET, POST"}, b"")
    r = client.options("/api/config")
    assert r.status_code == 204
    assert _StubAsyncClient.last_request["method"] == "OPTIONS"


def test_build_request_gets_the_media_stream_timeout(client: TestClient) -> None:
    """The one request through the shared client that legitimately needs an
    unbounded read (see frigate_api._DEFAULT_TIMEOUT's now-finite default)."""
    from marcellus.routes import proxy as proxy_module

    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "video/mp4"}, b"x")
    r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 200
    assert _StubAsyncClient.last_request["timeout"] is proxy_module._UPSTREAM_TIMEOUT


class _HangingResponse:
    """A response whose body stalls forever after its first chunk -- what the
    idle-chunk watchdog in `proxy.stream_body` is meant to catch."""

    status_code = 200
    headers = httpx.Headers({"content-type": "video/mp4"})

    def __init__(self) -> None:
        self.aclosed = False

    async def aiter_raw(self) -> Any:
        yield b"first-chunk"
        await asyncio.Event().wait()  # never set
        yield b"unreachable"  # pragma: no cover

    async def aclose(self) -> None:
        self.aclosed = True


def test_idle_chunk_watchdog_ends_a_stalled_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcellus.routes import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_IDLE_CHUNK_TIMEOUT_S", 0.05)
    hanging = _HangingResponse()
    _StubAsyncClient.next_response = hanging  # type: ignore[assignment]

    r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 200
    # The stream is cut short at the idle timeout rather than hanging or
    # raising -- the client sees the bytes that did arrive, and nothing more.
    assert r.content == b"first-chunk"
    assert hanging.aclosed is True


def test_client_stall_ends_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that stopped reading (a paused player) must not hold the
    upstream connection open forever -- see the 2026-09-10 pool-exhaustion
    incident in routes/proxy.py. Exercises `_BoundedStreamingResponse`
    directly: `send()` never returns, simulating a stalled client."""
    from marcellus.routes import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_CLIENT_STALL_TIMEOUT_S", 0.05)
    closed = {"value": False}

    async def body() -> Any:
        try:
            yield b"a"
            yield b"b"
        finally:
            closed["value"] = True

    async def never_returning_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            await asyncio.Event().wait()  # never set -- simulates a stalled client

    response = proxy_module._BoundedStreamingResponse(
        body(), status_code=200, headers={}, log_path="vod/doorbell/index.m3u8"
    )

    async def run() -> None:
        await response.stream_response(never_returning_send)

    asyncio.run(asyncio.wait_for(run(), timeout=2.0))
    assert closed["value"] is True


def test_max_duration_ends_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from marcellus.routes import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_STREAM_MAX_DURATION_S", 0.0)
    closed = {"value": False}

    async def body() -> Any:
        try:
            yield b"a"
            yield b"b"
        finally:
            closed["value"] = True

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    response = proxy_module._BoundedStreamingResponse(
        body(), status_code=200, headers={}, log_path="vod/doorbell/index.m3u8"
    )
    asyncio.run(response.stream_response(send))
    assert closed["value"] is True
    # The deadline is already past before the first chunk, so no body bytes
    # made it out -- only the response start and the final close frame.
    assert all(m.get("body", b"") == b"" for m in sent if m["type"] == "http.response.body")


def test_pool_timeout_maps_to_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _raise_pool_timeout(self: Any, req: Any, stream: bool = False) -> Any:
        raise httpx.PoolTimeout("pool exhausted")

    monkeypatch.setattr(_StubAsyncClient, "send", _raise_pool_timeout)
    r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "upstream_busy"


def test_header_wait_timeout_maps_to_504(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `client.send` that never returns (Frigate's nginx half-closing a
    keepalive socket before sending headers) must 504 rather than hang the
    whole route, and must not leak a response (none was ever created)."""
    from marcellus.routes import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_HEADER_WAIT_TIMEOUT_S", 0.05)

    async def _never_returning_send(self: Any, req: Any, stream: bool = False) -> Any:
        await asyncio.Event().wait()  # never set

    monkeypatch.setattr(_StubAsyncClient, "send", _never_returning_send)
    r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 504
    assert r.json()["detail"]["error"] == "upstream_timeout"


def test_slow_acquire_is_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from marcellus.routes import proxy as proxy_module

    monkeypatch.setattr(proxy_module, "_SLOW_ACQUIRE_LOG_THRESHOLD_S", 0.0)

    async def _slow_send(self: Any, req: Any, stream: bool = False) -> Any:
        await asyncio.sleep(0.02)
        assert _StubAsyncClient.next_response is not None
        return _StubAsyncClient.next_response

    monkeypatch.setattr(_StubAsyncClient, "send", _slow_send)
    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "video/mp4"}, b"x")
    with caplog.at_level("WARNING", logger="marcellus.routes.proxy"):
        r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 200
    assert any("slow upstream acquire" in rec.message for rec in caplog.records)


def test_stream_response_acloses_iterator_when_send_raises() -> None:
    """`send()` raising mid-stream (a client disconnect surfaced by ASGI) must
    still release the upstream connection via `body_iterator.aclose()`."""
    from marcellus.routes import proxy as proxy_module

    closed = {"value": False}

    async def body() -> Any:
        try:
            yield b"a"
            yield b"b"
        finally:
            closed["value"] = True

    async def raising_send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("more_body"):
            raise RuntimeError("client disconnected")

    response = proxy_module._BoundedStreamingResponse(
        body(), status_code=200, headers={}, log_path="vod/doorbell/index.m3u8"
    )

    async def run() -> None:
        await response.stream_response(raising_send)

    with pytest.raises(RuntimeError, match="client disconnected"):
        asyncio.run(run())
    assert closed["value"] is True


def test_resp_aclose_raising_is_swallowed(client: TestClient) -> None:
    class _BadCloseResponse(_StubResponse):
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    _StubAsyncClient.next_response = _BadCloseResponse(
        200, {"content-type": "video/mp4"}, b"x"
    )
    r = client.get("/vod/doorbell/index.m3u8")
    assert r.status_code == 200
    assert r.content == b"x"
