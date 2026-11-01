"""Transparent reverse proxy to Frigate's authenticated origin.

Gives Elsinore one base URL: everything the sidecar doesn't handle itself
(``/api/*``, ``/vod/*``, ``/live/*``, ``/preview/*``, and any other Frigate
path) streams through to Frigate's authed port unchanged, with ``Range`` and
the client's own auth cookie/header passed through untouched. Auth stays
entirely Frigate's -- the sidecar never holds or validates the password, and
never applies its own session gate here (see auth.py).

Registered LAST in server.py so ``/v1/*``, ``/static``, the sidecar's own
pages, and ``/healthz`` all win first; only unmatched paths fall through here.

The body is relayed **raw**: httpx transparently decodes ``Content-Encoding``
when you iterate the decoded stream, so forwarding the upstream
``content-length`` alongside a decoded body produced a length that disagreed
with the bytes on the wire for every gzipped Frigate response. Streaming the
raw bytes and relaying ``content-encoding`` keeps the two consistent and is
what a transparent proxy should do anyway.

WebSockets are proxied too (``/ws`` for Frigate's state feed, go2rtc's WebRTC
signalling): an HTTP-only proxy silently broke live view for a client pointed
at the sidecar as its single origin.

See docs/scrub-cache-and-proxy-spec.md §6.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from marcellus.errors import error_detail
from marcellus.frigate_api import get_stream_client, get_vod_stream_client, pool_stats

logger = logging.getLogger(__name__)

router = APIRouter(tags=["proxy"])

# Relayed verbatim from the upstream response. `content-encoding` belongs here
# because the body is streamed raw; `location` because a redirect without it is
# just a broken response.
_RESP_PASS = (
    "content-type",
    "content-length",
    "content-encoding",
    "content-range",
    "accept-ranges",
    "cache-control",
    "etag",
    "last-modified",
    "location",
    "www-authenticate",
)

_WS_SUBPROTOCOL_HEADER = "sec-websocket-protocol"

# `read=` here is deliberately *not* None even though VOD/live media are
# long-lived streams the user pauses and seeks: the body path is already
# bounded by `_IDLE_CHUNK_TIMEOUT_S` (30 s), which always fires first, so this
# never cuts a stream off mid-view. What it does bound is the header phase of
# a *shielded* `client.send` (see `proxy_passthrough`) -- an upstream that
# accepts the connection but never sends a status line would otherwise hold
# that connection ACTIVE forever, since nothing outside httpcore can cancel
# the send without leaking it. At 60 s httpcore raises `ReadTimeout` from
# inside the pool and reclaims the connection itself.
_LATE_HEADER_ABANDON_S = 60.0
_UPSTREAM_TIMEOUT = httpx.Timeout(30.0, read=_LATE_HEADER_ABANDON_S, pool=5.0)

# Bounds how long we'll wait for the upstream's response headers to arrive
# (the `client.send(req, stream=True)` call below). `_UPSTREAM_TIMEOUT`'s
# `read=None` is scoped to the body stream, not header arrival -- an upstream
# that accepts the connection but never sends a status line/headers (Frigate's
# nginx half-closing a keepalive socket) would otherwise hang this call
# indefinitely with nothing to show for it at `/healthz`.
_HEADER_WAIT_TIMEOUT_S = 10.0

# A `client.send` that takes longer than this to return (but still succeeds)
# is worth a log line even though it didn't time out -- an early signal that
# the pool is under pressure before it actually wedges.
_SLOW_ACQUIRE_LOG_THRESHOLD_S = 1.0

# What actually bounds a stalled body chunk -- a chunk that doesn't arrive
# within this many idle seconds ends the response instead of holding the
# connection (and the client's wait) open indefinitely. Must stay below
# `_LATE_HEADER_ABANDON_S` so httpx's own read timeout never reaches the body.
_IDLE_CHUNK_TIMEOUT_S = 30.0
assert _IDLE_CHUNK_TIMEOUT_S < _LATE_HEADER_ABANDON_S

# The idle-chunk watchdog above only bounds a stalled *upstream*. It does
# nothing for a client that stopped reading (a paused player): the `send()`
# call in `_BoundedStreamingResponse.stream_response` then blocks forever on
# backpressure while still holding the upstream connection open underneath it
# -- exactly what pinned the shared pool on 2026-09-10 and 502'd every other
# Frigate-backed route for 30s at a time. `_CLIENT_STALL_TIMEOUT_S` bounds
# each individual `send()`; `_STREAM_MAX_DURATION_S` bounds the whole
# response regardless of how promptly the client keeps reading (HLS
# segments/vod chunks are seconds long and playlists are tiny, so a real
# response finishes in a small fraction of this).
_CLIENT_STALL_TIMEOUT_S = 30.0
_STREAM_MAX_DURATION_S = 600.0

# Releasing the upstream must survive the response being cancelled. uvicorn
# advertises ASGI spec 2.3, so Starlette runs the body next to
# `listen_for_disconnect` in a task group and cancels it the moment the client
# goes away (a player seeking, an app killed mid-segment). anyio cancellation
# is level-triggered: every later `await` in that task is cancelled too, so an
# unshielded `await resp.aclose()` in a `finally` never completes and the
# connection stays ACTIVE in the pool -- in CLOSE-WAIT once Frigate hangs up.
# On 2026-10-17 that filled all 64 stream-pool slots in ~45 minutes of
# simulator testing (Python 3.10 / Starlette 1.0.0 / anyio 4.13 in prod; the
# newer stack on the dev Mac did not reproduce it). The release is therefore
# shielded, and bounded so a hung close can never pin the response either.
#
# The shield only holds against *anyio* cancellation. On 2026-10-19 the pool
# still leaked (2 -> 37 of 64 slots over 40 h, then a burst of event-chip
# taps filled it and recorded playback 503'd for 30 s): the idle-chunk bound
# was `asyncio.wait_for(chunks.__anext__())`, which on Python 3.10 runs the
# read in a child task and, when Starlette cancels the response, cancels that
# task natively. A small image/JSON body arrives whole on the first read, so a
# client that hung up early caught httpx mid-close: httpx, httpcore's pool
# stream and the HTTP/1.1 stream each mark themselves closed *before*
# awaiting, the native cancel cut `_response_closed()` short inside
# httpcore's own shield, and the connection stayed ACTIVE with every later
# `aclose()` a no-op -- and already out of the reaper's registry. (3.12+
# `wait_for` runs in-task, which is why the dev Mac never saw it.) Nothing may
# therefore read or close an upstream response under `asyncio.wait_for`; those
# time limits are anyio scopes in the same task, and `_finish_interrupted_close`
# checks after every release that httpcore really let go.
_RELEASE_TIMEOUT_S = 5.0

# Backstop for any path that still leaks: every upstream response is
# registered while open, and `reap_stale_upstreams` (run by the server's
# proxy watchdog) closes one still open this long after it was opened. No
# legitimate response outlives `_STREAM_MAX_DURATION_S`, so anything past it
# plus a minute is a leak -- logged with its path and progress so the next
# one names its own cause, and closed so it can't hold a pool slot for good.
_REAP_AFTER_S = _STREAM_MAX_DURATION_S + 60.0


@dataclass
class _OpenUpstream:
    path: str
    opened: float
    resp: httpx.Response
    body_started: bool = False
    bytes_relayed: int = 0


def _open_upstreams(app: Any) -> dict[int, _OpenUpstream]:
    registry = getattr(app.state, "proxy_open_upstreams", None)
    if registry is None:
        registry = {}
        app.state.proxy_open_upstreams = registry
    return registry


async def _finish_interrupted_close(resp: httpx.Response, path: str) -> bool:
    """Release the connection behind `resp` if an earlier close was cut short.

    Call after `resp.aclose()`. A completed close removes the request from
    httpcore's pool; one interrupted mid-way (the 2026-10-19 leak, see
    `_RELEASE_TIMEOUT_S`) leaves the request in the pool holding an ACTIVE
    connection, and every further `aclose()` is a no-op. This finishes that
    close the way httpcore's own `PoolByteStream.aclose` would have: close
    the connection, drop the request, let the pool prune and reassign.

    Reaches into httpx/httpcore internals, so any shape change on an upgrade
    degrades this to a no-op rather than an error. Returns True if it had to
    step in.
    """
    try:
        stream: Any = resp.stream
        pool_stream = stream._stream._httpcore_stream
        pool = pool_stream._pool
        pool_request = pool_stream._pool_request
        if pool_request not in pool._requests:
            return False
        connection = pool_request.connection
        logger.warning(
            "proxy: upstream close was interrupted, force-releasing its connection path=%s",
            path,
        )
        if connection is not None:
            await connection.aclose()
        with pool._optional_thread_lock:
            if pool_request in pool._requests:
                pool._requests.remove(pool_request)
            closing = pool._assign_requests_to_connections()
        await pool._close_connections(closing)
        return True
    except Exception:
        logger.debug("proxy: could not check upstream release for %s", path, exc_info=True)
        return False


async def reap_stale_upstreams(app: Any, now: float | None = None) -> int:
    """Close upstream responses open longer than `_REAP_AFTER_S`; returns how many."""
    registry = _open_upstreams(app)
    now = asyncio.get_event_loop().time() if now is None else now
    stale = [(key, entry) for key, entry in registry.items() if now - entry.opened > _REAP_AFTER_S]
    for key, entry in stale:
        registry.pop(key, None)
        logger.warning(
            "proxy: reaped leaked upstream path=%s age=%.0fs body_started=%s bytes=%d",
            entry.path,
            now - entry.opened,
            entry.body_started,
            entry.bytes_relayed,
        )
        with anyio.move_on_after(_RELEASE_TIMEOUT_S, shield=True):
            with contextlib.suppress(Exception):
                await entry.resp.aclose()
            await _finish_interrupted_close(entry.resp, entry.path)
    return len(stale)


class _BoundedStreamingResponse(StreamingResponse):
    """StreamingResponse that also bounds the client side and the total
    duration of the response, not just a stalled upstream chunk.

    Ending the response here (rather than letting `send()` hang, or raising
    into uvicorn) makes the `stream_body()` generator's `finally: await
    resp.aclose()` run via an explicit `aclose()` on the body iterator, so the
    upstream connection is always released back to the pool.
    """

    def __init__(
        self,
        *args: Any,
        log_path: str,
        release_upstream: Callable[[], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._log_path = log_path
        self._release_upstream = release_upstream
        self._released = False

    async def _release(self) -> None:
        """Close the body iterator and the upstream response, exactly once,
        shielded from cancellation and bounded by `_RELEASE_TIMEOUT_S`.

        Both steps matter: `aclose()` on a body iterator that never produced a
        chunk does not run its `finally` (an un-started async generator), so
        the upstream response is closed directly as well.
        """
        if self._released:
            return
        self._released = True
        with anyio.move_on_after(_RELEASE_TIMEOUT_S, shield=True):
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()
            if self._release_upstream is not None:
                with contextlib.suppress(Exception):
                    await self._release_upstream()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Covers the paths `stream_response` never reaches or never finishes:
        # cancelled before it starts, or `send` of the start message failing.
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._release()

    async def stream_response(self, send: Send) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _STREAM_MAX_DURATION_S
        bytes_sent = 0
        reason: str | None = None
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            async for chunk in self.body_iterator:
                if not isinstance(chunk, (bytes, memoryview)):
                    chunk = chunk.encode(self.charset)
                if loop.time() >= deadline:
                    reason = "max_duration"
                    break
                with anyio.move_on_after(_CLIENT_STALL_TIMEOUT_S) as stall:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                if stall.cancelled_caught:
                    reason = "client_stall"
                    break
                bytes_sent += len(chunk)
        finally:
            # Always release the upstream connection regardless of how the
            # loop above ended -- client disconnect (a cancellation, see
            # `_RELEASE_TIMEOUT_S`), any exception raised out of
            # `send`/iteration, or a normal fall-through. This is what returns
            # the connection to the pool; skipping it is exactly what let
            # CLOSE-WAIT sockets pile up.
            await self._release()

        if reason is not None:
            logger.warning(
                "proxy: stream aborted path=%s reason=%s bytes=%d",
                self._log_path,
                reason,
                bytes_sent,
            )

        # Best-effort close frame either way: a normal end-of-stream needs it
        # to terminate the response cleanly; on a stall/deadline abort the
        # client is presumably not reading anyway, so a failure here is
        # expected and harmless.
        with contextlib.suppress(Exception), anyio.move_on_after(_CLIENT_STALL_TIMEOUT_S):
            await send({"type": "http.response.body", "body": b"", "more_body": False})


def _upstream_url(settings: Any, path: str, query: str, *, scheme_ws: bool = False) -> str:
    base = settings.frigate.proxy_base_url.rstrip("/")
    if scheme_ws:
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
    url = f"{base}/{path}"
    return f"{url}?{query}" if query else url


def _reap_late_send(send_task: asyncio.Task[httpx.Response], upstream: str) -> None:
    """Close a `client.send()` that finally lands after its 504 was returned.

    We already gave up and answered the client, but the send itself was only
    *shielded* from that timeout, not cancelled -- see the comment above its
    call site. If it eventually succeeds, its response holds a real upstream
    connection open forever unless something reads or closes it; nothing else
    is going to, so this does. If it raises, there's nothing to close.
    """

    def _on_done(task: asyncio.Task[httpx.Response]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug(
                "proxy: late send after header-wait timeout failed for %s: %r",
                upstream,
                exc,
            )
            return
        late_resp = task.result()

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await late_resp.aclose()

        asyncio.ensure_future(_close())

    send_task.add_done_callback(_on_done)


def _reject_reserved(path: str) -> None:
    # `/v1` is a namespace reserved entirely for the sidecar's own endpoints
    # (docs/scrub-cache-and-proxy-spec.md §4.0) -- an unmatched /v1/* path
    # must JSON-404 here, never fall through to Frigate (which could 200 it
    # with its SPA shell, or the upstream could simply be unreachable and
    # return a confusing 502 for what is really a 404).
    if path == "v1" or path.startswith("v1/"):
        raise HTTPException(
            status_code=404, detail=error_detail("not_generated", "unknown /v1 path")
        )
    # Defense-in-depth: FastAPI's router already resolves ".." segments before
    # matching, but reject explicitly too (matches wildlife.py's guard).
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail=error_detail("bad_path", "bad path"))


@router.api_route(
    "/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_passthrough(path: str, request: Request) -> Any:
    settings = request.app.state.settings
    if not settings.proxy.enabled:
        raise HTTPException(
            status_code=404, detail=error_detail("proxy_disabled", "proxy disabled")
        )

    _reject_reserved(path)

    upstream = _upstream_url(settings, path, request.url.query)

    pass_headers = {h.lower() for h in settings.proxy.pass_request_headers}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in pass_headers}
    # The client's own negotiation has to travel with the raw body we relay,
    # otherwise httpx substitutes its own and the response encoding no longer
    # matches what the client asked for.
    if "accept-encoding" in request.headers:
        fwd_headers["accept-encoding"] = request.headers["accept-encoding"]
    else:
        fwd_headers["accept-encoding"] = "identity"

    body = await request.body()

    # Recorded playback never shares a pool with the app's image/JSON bursts
    # (see `frigate_api._VOD_STREAM_LIMITS`).
    if path.startswith("vod/"):
        client = get_vod_stream_client(request.app)
    else:
        client = get_stream_client(request.app)
    try:
        req = client.build_request(
            request.method,
            upstream,
            headers=fwd_headers,
            content=body or None,
            timeout=_UPSTREAM_TIMEOUT,
        )
        send_start = asyncio.get_event_loop().time()
        # `client.send(req, stream=True)` is wrapped in a real Task and only
        # *shielded* from the timeout below, never cancelled by it. httpx has
        # no per-request read timeout that bounds header arrival without also
        # bounding every subsequent body chunk read (httpcore's http11
        # connection keys both off the same `extensions["timeout"]["read"]`),
        # so a `read=` timeout here would fight `_IDLE_CHUNK_TIMEOUT_S` below.
        # Cancelling the send from outside instead of shielding it is exactly
        # what caused the 2026-09 pool-exhaustion incident: a cancelled send
        # is torn out of httpcore's `AsyncConnectionPool` mid-flight, but the
        # `AsyncHTTP11Connection` underneath it can be left ACTIVE forever
        # (never idle, never expired, never reaped) -- see the module-level
        # incident note. Shielding lets the send keep running to a real
        # conclusion (success or its own error) so httpcore always gets to
        # put the connection back to IDLE/CLOSED itself; `_reap_late_send`
        # below just closes the response if one shows up after we've already
        # given up and returned a 504.
        send_task = asyncio.ensure_future(client.send(req, stream=True))
        try:
            resp = await asyncio.wait_for(asyncio.shield(send_task), timeout=_HEADER_WAIT_TIMEOUT_S)
        except asyncio.CancelledError:
            # This handler cancelled mid-wait (shutdown, or any future
            # middleware that cancels on disconnect): the shielded send still
            # lands, and its response would hold a slot with no one to close it.
            _reap_late_send(send_task, upstream)
            raise
        except (asyncio.TimeoutError, httpx.ReadTimeout) as exc:
            logger.warning(
                "proxy: header wait timed out (%.0fs) streaming %s pool_stats=%s",
                _HEADER_WAIT_TIMEOUT_S,
                upstream,
                pool_stats(client),
            )
            _reap_late_send(send_task, upstream)
            raise HTTPException(
                status_code=504,
                detail=error_detail("upstream_timeout", "timed out waiting for upstream headers"),
            ) from exc
        send_elapsed = asyncio.get_event_loop().time() - send_start
        if send_elapsed > _SLOW_ACQUIRE_LOG_THRESHOLD_S:
            logger.warning(
                "proxy: slow upstream acquire path=%s secs=%.1f pool_stats=%s",
                path,
                send_elapsed,
                pool_stats(client),
            )
    except httpx.PoolTimeout as exc:
        stats = pool_stats(client)
        logger.warning("proxy: pool exhausted streaming %s pool_stats=%s", upstream, stats)
        raise HTTPException(
            status_code=503,
            detail=error_detail("upstream_busy", "upstream connection pool exhausted"),
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=error_detail("upstream_unavailable", f"upstream error: {exc}")
        ) from exc

    headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

    # httpx hands back an already-read response in a few cases (redirect and
    # auth flows, and non-streaming transports). Its body is then decoded and
    # buffered, so the upstream framing headers no longer describe the bytes
    # we're about to send -- drop them and let Starlette frame it instead.
    buffered = getattr(resp, "is_stream_consumed", False)
    if buffered:
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)

    registry = _open_upstreams(request.app)
    entry = _OpenUpstream(path=path, opened=asyncio.get_event_loop().time(), resp=resp)
    registry[id(resp)] = entry

    async def release_upstream() -> None:
        registry.pop(id(resp), None)
        await resp.aclose()
        await _finish_interrupted_close(resp, path)

    async def stream_body() -> Any:
        try:
            entry.body_started = True
            if buffered:
                yield resp.content
            else:
                chunks = resp.aiter_raw()
                while True:
                    # An anyio scope, never `asyncio.wait_for`: on Python 3.10
                    # `wait_for` runs the read in a child task and cancels it
                    # with a native `Task.cancel()`, which anyio's shield cannot
                    # block. A client disconnect landing while httpx closes a
                    # small, fully read body then left the connection ACTIVE
                    # for good -- see the 2026-10-19 note at `_RELEASE_TIMEOUT_S`.
                    with anyio.move_on_after(_IDLE_CHUNK_TIMEOUT_S) as idle:
                        try:
                            chunk = await chunks.__anext__()
                        except StopAsyncIteration:
                            break
                    if idle.cancelled_caught:
                        logger.warning(
                            "proxy: idle read timeout (%.0fs) streaming %s; ending response",
                            _IDLE_CHUNK_TIMEOUT_S,
                            upstream,
                        )
                        break
                    entry.bytes_relayed += len(chunk)
                    yield chunk
        finally:
            # A cancelled `__anext__` (e.g. from `_BoundedStreamingResponse`'s
            # own stall/duration watchdog reaching in and closing this
            # generator) must never skip releasing the upstream connection.
            try:
                await release_upstream()
            except Exception:
                logger.debug("proxy: resp.aclose() raised closing %s", upstream, exc_info=True)

    response = _BoundedStreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=headers,
        media_type=resp.headers.get("content-type"),
        log_path=path,
        release_upstream=release_upstream,
    )
    # Set-Cookie is the one header Frigate can legitimately send more than once
    # (login sets both the session and its refresh companion); reading it off
    # the mapping would comma-join them into a single malformed cookie.
    for value in _header_list(resp.headers, "set-cookie"):
        response.raw_headers.append((b"set-cookie", value.encode("latin-1")))
    return response


def _header_list(headers: Any, name: str) -> list[str]:
    get_list = getattr(headers, "get_list", None)
    if callable(get_list):
        return list(get_list(name))
    value = headers.get(name)
    return [value] if value else []


#: (module, keyword for extra request headers) -- websockets moved `connect`
#: and renamed `extra_headers` to `additional_headers` in 14.x, and
#: uvicorn[standard] can pull either side of that split.
_WS_CLIENTS = (
    ("websockets.asyncio.client", "additional_headers"),
    ("websockets.client", "extra_headers"),
)


def _ws_connector() -> Any:
    """Return `await connect(url, headers, subprotocols)`, or None if unavailable."""
    import importlib

    for module_name, headers_kw in _WS_CLIENTS:
        try:
            connect = importlib.import_module(module_name).connect
        except (ImportError, AttributeError):
            continue

        async def _connect(
            url: str,
            headers: list[tuple[str, str]],
            subs: list[str],
            _connect: Any = connect,
            _kw: str = headers_kw,
        ) -> Any:
            return await _connect(url, **{_kw: headers}, subprotocols=subs or None, open_timeout=10)

        return _connect
    return None


@router.websocket("/{path:path}")
async def proxy_websocket(path: str, websocket: WebSocket) -> None:
    """Bidirectional WebSocket relay to Frigate (state feed, WebRTC signalling)."""
    settings = websocket.app.state.settings
    if not settings.proxy.enabled:
        await websocket.close(code=1008)
        return
    if path == "v1" or path.startswith("v1/") or ".." in path.split("/"):
        await websocket.close(code=1008)
        return

    connect = _ws_connector()
    if connect is None:  # pragma: no cover - uvicorn[standard] ships websockets
        logger.warning("proxy: websockets package unavailable; cannot relay %s", path)
        await websocket.close(code=1011)
        return

    upstream = _upstream_url(settings, path, websocket.url.query, scheme_ws=True)
    pass_headers = {h.lower() for h in settings.proxy.pass_request_headers}
    fwd_headers = [
        (k, v)
        for k, v in websocket.headers.items()
        if k.lower() in pass_headers and k.lower() != _WS_SUBPROTOCOL_HEADER
    ]
    subprotocols = websocket.scope.get("subprotocols") or []

    try:
        upstream_ws = await connect(upstream, fwd_headers, subprotocols)
    except Exception as exc:  # noqa: BLE001 - any handshake failure is a 1011 to the client
        logger.info("proxy: websocket connect to %s failed: %s", upstream, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream_ws.subprotocol)

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if (data := message.get("text")) is not None:
                await upstream_ws.send(data)
            elif (raw := message.get("bytes")) is not None:
                await upstream_ws.send(raw)

    async def upstream_to_client() -> None:
        async for message in upstream_ws:
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        await upstream_ws.close()
        with contextlib.suppress(RuntimeError):
            await websocket.close()
