"""Regression tests for the 2026-10-17 stream-pool leak on client disconnect.

uvicorn advertises ASGI spec 2.3, so Starlette's `StreamingResponse` runs the
body in a task group next to `listen_for_disconnect` and *cancels* it the
moment the client goes away (a player seeking, an app killed mid-segment).
Under anyio that cancellation is level-triggered: every later `await` inside
the cancelled task is cancelled too, so the proxy's cleanup -- `aclose()` on
the body iterator and, inside it, `resp.aclose()` -- never ran to completion.
The upstream response stayed open, its connection stayed ACTIVE in the pool,
and once Frigate closed its side the socket sat in CLOSE-WAIT for good. On
the user's NVR all 64 stream-pool slots filled that way within ~45 minutes
of simulator testing (128 `preview.mp4` range reads, many killed mid-body),
and every proxied route 503'd `upstream_busy` until a restart.

A second, smaller hole: if the cancel lands before the body generator has
produced its first chunk, `aclose()` on a never-started async generator does
not run its `finally` at all, so `resp.aclose()` there could not help.

These tests drive the app with raw ASGI messages (spec 2.3, as uvicorn sends)
inside the TestClient's own event loop, against a real-socket fake Frigate,
so `pool_stats()` sees httpcore's real connection state.
"""

from __future__ import annotations

import contextlib
import functools
import socket
import time
from pathlib import Path
from typing import Any

import anyio
from fastapi.testclient import TestClient

from marcellus.server import create_app
from tests.test_proxy_pool_recovery import FakeUpstream, _read_request_path, _settings


def _slow_body_handler(first_chunk_delay: float, header_delay: float = 0.0) -> Any:
    """Headers (after `header_delay`), then a long body dribbled out until the
    peer goes away."""

    def handler(conn: socket.socket) -> None:
        _read_request_path(conn)
        time.sleep(header_delay)
        conn.sendall(
            b"HTTP/1.1 206 Partial Content\r\ncontent-type: video/mp4\r\n"
            b"content-length: 10000000\r\n\r\n"
        )
        time.sleep(first_chunk_delay)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            conn.sendall(b"x" * 4096)
            time.sleep(0.02)

    return handler


async def _drive_until_disconnect(
    app: Any, path: str, *, disconnect_after_body: bool, disconnect_at_once: bool = False
) -> None:
    """One proxied GET whose client disconnects: after the first body chunk, as
    soon as the response has started (before any body), or -- `disconnect_at_once`
    -- right after sending the request, i.e. while the sidecar is still waiting
    for Frigate's headers (a player that seeks away from a slow segment)."""
    started = anyio.Event()
    got_body = anyio.Event()
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if not disconnect_at_once:
            await (got_body if disconnect_after_body else started).wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.set()
        elif message["type"] == "http.response.body" and message.get("body"):
            got_body.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"range", b"bytes=0-")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    with anyio.fail_after(10):
        await app(scope, receive, send)


def _open_busy(stream_client: Any) -> int:
    """Connections still OPEN and not idle -- the leak. `pool_stats()` also counts
    a connection httpcore has already closed but not yet pruned (it prunes on
    the next request) as active, which is not a leak."""
    connections = stream_client._transport._pool.connections
    return sum(1 for c in connections if not c.is_closed() and not c.is_idle())


def _await_released(stream_client: Any, seconds: float = 3.0) -> int:
    deadline = time.monotonic() + seconds
    busy = _open_busy(stream_client)
    while busy and time.monotonic() < deadline:
        time.sleep(0.05)
        busy = _open_busy(stream_client)
    return busy


