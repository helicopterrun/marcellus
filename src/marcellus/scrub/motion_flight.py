"""Deadline-bounded, single-flight motion fetches for `/v1/reel`.

`/v1/reel` is the first thing a client needs before it can draw a still, and
motion is its only part that waits on Frigate's HTTP API. Under load (Frigate
CPU-bound on detection, measured 2026-10-25 at ~2 s per answer during an
arrival burst) that one upstream call held the whole reel response hostage.
This module lets the reel give up on motion after a short deadline and answer
with what the sidecar has locally (`motion_unavailable: true`), while the slow
fetch keeps running in the background and lands in a small cache, so the next
request for the same window is complete.

Invariants:

* At most one in-flight fetch per key (camera, window, scale). A request that
  arrives while one is running awaits the same task instead of starting
  another, so a burst of polls can't fan out into a burst of upstream calls.
* Every in-flight task races a hard ceiling (`hard_timeout_s`) -- its
  presence in `_inflight` gates starting a new fetch, so an unbounded await
  there would be a permanent "never fetch this window again".
* The cache is a fallback only: a fetch that answers inside the deadline is
  returned as-is (the fast path is unchanged). The cache serves only a
  request whose own fetch missed the deadline or failed.
* Bounded memory: an LRU of `max_entries` aggregated series.

Event-loop affinity: a task belongs to the loop that created it. A task left
over from another loop (TestClient without a context manager runs each
request on its own loop) is ignored rather than awaited.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable

logger = logging.getLogger(__name__)

MotionResult = tuple[list[float], bool]  # (values, unavailable)

#: Ceiling on one background fetch. The upstream call itself carries a 10 s
#: httpx timeout per phase; this bounds the whole thing, phases included.
DEFAULT_HARD_TIMEOUT_S = 15.0

#: A window whose end is at least this far behind the fetch is settled: its
#: motion will not change, so a cached copy stays good for a long time.
_SETTLED_MARGIN_S = 60.0
_SETTLED_TTL_S = 600.0
#: A window touching the live edge keeps growing; a cached copy is only a
#: short-lived stand-in for a fetch that missed its deadline.
_LIVE_TTL_S = 30.0

_DEFAULT_MAX_ENTRIES = 256


class MotionFlight:
    """Per-app coordinator (one instance on `app.state`)."""

    def __init__(
        self,
        *,
        hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.hard_timeout_s = hard_timeout_s
        self.max_entries = max_entries
        self._clock = clock
        self._inflight: dict[Hashable, asyncio.Task[MotionResult]] = {}
        # key -> (fetched_at, window_end, values)
        self._cache: OrderedDict[Hashable, tuple[float, float, list[float]]] = OrderedDict()

    # -- cache ---------------------------------------------------------------

    def cached(self, key: Hashable) -> list[float] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        fetched_at, window_end, values = entry
        settled = window_end <= fetched_at - _SETTLED_MARGIN_S
        ttl = _SETTLED_TTL_S if settled else _LIVE_TTL_S
        if self._clock() - fetched_at > ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return values

    def _store(self, key: Hashable, window_end: float, values: list[float]) -> None:
        self._cache[key] = (self._clock(), window_end, values)
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)

    def inflight_count(self) -> int:
        return len(self._inflight)

    # -- fetch ---------------------------------------------------------------

    def _task_for(
        self,
        key: Hashable,
        window_end: float,
        fetch: Callable[[], Awaitable[MotionResult]],
    ) -> asyncio.Task[MotionResult]:
        loop = asyncio.get_running_loop()
        task = self._inflight.get(key)
        if task is not None and not task.done() and task.get_loop() is loop:
            return task

        async def _bounded() -> MotionResult:
            return await asyncio.wait_for(fetch(), timeout=self.hard_timeout_s)

        task = loop.create_task(_bounded())
        self._inflight[key] = task

        def _done(t: asyncio.Task[MotionResult]) -> None:
            if self._inflight.get(key) is t:
                del self._inflight[key]
            if t.cancelled():
                return
            exc = t.exception()  # also marks it retrieved -- no "never retrieved" noise
            if exc is not None:
                return
            values, unavailable = t.result()
            if not unavailable:
                self._store(key, window_end, values)

        task.add_done_callback(_done)
        return task

    async def get(
        self,
        key: Hashable,
        window_end: float,
        fetch: Callable[[], Awaitable[MotionResult]],
        *,
        deadline_s: float,
        zero_fill: Callable[[], list[float]],
    ) -> MotionResult:
        """Motion for `key`, answering within `deadline_s`.

        Returns the live fetch's result when it lands in time; otherwise the
        cached series for the key (complete, so `unavailable=False`) if one is
        fresh; otherwise `zero_fill()` with `unavailable=True`. A fetch that
        misses the deadline is not cancelled -- it finishes in the background
        and its result is cached for the next request.
        """
        task = self._task_for(key, window_end, fetch)
        started = time.monotonic()
        try:
            values, unavailable = await asyncio.wait_for(
                asyncio.shield(task), timeout=deadline_s
            )
        except asyncio.TimeoutError:
            logger.info(
                "reel: motion fetch missed its %.2fs deadline key=%s; "
                "answering without it (fetch continues in background)",
                deadline_s, key,
            )
            values, unavailable = [], True
        except Exception as exc:  # noqa: BLE001 -- motion is best-effort, never a 500
            logger.warning(
                "reel: motion fetch failed key=%s secs=%.2f: %r",
                key, time.monotonic() - started, exc,
            )
            values, unavailable = [], True

        if not unavailable:
            return values, False
        cached = self.cached(key)
        if cached is not None:
            return cached, False
        return zero_fill(), True
