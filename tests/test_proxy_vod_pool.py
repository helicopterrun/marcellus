"""Recorded playback keeps its own upstream pool.

On 2026-10-19 a burst of event thumbnails, snapshots and `/api/events` from
rapid event-chip taps filled the one stream pool every proxied request shared,
and the `/vod` segments of a video already playing 503'd `upstream_busy` for
30 s. `/vod/*` now goes through `frigate_api.get_vod_stream_client()`, so no
amount of image/JSON traffic can take its slots.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from marcellus import frigate_api
from marcellus.routes import health
from marcellus.routes import proxy as proxy_module
from marcellus.server import create_app
from tests.test_proxy_pool_recovery import FakeUpstream, _read_request_path, _respond_ok, _settings


def _held_or_ok(conn: socket.socket) -> None:
    """`/hold/*` answers headers then never finishes its body (a slot held for
    the whole test); anything else answers at once."""
    path = _read_request_path(conn)
    if path.startswith("/hold/"):
        conn.sendall(
            b"HTTP/1.1 200 OK\r\ncontent-type: image/jpeg\r\ncontent-length: 999999\r\n\r\nx"
        )
        time.sleep(5.0)
        return
    _respond_ok(conn, b"segment")


def test_vod_is_served_while_the_shared_pool_is_full(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        frigate_api, "_STREAM_LIMITS", httpx.Limits(max_connections=2, max_keepalive_connections=2)
    )
    # Fail the shared pool's wait in a blink rather than the real 5 s.
    monkeypatch.setattr(proxy_module, "_UPSTREAM_TIMEOUT", httpx.Timeout(30.0, pool=0.3))
    upstream = FakeUpstream(_held_or_ok)
    try:
        settings = _settings(tmp_path, frigate_db_path, sidecar_db_path, upstream.port)
        with TestClient(create_app(settings)) as client:
            app = client.app
            shared = frigate_api.get_stream_client(app)
            base = f"http://127.0.0.1:{upstream.port}"

            async def fill_shared_pool() -> list[httpx.Response]:
                return [
                    await shared.send(shared.build_request("GET", f"{base}/hold/{i}"), stream=True)
                    for i in range(2)
                ]

            held = client.portal.call(fill_shared_pool)
            try:
                api = client.get("/api/events/1.0-abc/thumbnail.jpg")
                vod = client.get("/vod/gate-face/start/1/end/2/seg-46-v1-a1.m4s")
            finally:
                for resp in held:
                    client.portal.call(resp.aclose)
            assert api.status_code == 503
            assert vod.status_code == 200 and vod.content == b"segment"
            assert app.state.vod_stream_http_client is not app.state.stream_http_client
    finally:
        upstream.close()


def test_vod_pool_is_watched_and_recycled_like_the_shared_one(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    vod_client, shared_client = object(), object()
    app = SimpleNamespace(
        state=SimpleNamespace(
            vod_stream_http_client=vod_client,
            stream_http_client=shared_client,
            _stalled_vod_pool_since=None,
            _stalled_pool_since=None,
        )
    )
    full = {"connections": frigate_api._VOD_STREAM_LIMITS.max_connections, "active": 32, "idle": 0}
    monkeypatch.setattr(health, "pool_stats", lambda c: full if c is vod_client else {"idle": 1})
    recycled: list[str] = []
    monkeypatch.setattr(
        health, "_recycle_stream_client", lambda a, pool: recycled.append(pool.name)
    )

    stats, saturated = health.check_vod_stream_pool(app, 100.0)
    assert saturated and stats == full and not recycled
    health.check_stream_pool(app, 100.0 + health._STALLED_POOL_RECYCLE_S)
    assert not recycled  # the shared pool is fine
    health.check_vod_stream_pool(app, 100.0 + health._STALLED_POOL_RECYCLE_S)
    assert recycled == ["vod"]
