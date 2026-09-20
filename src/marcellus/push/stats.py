"""Process-wide counters for the push pipeline.

One module-level ``STATS`` instance; the MQTT thread and the event loop both
write to it, so a plain ``threading.Lock`` guards every mutation.  Read back
by ``GET /v1/stats`` and the status page.  Counters are monotonic since
process start; gauges are last-value (``gauge``) or high-water
(``gauge_max``).

Names in use (keep this list current — it is the contract between the
writers and the status page):

Counters
  relay.send.ok / .unregistered / .rejected / .failed
      one per *logical* send, by final outcome (rejected = 422/429)
  relay.send.<kind>.attempts      HTTP attempts by kind (push/liveactivity/…)
  relay.retry                     attempts beyond the first
  relay.breaker.open              times the breaker tripped
  relay.breaker.skipped           sends short-circuited while open
  mqtt.msg.events / .reviews      messages accepted onto the queue
  mqtt.dropped.overflow           non-terminal frames dropped, queue full
  mqtt.reconnect                  broker (re)connects
  mqtt.consumer.errors            handler exceptions swallowed by the consumer
  pipeline.sweep.ended            activities ended by the sweep
  db.locked.retry                 SQLITE_BUSY retries (see db.read_with_retry)

Gauges
  relay.breaker.state             0 closed / 1 open
  relay.breaker.open_until        epoch seconds, 0 when closed
  mqtt.queue.depth                current consumer queue length
  mqtt.queue.high_water           max depth seen (gauge_max)
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any


class PushStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._gauges: dict[str, float] = {}
        self.started_at = time.time()

    def incr(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] += n

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def gauge_max(self, name: str, value: float) -> None:
        with self._lock:
            cur = self._gauges.get(name)
            if cur is None or value > cur:
                self._gauges[name] = float(value)

    def get(self, name: str) -> float:
        """Counter or gauge value; 0 when unset.  Test helper."""
        with self._lock:
            if name in self._counters:
                return float(self._counters[name])
            return self._gauges.get(name, 0.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_s": round(time.time() - self.started_at, 1),
                "counters": dict(sorted(self._counters.items())),
                "gauges": dict(sorted(self._gauges.items())),
            }

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self.started_at = time.time()


STATS = PushStats()
