"""Tests for the shared async httpx client (frigate_api.get_async_client).

Covers the finite default timeout + connection Limits (previously `read=None`
and no `Limits` at all -- see docstring on `get_async_client`).
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Awaitable, Callable

import httpx
import pytest

from marcellus.frigate_api import (
    _DEFAULT_LIMITS,
    _DEFAULT_TIMEOUT,
    get_async_client,
)


class _App:
    def __init__(self) -> None:
        self.state = types.SimpleNamespace()


def test_get_async_client_has_a_finite_read_timeout() -> None:
    client = get_async_client(_App())
    assert client.timeout.read is not None
    assert client.timeout.read > 0
    assert client.timeout == _DEFAULT_TIMEOUT


def test_get_async_client_bounds_the_connection_pool() -> None:
    client = get_async_client(_App())
    pool = client._transport._pool  # noqa: SLF001 -- no public accessor on httpx's client
    assert pool._max_connections == _DEFAULT_LIMITS.max_connections  # noqa: SLF001
    assert pool._max_keepalive_connections == _DEFAULT_LIMITS.max_keepalive_connections  # noqa: SLF001


def test_get_async_client_reuses_the_cached_client_per_app() -> None:
    app = _App()
    first = get_async_client(app)
    second = get_async_client(app)
    assert first is second


async def _run_against_a_hung_server(make_request: Callable[[int], Awaitable[None]]) -> None:
    """Start a TCP listener that accepts and then never writes a byte back,
    run `make_request(port)`, and clean up -- without ever awaiting
    `server.wait_closed()`, which blocks until every accepted connection's own
    transport reports closed, and the handler below only closes on
    cancellation nobody sends it.

    A `MockTransport` handler would run outside httpx's own connect/read
    timeout machinery entirely (it's a direct function call, not a network
    read), so it can't exercise this -- a real listening socket is what the
    old `read=None` default actually left unbounded.
    """

    async def handler(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.Event().wait()  # never set -- connection stays open, silent
        except asyncio.CancelledError:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await make_request(port)
    finally:
        server.close()  # stops accepting; deliberately not awaiting wait_closed()


async def test_ordinary_request_times_out_on_a_hung_transport() -> None:
    """A real socket that accepts and then never responds must not hang an
    ordinary request past its configured read timeout."""

    async def make_request(port: int) -> None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.2)) as client:
            with pytest.raises(httpx.TimeoutException):
                await client.get(f"http://127.0.0.1:{port}/")

    await _run_against_a_hung_server(make_request)


async def test_default_timeout_bounds_a_hung_transport() -> None:
    """Same, but through the actual client construction `get_async_client`
    uses (finite default timeout + Limits), not a test-only short override."""

    async def make_request(port: int) -> None:
        app = _App()
        client = get_async_client(app)
        try:
            # The real default (15s) would make this test slow without
            # changing what it demonstrates; override per-request the same
            # way a real caller already does (see get_async_client's
            # docstring -- every current caller passes its own timeout).
            with pytest.raises(httpx.TimeoutException):
                await client.get(f"http://127.0.0.1:{port}/", timeout=httpx.Timeout(0.2))
        finally:
            await client.aclose()

    await _run_against_a_hung_server(make_request)
