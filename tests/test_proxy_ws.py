"""Tests for the proxy's WebSocket relay (routes/proxy.py).

An HTTP-only proxy silently broke live view for a client pointed at the sidecar
as its single base URL: Frigate's `/ws` state feed and go2rtc's WebRTC
signalling are both WebSockets, and they simply never connected.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _serve() -> object | None:
    """websockets moved its server API in 14.x, same split as the client."""
    import importlib

    for module_name in ("websockets.asyncio.server", "websockets.server"):
        try:
            return importlib.import_module(module_name).serve
        except (ImportError, AttributeError):
            continue
    return None


@pytest.fixture
def echo_server() -> Iterator[int]:
    """A real upstream WebSocket echo server on localhost."""
    serve = _serve()
    if serve is None:  # pragma: no cover
        pytest.skip("websockets not installed")

    port_holder: list[int] = []
    stop_holder: list[asyncio.Future[None]] = []
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    async def handler(ws: object) -> None:
        async for message in ws:  # type: ignore[attr-defined]
            if isinstance(message, bytes):
                await ws.send(b"bin:" + message)  # type: ignore[attr-defined]
            else:
                await ws.send(f"echo:{message}")  # type: ignore[attr-defined]

    async def main() -> None:
        async with serve(handler, "127.0.0.1", 0) as server:  # type: ignore[operator]
            sockets = getattr(server, "sockets", None) or server.server.sockets
            port_holder.append(sockets[0].getsockname()[1])
            stop_holder.append(asyncio.get_running_loop().create_future())
            ready.set()
            await stop_holder[0]

    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    assert ready.wait(10), "echo server did not start"
    try:
        yield port_holder[0]
    finally:
        loop.call_soon_threadsafe(stop_holder[0].set_result, None)
        thread.join(timeout=5)
        if not loop.is_running():
            loop.close()


def _client(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, port: int, **proxy_kw: object
) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url=f"http://127.0.0.1:{port}",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=True, **proxy_kw),  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings))


def test_text_and_binary_frames_relay_both_ways(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, echo_server: int
) -> None:
    client = _client(frigate_db_path, sidecar_db_path, tmp_path, echo_server)
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo:hello"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"bin:\x00\x01"


def test_query_string_travels_with_the_upgrade(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, echo_server: int
) -> None:
    """go2rtc's signalling carries the stream name in the query string."""
    client = _client(frigate_db_path, sidecar_db_path, tmp_path, echo_server)
    with client.websocket_connect("/live/webrtc/api/ws?src=doorbell") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"


def test_v1_namespace_is_not_relayed(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, echo_server: int
) -> None:
    """`/v1` belongs to the sidecar on every protocol, not just HTTP."""
    from starlette.websockets import WebSocketDisconnect

    client = _client(frigate_db_path, sidecar_db_path, tmp_path, echo_server)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/v1/anything") as ws:
        ws.receive_text()


def test_disabled_proxy_refuses_the_upgrade(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, echo_server: int
) -> None:
    from starlette.websockets import WebSocketDisconnect

    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url=f"http://127.0.0.1:{echo_server}",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=False),
    )
    client = TestClient(create_app(settings))
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
        ws.receive_text()


def test_client_leaving_during_the_handshake_is_not_an_error(
    frigate_db_path: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real uvicorn: its websockets protocol never opens if the client leaves mid-handshake,
    and closing it then raises AttributeError (seen in bursts on production, 2026-11-01)."""
    import socket

    import uvicorn

    from marcellus.routes import proxy

    class Upstream:
        subprotocol = None
        closed = False

        async def close(self) -> None:
            self.closed = True

        def __aiter__(self) -> Upstream:
            return self

        async def __anext__(self) -> bytes:
            await asyncio.sleep(3600)
            raise StopAsyncIteration

    upstream = Upstream()

    async def connect(*_: object) -> Upstream:
        # The client's disconnect lands while the upstream is still connecting.
        await asyncio.sleep(0.2)
        return upstream

    monkeypatch.setattr(proxy, "_ws_connector", lambda: connect)
    app = _client(frigate_db_path, sidecar_db_path, tmp_path, 9).app

    async def scenario() -> None:
        # log_config=None: uvicorn's default stops its loggers propagating to caplog.
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, ws="websockets", lifespan="off", log_config=None
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as raw:
            raw.sendall(
                b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
            )
            await asyncio.sleep(0.05)
        for _ in range(300):
            if upstream.closed:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        server.should_exit = True
        await task

    with caplog.at_level("ERROR", logger="uvicorn.error"):
        asyncio.run(scenario())

    assert upstream.closed
    assert "Exception in ASGI application" not in caplog.text
