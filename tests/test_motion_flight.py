"""Unit tests for `scrub.motion_flight.MotionFlight` -- the deadline-bounded,
single-flight motion fetch `/v1/reel` uses so a slow Frigate can't hold the
reel response hostage."""

from __future__ import annotations

import asyncio

from marcellus.scrub.motion_flight import MotionFlight

KEY = ("doorbell", 1000.0, 4600.0, 10.0)
WINDOW_END = 4600.0  # far in the past relative to the fake clock -> "settled"


def _zero() -> list[float]:
    return [0.0] * 3


async def _drain(flight: MotionFlight) -> None:
    """Wait for every background fetch to finish (each is hard-bounded)."""
    for _ in range(200):
        if flight.inflight_count() == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background fetch never finished")


async def test_fast_fetch_passes_through_unchanged() -> None:
    flight = MotionFlight()

    async def fetch() -> tuple[list[float], bool]:
        return [1.0, 2.0, 3.0], False

    got = await flight.get(KEY, WINDOW_END, fetch, deadline_s=1.0, zero_fill=_zero)
    assert got == ([1.0, 2.0, 3.0], False)


async def test_slow_fetch_answers_within_deadline_flagged_unavailable() -> None:
    flight = MotionFlight()
    release = asyncio.Event()

    async def fetch() -> tuple[list[float], bool]:
        await release.wait()
        return [5.0, 5.0, 5.0], False

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await flight.get(KEY, WINDOW_END, fetch, deadline_s=0.05, zero_fill=_zero)
    assert loop.time() - t0 < 0.5
    assert got == ([0.0, 0.0, 0.0], True)
    # The fetch was not cancelled -- it is still running in the background.
    assert flight.inflight_count() == 1
    release.set()
    await _drain(flight)


async def test_late_result_lands_in_cache_and_serves_next_miss() -> None:
    flight = MotionFlight()
    release = asyncio.Event()

    async def slow() -> tuple[list[float], bool]:
        await release.wait()
        return [4.0, 5.0, 6.0], False

    first = await flight.get(KEY, WINDOW_END, slow, deadline_s=0.02, zero_fill=_zero)
    assert first[1] is True
    release.set()
    await _drain(flight)
    assert flight.cached(KEY) == [4.0, 5.0, 6.0]

    # Next request: Frigate is still slow, but the window is now complete.
    async def hanging() -> tuple[list[float], bool]:
        await asyncio.sleep(10)
        return [], False

    second = await flight.get(KEY, WINDOW_END, hanging, deadline_s=0.02, zero_fill=_zero)
    assert second == ([4.0, 5.0, 6.0], False)
    # Clean up the hanging fetch rather than waiting out its sleep.
    for task in list(flight._inflight.values()):
        task.cancel()
    await _drain(flight)


async def test_single_inflight_fetch_per_key() -> None:
    flight = MotionFlight()
    calls = 0
    release = asyncio.Event()

    async def fetch() -> tuple[list[float], bool]:
        nonlocal calls
        calls += 1
        await release.wait()
        return [1.0, 1.0, 1.0], False

    results = await asyncio.gather(
        *(
            flight.get(KEY, WINDOW_END, fetch, deadline_s=0.05, zero_fill=_zero)
            for _ in range(5)
        )
    )
    assert all(r == ([0.0, 0.0, 0.0], True) for r in results)
    assert calls == 1
    assert flight.inflight_count() == 1

    # A request while the first fetch is still running joins it too.
    late_joiner = asyncio.create_task(
        flight.get(KEY, WINDOW_END, fetch, deadline_s=1.0, zero_fill=_zero)
    )
    await asyncio.sleep(0.01)
    release.set()
    assert await late_joiner == ([1.0, 1.0, 1.0], False)
    assert calls == 1
    await _drain(flight)


async def test_distinct_keys_fetch_independently() -> None:
    flight = MotionFlight()
    seen: list[str] = []

    def make(name: str):  # type: ignore[no-untyped-def]
        async def fetch() -> tuple[list[float], bool]:
            seen.append(name)
            await asyncio.sleep(0.05)
            return [1.0, 1.0, 1.0], False

        return fetch

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(
        *(
            flight.get(("cam", float(i), float(i + 1), 10.0), WINDOW_END, make(str(i)),
                       deadline_s=1.0, zero_fill=_zero)
            for i in range(5)
        )
    )
    assert sorted(seen) == ["0", "1", "2", "3", "4"]
    assert loop.time() - t0 < 0.2  # concurrent, not 5 x 0.05 serialized


async def test_hard_timeout_clears_inflight_and_does_not_cache() -> None:
    flight = MotionFlight(hard_timeout_s=0.05)

    async def hanging() -> tuple[list[float], bool]:
        await asyncio.sleep(10)
        return [9.0, 9.0, 9.0], False

    got = await flight.get(KEY, WINDOW_END, hanging, deadline_s=0.01, zero_fill=_zero)
    assert got == ([0.0, 0.0, 0.0], True)
    await _drain(flight)
    assert flight.inflight_count() == 0
    assert flight.cached(KEY) is None


async def test_errors_and_unavailable_results_are_not_cached() -> None:
    flight = MotionFlight()

    async def raising() -> tuple[list[float], bool]:
        raise RuntimeError("boom")

    got = await flight.get(KEY, WINDOW_END, raising, deadline_s=1.0, zero_fill=_zero)
    assert got == ([0.0, 0.0, 0.0], True)

    async def unavailable() -> tuple[list[float], bool]:
        return [0.0, 0.0, 0.0], True

    got = await flight.get(KEY, WINDOW_END, unavailable, deadline_s=1.0, zero_fill=_zero)
    assert got == ([0.0, 0.0, 0.0], True)
    await _drain(flight)
    assert flight.cached(KEY) is None


async def test_failure_falls_back_to_a_fresh_cached_series() -> None:
    flight = MotionFlight()

    async def good() -> tuple[list[float], bool]:
        return [2.0, 2.0, 2.0], False

    await flight.get(KEY, WINDOW_END, good, deadline_s=1.0, zero_fill=_zero)

    async def unavailable() -> tuple[list[float], bool]:
        return [0.0, 0.0, 0.0], True

    got = await flight.get(KEY, WINDOW_END, unavailable, deadline_s=1.0, zero_fill=_zero)
    assert got == ([2.0, 2.0, 2.0], False)


def test_cache_ttl_live_vs_settled_and_lru_bound() -> None:
    now = [10_000.0]
    flight = MotionFlight(clock=lambda: now[0], max_entries=2)
    flight._store("settled", 10_000.0 - 3600, [1.0])
    flight._store("live", 10_000.0, [2.0])

    now[0] += 31  # past the live TTL, well inside the settled one
    assert flight.cached("live") is None
    assert flight.cached("settled") == [1.0]

    now[0] += 600  # past the settled TTL too
    assert flight.cached("settled") is None

    flight._store("a", 0.0, [1.0])
    flight._store("b", 0.0, [2.0])
    flight._store("c", 0.0, [3.0])
    assert flight.cached("a") is None  # evicted, least recently used
    assert flight.cached("c") == [3.0]
