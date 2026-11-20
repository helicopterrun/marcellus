"""Response compression skips media (compression.py) on any Starlette."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from marcellus.compression import MediaSafeGZipMiddleware, skips_content_type
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app

_BIG = b"0123456789abcdef" * 512  # 8 KiB, compresses well


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
    next_response: _StubResponse | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        return {"method": method, "url": url, **kwargs}

    async def send(self, req: dict[str, Any], stream: bool = False) -> _StubResponse:
        assert _StubAsyncClient.next_response is not None
        return _StubAsyncClient.next_response

    async def aclose(self) -> None:
        return None


@pytest.fixture
def proxy_client(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)
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


@pytest.mark.parametrize(
    "path",
    ["/vod/camera_a/start/1/end/2/seg-1.m4s", "/api/camera_a/start/1/end/2/clip.mp4"],
)
def test_proxied_video_is_never_gzipped(proxy_client: TestClient, path: str) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        200, {"content-type": "video/mp4", "content-length": str(len(_BIG))}, _BIG
    )
    r = proxy_client.get(path, headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
    assert r.content == _BIG


def test_proxied_playlist_is_never_gzipped(proxy_client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        200, {"content-type": "application/vnd.apple.mpegurl"}, _BIG
    )
    r = proxy_client.get("/api/camera_a/playlist.m3u8", headers={"accept-encoding": "gzip"})
    assert "content-encoding" not in r.headers
    assert r.content == _BIG


def test_proxied_json_still_compresses(proxy_client: TestClient) -> None:
    body = json.dumps([{"id": i, "label": "person"} for i in range(400)]).encode()
    _StubAsyncClient.next_response = _StubResponse(
        200, {"content-type": "application/json"}, body
    )
    r = proxy_client.get("/api/events", headers={"accept-encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.json()[0]["label"] == "person"


def test_sidecar_json_still_compresses(proxy_client: TestClient) -> None:
    r = proxy_client.get("/openapi.json", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json"
    assert len(r.content) >= 1024
    assert r.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in r.headers["vary"].lower()


# -- the middleware alone ------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/json")
    def _json() -> JSONResponse:
        return JSONResponse({"x": "y" * 4000})

    @app.get("/small")
    def _small() -> JSONResponse:
        return JSONResponse({"x": 1})

    @app.get("/jpeg")
    def _jpeg() -> Response:
        return Response(_BIG, media_type="image/jpeg")

    @app.get("/svg")
    def _svg() -> Response:
        return Response(_BIG, media_type="image/svg+xml")

    @app.get("/octet")
    def _octet() -> Response:
        return Response(_BIG, media_type="application/octet-stream")

    @app.get("/partial")
    def _partial() -> Response:
        return Response(_BIG, status_code=206, media_type="text/plain")

    @app.get("/encoded")
    def _encoded() -> Response:
        return Response(
            gzip.compress(_BIG), media_type="text/plain", headers={"content-encoding": "gzip"}
        )

    @app.get("/stream")
    def _stream() -> StreamingResponse:
        async def gen() -> Any:
            for _ in range(4):
                yield _BIG

        return StreamingResponse(gen(), media_type="application/json")

    app.add_middleware(MediaSafeGZipMiddleware, minimum_size=1024, compresslevel=6)
    return app


@pytest.fixture
def mw_client() -> TestClient:
    return TestClient(_app())


@pytest.mark.parametrize(
    ("path", "gzipped"),
    [
        ("/json", True),
        ("/svg", True),
        ("/stream", True),
        ("/small", False),
        ("/jpeg", False),
        ("/octet", False),
        ("/partial", False),
    ],
)
def test_what_compresses(mw_client: TestClient, path: str, gzipped: bool) -> None:
    r = mw_client.get(path, headers={"accept-encoding": "gzip"})
    assert (r.headers.get("content-encoding") == "gzip") is gzipped, r.headers
    if gzipped and path != "/stream":
        assert int(r.headers["content-length"]) < len(r.content)


def test_streamed_body_round_trips(mw_client: TestClient) -> None:
    r = mw_client.get("/stream", headers={"accept-encoding": "gzip"})
    assert r.content == _BIG * 4
    assert "content-length" not in r.headers


def test_already_encoded_body_is_left_alone(mw_client: TestClient) -> None:
    r = mw_client.get("/encoded", headers={"accept-encoding": "gzip"})
    assert r.content == _BIG  # decoded once by the client, not twice-encoded


def test_no_accept_encoding_means_no_gzip(mw_client: TestClient) -> None:
    r = mw_client.get("/json", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in r.headers


@pytest.mark.parametrize(
    ("content_type", "skipped"),
    [
        ("video/mp4", True),
        ("video/MP2T", True),
        ("image/png", True),
        ("audio/aac", True),
        ("application/vnd.apple.mpegurl; charset=utf-8", True),
        ("application/x-mpegURL", True),
        ("application/octet-stream", True),
        ("image/svg+xml", False),
        ("application/json", False),
        ("text/html; charset=utf-8", False),
        ("", False),
    ],
)
def test_skips_content_type(content_type: str, skipped: bool) -> None:
    assert skips_content_type(content_type) is skipped
