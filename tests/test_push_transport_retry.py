"""RelayTransport retry policy + circuit breaker (wave 2A).

`STATS` is a process-wide singleton, so every test resets it first; `asyncio.
sleep` is patched to a no-op recorder so backoff delays are asserted, not
waited out.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from marcellus.push.models import Device
from marcellus.push.stats import STATS
from marcellus.push.transport import RelayTransport, compute_retry_delay


def _device(**kwargs: object) -> Device:
    defaults: dict[str, object] = dict(
        apns_token="tok1", device_id="d_abc", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox",
    )
    defaults.update(kwargs)
    return Device(**defaults)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_stats():
    STATS.reset()
    yield
    STATS.reset()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return delays


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# -- 1. liveactivity start/end and push retry on 5xx/timeout, up to N times --


async def test_liveactivity_start_retries_then_succeeds(no_sleep: list[float]) -> None:
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200)])
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=3, breaker_failures=10,
    )
    result = await relay.send_live_activity(
        _device(), token="t1", payload={"aps": {}}, collapse_id="c1", event="start",
    )
    assert result.ok is True
    assert len(calls) == 3
    assert STATS.get("relay.send.liveactivity.attempts") == 3
    assert STATS.get("relay.retry") == 2
    assert STATS.get("relay.send.ok") == 1
    assert len(no_sleep) == 2


# -- 2. push exhausts retries on repeated transport errors --------------------


async def test_push_exhausts_retries_on_timeout(
    no_sleep: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=3, breaker_failures=10,
    )
    # Construction itself warns once ("relay_key not set") -- only the send
    # below is under test.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="marcellus.push.transport"):
        result = await relay.send(
            _device(), handle="h1", server_id="s1", severity="alert", collapse_id="c1",
        )
    assert result.ok is False
    assert STATS.get("relay.send.push.attempts") == 3
    assert STATS.get("relay.send.failed") == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "send failed" in warnings[0].getMessage()


# -- 3. liveactivity update never retries -------------------------------------


async def test_liveactivity_update_never_retries(no_sleep: list[float]) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=3, breaker_failures=10,
    )
    result = await relay.send_live_activity(
        _device(), token="t1", payload={"aps": {}}, collapse_id="c1", event="update",
    )
    assert result.ok is False
    assert len(calls) == 1
    assert STATS.get("relay.send.liveactivity.attempts") == 1
    assert STATS.get("relay.retry") == 0
    assert no_sleep == []


# -- 4. 400/410/422 never retry, and all three are permanent (prune) --------


@pytest.mark.parametrize(
    # A non-JSON 422 body can't name a bad device_token, so it's the safe
    # default of NOT unregistered -- unlike 400/410, which are always dead
    # tokens. It still never retries (see assertions below).
    ("status", "expect_unregistered"), [(400, True), (410, True), (422, False)]
)
async def test_terminal_statuses_never_retry(
    no_sleep: list[float], status: int, expect_unregistered: bool
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text="nope")

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=3, breaker_failures=10,
    )
    result = await relay.send(
        _device(), handle="h1", server_id="s1", severity="alert", collapse_id="c1",
    )
    assert len(calls) == 1
    assert result.ok is False
    assert result.unregistered is expect_unregistered
    assert no_sleep == []


# -- 5. 429 Retry-After: used verbatim, and capped at 5s ----------------------


async def test_429_retry_after_used(no_sleep: list[float]) -> None:
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "2"}, text="slow"), httpx.Response(200)]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler), retry_attempts=3,
    )
    result = await relay.send(
        _device(), handle="h1", server_id="s1", severity="alert", collapse_id="c1",
    )
    assert result.ok is True
    assert no_sleep == [2.0]


async def test_429_retry_after_capped_at_5s(no_sleep: list[float]) -> None:
    responses = iter(
        [httpx.Response(429, headers={"Retry-After": "60"}, text="slow"), httpx.Response(200)]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler), retry_attempts=3,
    )
    result = await relay.send(
        _device(), handle="h1", server_id="s1", severity="alert", collapse_id="c1",
    )
    assert result.ok is True
    assert no_sleep == [5.0]


# -- 6. circuit breaker: opens, skips, half-opens, closes ---------------------


async def test_breaker_opens_skips_then_half_opens_and_closes(no_sleep: list[float]) -> None:
    clock = {"t": 0.0}
    responses = [
        httpx.Response(503, text="down"),  # 1: situation fails -> 1 consecutive failure
        httpx.Response(503, text="down"),  # 2: situation fails -> breaker opens
        httpx.Response(200),  # 3: half-open probe succeeds -> breaker closes
        httpx.Response(503, text="down"),  # 4: post-close push attempt 1
        httpx.Response(503, text="down"),  # 5: post-close push attempt 2
        httpx.Response(200),  # 6: post-close push attempt 3 -> ok
    ]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[len(calls) - 1]

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=1, breaker_failures=2, breaker_open_s=30.0,
        clock=lambda: clock["t"],
    )

    r1 = await relay.send_situation(_device(), payload={"aps": {}}, collapse_id="s1")
    assert r1.ok is False
    r2 = await relay.send_situation(_device(), payload={"aps": {}}, collapse_id="s2")
    assert r2.ok is False
    assert STATS.get("relay.breaker.open") == 1
    assert STATS.get("relay.breaker.state") == 1

    # Breaker open: third send makes zero new HTTP calls.
    calls_before = len(calls)
    r3 = await relay.send_situation(_device(), payload={"aps": {}}, collapse_id="s3")
    assert r3.ok is False
    assert "breaker open" in (r3.error or "")
    assert len(calls) == calls_before
    assert STATS.get("relay.breaker.skipped") == 1

    # Advance the clock past open_s: the next send is a single probe attempt,
    # regardless of kind (push normally retries relay_retry_attempts times).
    clock["t"] = 31.0
    relay._retry_attempts = 3  # so the *following* send can prove retry still works
    r4 = await relay.send(
        _device(), handle="h1", server_id="s1", severity="alert", collapse_id="probe",
    )
    assert r4.ok is True
    assert len(calls) == 3
    assert STATS.get("relay.breaker.state") == 0

    # Breaker closed: a subsequent failing send retries normally again.
    # (Raise the trip threshold so r5's own two failures don't re-open it
    # mid-send -- that case is covered by the test below.)
    relay._breaker_failures = 10
    r5 = await relay.send(
        _device(), handle="h2", server_id="s1", severity="alert", collapse_id="c5",
    )
    assert r5.ok is True
    assert len(calls) == 6


async def test_breaker_tripping_mid_send_stops_remaining_attempts(
    no_sleep: list[float],
) -> None:
    """A send whose own failures trip the breaker doesn't burn its remaining
    attempts on a relay it just declared down."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="down")

    relay = RelayTransport(
        "https://relay.example.test", client=_client(handler),
        retry_attempts=3, breaker_failures=2, breaker_open_s=30.0,
        clock=lambda: 0.0,
    )
    r = await relay.send(
        _device(), handle="h1", server_id="s1", severity="alert", collapse_id="c1",
    )
    assert r.ok is False
    assert len(calls) == 2
    assert STATS.get("relay.breaker.open") == 1
    assert STATS.get("relay.send.failed") == 1


# -- 7. compute_retry_delay jitter and Retry-After override -------------------


def test_compute_retry_delay_jitter_within_30_pct() -> None:
    for attempt, base in enumerate([0.5, 1.5, 4.5]):
        samples = [compute_retry_delay(attempt) for _ in range(200)]
        assert all(base * 0.7 - 1e-9 <= s <= base * 1.3 + 1e-9 for s in samples)


def test_compute_retry_delay_retry_after_overrides_and_caps() -> None:
    assert compute_retry_delay(0, retry_after=2.0) == 2.0
    assert compute_retry_delay(5, retry_after=60.0) == 5.0
