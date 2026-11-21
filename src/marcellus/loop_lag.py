"""Event-loop lag monitor.

Everything the sidecar serves -- proxied recorded video included -- shares one
asyncio loop, so a synchronous call that holds it (a sqlite read in an
`async def`, say) delays every other request by as long as it runs. That
showed up only indirectly, as proxied requests waiting seconds for an
upstream that had answered in milliseconds. This measures it directly: a task
sleeps a fixed interval and records how late it woke up.

`max_lag_s()` (the worst wake-up over the last minute) is reported in
`/healthz` as `checks.loop_lag_max_ms` -- informational only, it never
changes the status or the 503 -- and a wake-up later than `warn_s` is logged,
at most once per `warn_every_s`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)


class LoopLagMonitor:
    def __init__(
        self,
        *,
        interval_s: float = 0.25,
        window_s: float = 60.0,
        warn_s: float = 0.25,
        warn_every_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval_s = interval_s
        self.window_s = window_s
        self.warn_s = warn_s
        self.warn_every_s = warn_every_s
        self._clock = clock
        self._samples: deque[tuple[float, float]] = deque()
        self._last_warn: float | None = None
        # Late wake-ups since the last warning (the one logged included).
        self._late_count = 0
        self._late_max = 0.0

    def record(self, now: float, lag_s: float) -> None:
        """Note a wake-up `lag_s` seconds late at monotonic time `now`."""
        self._samples.append((now, lag_s))
        cutoff = now - self.window_s
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if lag_s < self.warn_s:
            return
        self._late_count += 1
        self._late_max = max(self._late_max, lag_s)
        if self._last_warn is not None and now - self._last_warn < self.warn_every_s:
            return
        logger.warning(
            "event loop lag: %.0f ms (worst %.0f ms over %d late wake-up(s) since the last "
            "warning) -- something synchronous is holding the loop",
            lag_s * 1000,
            self._late_max * 1000,
            self._late_count,
        )
        self._last_warn = now
        self._late_count = 0
        self._late_max = 0.0

    def max_lag_s(self, now: float | None = None) -> float:
        """The worst lag recorded within the last `window_s`."""
        now = self._clock() if now is None else now
        cutoff = now - self.window_s
        return max((lag for t, lag in self._samples if t >= cutoff), default=0.0)

    async def run(self) -> None:
        while True:
            started = self._clock()
            await asyncio.sleep(self.interval_s)
            woke = self._clock()
            self.record(woke, max(0.0, woke - started - self.interval_s))
