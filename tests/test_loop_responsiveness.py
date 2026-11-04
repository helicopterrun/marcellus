"""The scrub generation cycle must not stall the request loop, and the
retirement coverage check must match the brute-force reference."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import pytest

from marcellus.scrub.generator import _AgedCoverage, _span_covered_by_aged
from marcellus.server import run_in_worker_loop


def _rows(spans: list[tuple[float, float]]) -> list[dict[str, float]]:
    return [{"start_ts": s, "end_ts": e} for s, e in sorted(spans)]


@pytest.mark.parametrize("tol", [0.0, 1.0, 5.0])
@pytest.mark.parametrize("seed", range(40))
def test_aged_coverage_matches_bruteforce(seed: int, tol: float) -> None:
    rng = random.Random(seed)
    # Small integer grid -> plenty of exact adjacency/touching ends.
    spans = []
    for _ in range(rng.randint(0, 25)):
        s = rng.randint(0, 60)
        spans.append((float(s), float(s + rng.randint(0, 8))))
    rows = _rows(spans)
    cov = _AgedCoverage(rows, tolerance_s=tol)
    for _ in range(200):
        a = rng.randint(-3, 70)
        b = a + rng.randint(-2, 15)
        assert cov.covers(float(a), float(b)) == _span_covered_by_aged(
            rows, float(a), float(b), tol  # type: ignore[arg-type]
        ), (rows, a, b, tol)


def test_aged_coverage_perf() -> None:
    rng = random.Random(1)
    aged = [{"start_ts": i * 10.0, "end_ts": i * 10.0 + 9.0} for i in range(12_000)]  # gaps
    stale = []
    for _ in range(1500):
        s = rng.uniform(0, 119_000)
        stale.append({"start_ts": s, "end_ts": s + 50.0})
    t0 = time.perf_counter()
    cov = _AgedCoverage(aged)
    n = sum(cov.covers(r["start_ts"], r["end_ts"]) for r in stale)
    assert time.perf_counter() - t0 < 0.2
    assert n == 0


async def _max_lag(work: Any) -> float:
    lag = 0.0
    stop = False

    async def ticker() -> None:
        nonlocal lag
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            lag = max(lag, now - last - 0.05)
            last = now

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0.1)
    await work()
    stop = True
    await t
    return lag


async def test_blocking_cycle_does_not_stall_loop() -> None:
    async def blocking_cycle() -> str:
        time.sleep(1.5)  # synchronous, like the old Pass 3 retire
        return "done"

    async def work() -> None:
        assert await run_in_worker_loop(blocking_cycle()) == "done"

    assert await _max_lag(work) < 0.3


async def test_blocking_cycle_on_main_loop_would_stall() -> None:
    """Control: proves the harness detects the old direct-await behaviour."""

    async def blocking_cycle() -> None:
        time.sleep(1.0)

    assert await _max_lag(blocking_cycle) > 0.5


async def test_worker_loop_cancel_is_prompt_and_propagates() -> None:
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    cancelled_inside = []

    async def cycle() -> None:
        loop.call_soon_threadsafe(started.set)
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled_inside.append(True)
            raise

    task = asyncio.create_task(run_in_worker_loop(cycle()))
    await started.wait()
    t0 = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - t0 < 2
    assert cancelled_inside == [True]


async def test_worker_loop_propagates_exceptions() -> None:
    async def boom() -> None:
        raise ValueError("x")

    with pytest.raises(ValueError):
        await run_in_worker_loop(boom())
