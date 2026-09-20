"""Regression tests for the 2026-09 stream-pool-exhaustion incident.

`routes/proxy.py` used to cancel `client.send(req, stream=True)` from outside
via `asyncio.wait_for` when the upstream took too long to answer with
headers. httpcore's `AsyncConnectionPool` drops a cancelled pool request but
can leave the underlying `AsyncHTTP11Connection` stuck ACTIVE forever (never
idle, never expired, never reaped) -- each such timeout permanently burned
one of the stream pool's slots until every route through it 503'd.

The fix shields the send from that cancellation (see the comment above the
`asyncio.wait_for(asyncio.shield(...))` call in `proxy_passthrough`) so the
real network operation always runs to a real conclusion and httpcore always
gets to reclaim the connection itself, whether or not we're still waiting
for it. These tests use a real TCP listener (not a stubbed httpx client) so
`pool_stats()` -- which reaches into httpx's real transport internals -- sees
the real connection-state effects of that fix.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from marcellus import frigate_api
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.routes import proxy as proxy_module
from marcellus.server import create_app


class FakeUpstream:
    """A bare-socket stand-in for Frigate that a test can make behave however
    it wants at the byte level -- accept-and-stall, slow-then-respond, or
    respond immediately -- which a mocked httpx transport can't do, since the
    whole point here is exercising real httpcore connection lifecycle."""

    def __init__(self, handler: Callable[[socket.socket], None]) -> None:
        self._handler = handler
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port: int = self._sock.getsockname()[1]
        self._stop = False
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def _accept_loop(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            self._handler(conn)
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        self._stop = True
        with contextlib.suppress(Exception):
            self._sock.close()


def _read_request_path(conn: socket.socket) -> str:
    data = b""
    conn.settimeout(2.0)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    try:
        return data.split(b" ")[1].decode()
    except IndexError:
        return ""


def _respond_ok(conn: socket.socket, body: bytes = b"ok") -> None:
    conn.sendall(
        b"HTTP/1.1 200 OK\r\ncontent-type: text/plain\r\ncontent-length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )


def _settings(tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, port: int) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url=f"http://127.0.0.1:{port}",
            proxy_base_url=f"http://127.0.0.1:{port}",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=True),
    )


def test_header_timeout_pool_recovers_once_the_shielded_send_lands(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: Any
) -> None:
    """An upstream that accepts the connection but is slower than the header
    wait must still 504 promptly, and -- now that the send is shielded rather
    than cancelled -- once the upstream actually answers, httpcore reclaims
    the connection itself and `pool_stats()` goes back to `active == 0`."""
    monkeypatch.setattr(proxy_module, "_HEADER_WAIT_TIMEOUT_S", 0.15)

    def handler(conn: socket.socket) -> None:
        _read_request_path(conn)
        time.sleep(0.4)  # well past the 504, but the connection is still live
        _respond_ok(conn)

    upstream = FakeUpstream(handler)
    try:
        settings = _settings(tmp_path, frigate_db_path, sidecar_db_path, upstream.port)
        with TestClient(create_app(settings)) as client:
            r = client.get("/hang")
            assert r.status_code == 504
            assert r.json()["detail"]["error"] == "upstream_timeout"

            stream_client = client.app.state.stream_http_client
            deadline = time.monotonic() + 3.0
            stats: dict[str, int] = {}
            while time.monotonic() < deadline:
                stats = frigate_api.pool_stats(stream_client)
                if stats.get("active", 1) == 0:
                    break
                time.sleep(0.05)
            assert stats.get("active") == 0, f"pool never recovered: {stats}"
    finally:
        upstream.close()


def test_repeated_header_timeouts_do_not_permanently_exhaust_the_pool(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: Any
) -> None:
    """Regression for the actual incident: with the old cancel-from-outside
    behavior, each header-wait timeout permanently burned one pool slot, so
    more timeouts than `max_connections` wedged the pool for good and even
    an unrelated fast request would then 503/hang. With the shielded send,
    a request still succeeds afterward."""
    monkeypatch.setattr(
        frigate_api,
        "_STREAM_LIMITS",
        httpx.Limits(max_connections=2, max_keepalive_connections=2, keepalive_expiry=15.0),
    )
    monkeypatch.setattr(proxy_module, "_HEADER_WAIT_TIMEOUT_S", 0.15)

    def handler(conn: socket.socket) -> None:
        path = _read_request_path(conn)
        if path == "/hang":
            time.sleep(0.3)  # past the client's giveup, but finishes quickly
        _respond_ok(conn)

    upstream = FakeUpstream(handler)
    try:
        settings = _settings(tmp_path, frigate_db_path, sidecar_db_path, upstream.port)
        with TestClient(create_app(settings)) as client:
            for _ in range(3):  # more than max_connections (2)
                r = client.get("/hang")
                assert r.status_code == 504

            # Give the last shielded send(s) time to land and free their slot.
            time.sleep(0.5)

            r = client.get("/fast")
            assert r.status_code == 200
            assert r.content == b"ok"
    finally:
        upstream.close()