def _run(
    tmp_path: Path,
    frigate_db_path: Path,
    sidecar_db_path: Path,
    *,
    first_chunk_delay: float,
    disconnect_after_body: bool,
    rounds: int = 1,
    header_delay: float = 0.0,
    disconnect_at_once: bool = False,
) -> int:
    """Open, not-idle upstream connections left after the disconnects."""
    upstream = FakeUpstream(_slow_body_handler(first_chunk_delay, header_delay))
    try:
        settings = _settings(tmp_path, frigate_db_path, sidecar_db_path, upstream.port)
        with TestClient(create_app(settings)) as client:
            app = client.app
            for i in range(rounds):
                path = f"/api/cam/start/{i}/end/{i + 10}/preview.mp4"
                client.portal.call(
                    functools.partial(
                        _drive_until_disconnect,
                        app,
                        path,
                        disconnect_after_body=disconnect_after_body,
                        disconnect_at_once=disconnect_at_once,
                    )
                )
            return _await_released(client.app.state.stream_http_client)
    finally:
        upstream.close()


def test_client_disconnect_mid_body_releases_the_upstream_connection(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    busy = _run(
        tmp_path,
        frigate_db_path,
        sidecar_db_path,
        first_chunk_delay=0.0,
        disconnect_after_body=True,
    )
    assert busy == 0, f"{busy} upstream connection(s) left open"


def test_client_disconnect_before_any_body_releases_the_upstream_connection(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    busy = _run(
        tmp_path,
        frigate_db_path,
        sidecar_db_path,
        first_chunk_delay=0.5,
        disconnect_after_body=False,
    )
    assert busy == 0, f"{busy} upstream connection(s) left open"


def test_repeated_disconnects_do_not_accumulate(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    """The incident's shape: many range reads abandoned mid-body in a row."""
    busy = _run(
        tmp_path,
        frigate_db_path,
        sidecar_db_path,
        first_chunk_delay=0.0,
        disconnect_after_body=True,
        rounds=6,
    )
    assert busy == 0, f"{busy} upstream connection(s) left open"


def test_client_gone_while_waiting_for_headers_releases_the_upstream_connection(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    """The incident path: the client has already disconnected by the time
    Frigate's headers arrive, so Starlette cancels the response before its body
    generator ever starts -- and `aclose()` on an un-started generator skips
    the generator's `finally`, which held the only `resp.aclose()`."""
    busy = _run(
        tmp_path,
        frigate_db_path,
        sidecar_db_path,
        first_chunk_delay=0.0,
        disconnect_after_body=False,
        header_delay=0.3,
        disconnect_at_once=True,
        rounds=3,
    )
    assert busy == 0, f"{busy} upstream connection(s) left open"


class _FakeResp:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_reaper_closes_only_upstreams_open_past_their_life(caplog: Any) -> None:
    from types import SimpleNamespace

    from marcellus.routes import proxy as proxy_module

    app = SimpleNamespace(state=SimpleNamespace())
    registry = proxy_module._open_upstreams(app)
    old, fresh = _FakeResp(), _FakeResp()
    registry[1] = proxy_module._OpenUpstream(
        path="api/cam/start/1/end/2/preview.mp4", opened=0.0, resp=old
    )  # type: ignore[arg-type]
    registry[2] = proxy_module._OpenUpstream(path="vod/cam/seg-1.m4s", opened=1000.0, resp=fresh)  # type: ignore[arg-type]
    now = proxy_module._REAP_AFTER_S + 1.0
    reaped = anyio.run(proxy_module.reap_stale_upstreams, app, now)
    assert reaped == 1
    assert old.closed and not fresh.closed
    assert list(registry) == [2]
    assert "reaped leaked upstream path=api/cam/start/1/end/2/preview.mp4" in caplog.text


def test_pool_check_recycles_a_wedged_pool_without_healthz(monkeypatch: Any) -> None:
    """The watchdog's path: `check_stream_pool` alone (no /healthz request)
    recycles once saturation has lasted the recycle window."""
    from types import SimpleNamespace

    from marcellus.routes import health

    sentinel = object()
    app = SimpleNamespace(
        state=SimpleNamespace(stream_http_client=sentinel, _stalled_pool_since=None)
    )
    full = {"connections": health._STREAM_LIMITS.max_connections, "active": 64, "idle": 0}
    monkeypatch.setattr(health, "pool_stats", lambda client: full)
    recycled: list[Any] = []
    monkeypatch.setattr(health, "_recycle_stream_client", lambda a, *_: recycled.append(a))

    stats, saturated = health.check_stream_pool(app, 100.0)
    assert saturated and stats == full and not recycled
    health.check_stream_pool(app, 100.0 + health._STALLED_POOL_RECYCLE_S)
    assert recycled == [app]


def _small_body_handler(header_delay: float) -> Any:
    """Frigate answering an image/JSON request: headers after `header_delay`,
    then a small body sent whole -- so httpx has the complete response (and
    starts closing it) on its very first body read."""

    def handler(conn: socket.socket) -> None:
        _read_request_path(conn)
        time.sleep(header_delay)
        body = b"j" * 800
        conn.sendall(
            b"HTTP/1.1 200 OK\r\ncontent-type: image/jpeg\r\ncontent-length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        # Keep-alive, as Frigate's nginx does: the socket stays open, so a
        # connection httpcore never released is never closed out from under it.
        time.sleep(5.0)

    return handler


def test_burst_of_cancelled_small_requests_releases_every_connection(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    """2026-10-19 wedge: rapid Previous/Next taps fired bursts of event
    thumbnails, snapshots and `/api/events`, and the app cancelled most of
    them in flight. Each request's body was small enough to arrive whole, so
    the client's disconnect cancelled the response while httpx was *closing*
    it. On Python 3.10 `asyncio.wait_for` ran that body read in a child task
    and delivered the cancel natively, which anyio's shield (httpcore's only
    protection for the close) cannot block: every layer had already marked
    itself closed, the connection stayed ACTIVE, and the pool lost a slot for
    good -- 37 of 64 by the evening, then a burst finished it off."""
    upstream = FakeUpstream(_small_body_handler(header_delay=0.05))
    try:
        settings = _settings(tmp_path, frigate_db_path, sidecar_db_path, upstream.port)
        with TestClient(create_app(settings)) as client:
            app = client.app

            async def burst() -> None:
                async with anyio.create_task_group() as tg:
                    for i in range(30):
                        tg.start_soon(
                            functools.partial(
                                _drive_until_disconnect,
                                app,
                                f"/api/events/{i}.0-abc/thumbnail.jpg",
                                disconnect_after_body=False,
                                disconnect_at_once=True,
                            )
                        )

            for _ in range(2):
                client.portal.call(burst)
            busy = _await_released(client.app.state.stream_http_client)
    finally:
        upstream.close()
    assert busy == 0, f"{busy} upstream connection(s) left open"


def test_interrupted_close_is_finished_by_the_release_check() -> None:
    """The backstop on its own: a native `Task.cancel()` landing inside
    httpx's close (what `asyncio.wait_for` did on 3.10) leaves the connection
    ACTIVE with every later `aclose()` a no-op; `_finish_interrupted_close`
    must notice and release it, and must leave a cleanly closed response alone."""
    import asyncio

    import httpx

    from marcellus.routes import proxy as proxy_module

    upstream = FakeUpstream(_small_body_handler(header_delay=0.0))

    async def scenario() -> tuple[int, bool, int, int, bool]:
        async with httpx.AsyncClient() as client:
            url = f"http://127.0.0.1:{upstream.port}/api/events/1.0-abc/thumbnail.jpg"
            resp = await client.send(client.build_request("GET", url), stream=True)
            closing = asyncio.ensure_future(resp.aclose())
            await asyncio.sleep(0)  # let the close start, then cut it short
            closing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await closing
            await resp.aclose()  # a no-op now: everything says "closed"
            leaked = _open_busy(client)
            stepped_in = await proxy_module._finish_interrupted_close(resp, "t")
            after = _open_busy(client)

            clean = await client.send(client.build_request("GET", url), stream=True)
            await clean.aread()
            await clean.aclose()
            untouched = not await proxy_module._finish_interrupted_close(clean, "t")
            return leaked, stepped_in, after, len(client._transport._pool._requests), untouched

    try:
        leaked, stepped_in, after, pending, untouched = anyio.run(scenario)
    finally:
        upstream.close()
    assert leaked == 1, "the simulated interruption should strand the connection"
    assert stepped_in and after == 0 and pending == 0
    assert untouched
