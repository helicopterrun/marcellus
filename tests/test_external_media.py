from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from marcellus.external.media import SourceHttp

HOST = "secret-bernardo.internal"


@dataclass
class FakeSource:
    id: str = "bernardo"
    base_url: str = f"http://{HOST}:8080"
    max_connections: int = 2


def _make(handler: Any) -> SourceHttp:
    return SourceHttp([FakeSource()], transport=httpx.MockTransport(handler))


async def _collect(response: Any) -> bytes:
    out = b""
    async for chunk in response.body_iterator:
        out += chunk if isinstance(chunk, bytes) else bytes(chunk)
    return out


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _relay(handler: Any, path: str = "/clip.mp4", headers: dict[str, str] | None = None) -> Any:
    async def go() -> tuple[Any, bytes]:
        s = _make(handler)
        try:
            r = await s.relay("bernardo", path, headers or {})
            body = await _collect(r) if hasattr(r, "body_iterator") else bytes(r.body)
            return r, body
        finally:
            await s.aclose()

    return _run(go())


def test_200_passthrough_headers() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"abcdef",
            headers={
                "content-type": "video/mp4",
                "etag": '"x"',
                "accept-ranges": "bytes",
                "x-secret": "no",
                "server": HOST,
            },
        )

    r, body = _relay(h)
    assert r.status_code == 200 and body == b"abcdef"
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["etag"] == '"x"'
    assert r.headers["accept-ranges"] == "bytes"
    assert "x-secret" not in r.headers and "server" not in r.headers
    assert HOST not in str(r.headers.items()) and HOST.encode() not in body


def test_range_forwarded_206() -> None:
    seen: dict[str, str] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(206, content=b"bc", headers={"content-range": "bytes 1-2/6"})

    r, body = _relay(h, headers={"Range": "bytes=1-2"})
    assert seen["range"] == "bytes=1-2"
    assert r.status_code == 206 and body == b"bc"
    assert r.headers["content-range"] == "bytes 1-2/6"


def test_304_and_conditional_forwarded() -> None:
    seen: dict[str, str] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(304, headers={"etag": '"x"'})

    r, body = _relay(h, headers={"If-None-Match": '"x"', "If-Modified-Since": "Mon"})
    assert r.status_code == 304 and body == b""
    assert seen["if-none-match"] == '"x"' and seen["if-modified-since"] == "Mon"


def test_416_passthrough() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(416, headers={"content-range": "bytes */6"})

    r, _ = _relay(h)
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */6"


def test_404_media_gone() -> None:
    r, body = _relay(lambda req: httpx.Response(404))
    assert r.status_code == 404
    assert b"media_gone" in body


def test_500_upstream_error() -> None:
    r, body = _relay(lambda req: httpx.Response(500, text=f"boom {HOST}"))
    assert r.status_code == 502
    assert b"upstream_error" in body and b"500" in body
    assert HOST.encode() not in body


def test_connect_error_unavailable() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {HOST}", request=req)

    r, body = _relay(h)
    assert r.status_code == 502
    assert b"upstream_unavailable" in body and HOST.encode() not in body


@pytest.mark.parametrize("path", ["clip.mp4", "http://evil/x", "//evil/x", ""])
def test_bad_path_rejected(path: str) -> None:
    async def go() -> None:
        s = _make(lambda req: httpx.Response(200))
        try:
            await s.relay("bernardo", path, {})
        finally:
            await s.aclose()

    with pytest.raises(ValueError):
        _run(go())


def test_disallowed_request_headers_not_forwarded() -> None:
    seen: dict[str, str] = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, content=b"")

    _relay(h, headers={"Cookie": "a=b", "Authorization": "Bearer t", "Range": "bytes=0-1"})
    assert "cookie" not in seen and "authorization" not in seen
    assert seen["range"] == "bytes=0-1"


def test_client_lookup() -> None:
    async def go() -> None:
        s = _make(lambda req: httpx.Response(200))
        try:
            assert isinstance(s.client("bernardo"), httpx.AsyncClient)
            with pytest.raises(KeyError):
                s.client("nope")
        finally:
            await s.aclose()

    _run(go())
