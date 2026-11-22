"""Absolute motion series for `/v1/motion` (docs/scrub-cache-and-proxy-spec.md §4.6).

Frigate's `/api/review/activity/motion` normalises every answer to the request
window's own peak, so its 0-100 `values` are not comparable between windows:
one stir on a quiet camera reads as loud as a busy road. This module reads
the raw per-segment `recordings.motion` counter from `frigate.db` instead and
turns it into a rate a client can compare across windows of one camera:

* `rate[i]`: motion of the recording segments overlapping bucket `i`, each
  segment's motion apportioned by the fraction of the segment inside the
  bucket, divided by the recorded seconds overlapping the bucket. The unit is
  Frigate's own motion counter per recorded second
  (`RATE_UNIT`): what the counter measures is Frigate's business, and it
  depends on the camera's resolution and motion settings, so rates are
  comparable within one camera only.
* `recorded[i]`: the fraction of the bucket's in-window seconds covered by at
  least one recording segment (overlapping segments are merged), so a client
  can tell "no movement" (`rate` 0, `recorded` 1) from "not recording"
  (`rate` 0, `recorded` 0).
* the reference: the 95th percentile of the 5-minute rate over the trailing
  7 days, ignoring buckets that were less than half recorded -- the camera's
  "normally busy" level, a scale a client can draw `rate` against.

Row handling (checked against a production database, 2026-11-22: no NULL
`motion` or `duration`, `end_time - start_time == duration` on every row,
segments up to ~280 s, ~2% of segments overlapping a neighbour):

* `motion` NULL (nullable in both schemas) or negative counts as 0, as a SQL
  `SUM` over it would; the segment's seconds still count as recorded.
* `duration` NULL, zero, negative or NaN: the segment is skipped -- it covers
  no time, so it has no seconds to apportion its motion over.
* the segment's span is `[start_time, start_time + duration)`. Frigate writes
  `end_time` as exactly that, and reading `duration` instead of `end_time`
  lets the query run off the covering index `recordings_api_recordings_summary`
  (camera, start_time, duration, motion) present in Frigate 0.17 and 0.18.
* overlapping segments both count toward `rate` (each file's counter covers
  its own frames, so the overlap's motion and seconds are both counted twice
  and the ratio holds) but only once toward `recorded`.

Pure except `read_segments`, which takes an open connection; the route owns
threads, deadlines and caching (`RateReferenceCache` is the per-camera cache).
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

RATE_UNIT = "motion_per_recorded_second"

#: How far before a window's start a segment may begin and still reach into
#: it. Frigate's segments are ~10 s (the longest seen in production was
#: ~280 s); bounding `start_time` from below keeps the read an index range
#: instead of a scan of the camera's whole history.
SEGMENT_LOOKBACK_S = 3600.0

REFERENCE_WINDOW_S = 7 * 86400.0
REFERENCE_SCALE_S = 300.0
REFERENCE_PERCENTILE = 0.95
#: A 5-minute bucket counts toward the reference only when at least this much
#: of it was recorded (a half-recorded bucket's rate rests on few seconds).
REFERENCE_MIN_RECORDED = 0.5
#: Fewer qualifying buckets than this (one hour's worth) and there is no
#: reference: a percentile over a handful of buckets is noise.
REFERENCE_MIN_BUCKETS = 12
REFERENCE_TTL_S = 3600.0
#: A reference older than its TTL is still served (while a fresh one is
#: computed) for up to this long: a 7-day statistic barely moves in a day.
REFERENCE_STALE_TTL_S = 86400.0
#: After a failed computation, wait this long before trying again.
REFERENCE_RETRY_S = 60.0

_RATE_DIGITS = 4
_RECORDED_DIGITS = 3

#: (start, end, motion) of one recording segment, `end > start`.
Segment = tuple[float, float, float]


def bucket_count(start: float, end: float, scale: float) -> int:
    """Same grid length as `aggregate_motion`, so `rate`/`recorded` line up
    with `values` index for index."""
    return max(0, int((end - start) / scale + 0.9999999))


def read_segments(conn: sqlite3.Connection, camera: str, start: float, end: float) -> list[Segment]:
    """Recording segments of `camera` overlapping `[start, end)`, oldest first.

    One index range read (see the module docstring for the row rules)."""
    rows = conn.execute(
        "SELECT start_time, duration, motion FROM recordings "
        "WHERE camera = ? AND start_time >= ? AND start_time < ? "
        "ORDER BY start_time",
        (camera, start - SEGMENT_LOOKBACK_S, end),
    ).fetchall()
    segments: list[Segment] = []
    for row in rows:
        seg_start, duration, motion = row[0], row[1], row[2]
        if seg_start is None or duration is None:
            continue
        seg_start = float(seg_start)
        duration = float(duration)
        if not duration > 0 or math.isinf(duration):  # also rejects NaN
            continue
        seg_end = seg_start + duration
        if seg_end <= start:
            continue
        value = float(motion) if motion is not None else 0.0
        segments.append((seg_start, seg_end, value if value > 0 else 0.0))
    return segments


def interrupt_after(conn: sqlite3.Connection, seconds: float) -> None:
    """Abort any statement on `conn` still running `seconds` from now
    (`sqlite3.OperationalError: interrupted`). A read on a worker thread can't
    be cancelled from the loop; this bounds how long it can hold its slot."""
    give_up = time.monotonic() + seconds
    conn.set_progress_handler(lambda: time.monotonic() > give_up, 10_000)


def apportion(
    segments: list[Segment], start: float, end: float, scale: float
) -> tuple[list[float], list[float]]:
    """(`rate`, `recorded`) on the `scale`-second grid covering `[start, end)`.

    `segments` must be sorted by start (as `read_segments` returns them). The
    last bucket's `recorded` is relative to its in-window width, so a window
    that ends mid-bucket can still read fully recorded.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    n = bucket_count(start, end, scale)
    motion = [0.0] * n
    seconds = [0.0] * n
    covered = [0.0] * n
    covered_until = start
    for seg_start, seg_end, seg_motion in segments:
        length = seg_end - seg_start
        if length <= 0:
            continue
        a = max(seg_start, start)
        b = min(seg_end, end)
        if b <= a:
            continue
        density = seg_motion / length
        # Sorted by start, so a sweep merges overlaps for coverage: only
        # [c, b) is newly covered.
        c = max(a, covered_until)
        if b > covered_until:
            covered_until = b
        i = int((a - start) / scale)
        while i < n:
            lo = start + i * scale
            if lo >= b:
                break
            piece_lo = a if a > lo else lo
            hi = lo + scale
            piece_hi = b if b < hi else hi
            piece = piece_hi - piece_lo
            if piece > 0:
                seconds[i] += piece
                motion[i] += piece * density
                fresh = piece_hi - (c if c > piece_lo else piece_lo)
                if fresh > 0:
                    covered[i] += fresh
            i += 1
    rate: list[float] = []
    recorded: list[float] = []
    for i in range(n):
        rate.append(round(motion[i] / seconds[i], _RATE_DIGITS) if seconds[i] > 0 else 0.0)
        width = min(start + (i + 1) * scale, end) - (start + i * scale)
        frac = covered[i] / width if width > 0 else 0.0
        recorded.append(round(min(1.0, max(0.0, frac)), _RECORDED_DIGITS))
    return rate, recorded


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (`numpy.percentile`'s default method)."""
    if not values:
        raise ValueError("no values")
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def reference_from_series(rate: list[float], recorded: list[float]) -> float | None:
    """The 95th percentile of `rate` over buckets at least half recorded, or
    None when fewer than `REFERENCE_MIN_BUCKETS` qualify."""
    qualified = [r for r, c in zip(rate, recorded, strict=True) if c >= REFERENCE_MIN_RECORDED]
    if len(qualified) < REFERENCE_MIN_BUCKETS:
        return None
    return round(percentile(qualified, REFERENCE_PERCENTILE), _RATE_DIGITS)


def reference_window(now: float) -> tuple[float, float]:
    """The trailing 7 days, on a 5-minute grid aligned to the epoch so every
    computation in the same 5 minutes reads the same buckets."""
    end = math.floor(now / REFERENCE_SCALE_S) * REFERENCE_SCALE_S
    return end - REFERENCE_WINDOW_S, end


def compute_reference(conn: sqlite3.Connection, camera: str, now: float) -> float | None:
    start, end = reference_window(now)
    rate, recorded = apportion(read_segments(conn, camera, start, end), start, end,
                               REFERENCE_SCALE_S)
    return reference_from_series(rate, recorded)


class RateReferenceCache:
    """Per-camera reference values: in memory, single-flight, deadline-bound.

    * A value (None included: "too little recorded") is fresh for `ttl_s`.
    * At most one computation per camera runs at a time; callers arriving
      while it runs wait on the same task.
    * A caller waits at most `deadline_s`. A computation that misses it is
      not cancelled: it finishes in the background and is cached for the next
      call. Meanwhile the caller gets the previous value if it is no older
      than `stale_ttl_s`, else None.
    * A failed computation is retried no sooner than `retry_s` later.

    Event-loop affinity as `MotionFlight`: a task left over from another loop
    is ignored. Loop-only (no locking).
    """

    def __init__(
        self,
        *,
        ttl_s: float = REFERENCE_TTL_S,
        stale_ttl_s: float = REFERENCE_STALE_TTL_S,
        retry_s: float = REFERENCE_RETRY_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_s = ttl_s
        self.stale_ttl_s = stale_ttl_s
        self.retry_s = retry_s
        self._clock = clock
        self._values: dict[str, tuple[float, float | None]] = {}
        self._failed_at: dict[str, float] = {}
        self._inflight: dict[str, asyncio.Task[float | None]] = {}

    def inflight_count(self) -> int:
        return len(self._inflight)

    def _stale(self, camera: str, now: float) -> float | None:
        entry = self._values.get(camera)
        if entry is not None and now - entry[0] <= self.stale_ttl_s:
            return entry[1]
        return None

    def _task_for(
        self, camera: str, compute: Callable[[], Awaitable[float | None]]
    ) -> asyncio.Task[float | None] | None:
        loop = asyncio.get_running_loop()
        task = self._inflight.get(camera)
        if task is not None and not task.done() and task.get_loop() is loop:
            return task
        failed_at = self._failed_at.get(camera)
        if failed_at is not None and self._clock() - failed_at < self.retry_s:
            return None

        async def _run() -> float | None:
            return await compute()

        task = loop.create_task(_run())
        self._inflight[camera] = task

        def _done(t: asyncio.Task[float | None]) -> None:
            if self._inflight.get(camera) is t:
                del self._inflight[camera]
            if t.cancelled():
                return
            exc = t.exception()  # also marks it retrieved
            if exc is not None:
                self._failed_at[camera] = self._clock()
                logger.warning("motion: rate reference for %s failed: %r", camera, exc)
                return
            self._failed_at.pop(camera, None)
            self._values[camera] = (self._clock(), t.result())

        task.add_done_callback(_done)
        return task

    async def get(
        self,
        camera: str,
        compute: Callable[[], Awaitable[float | None]],
        *,
        deadline_s: float,
    ) -> float | None:
        now = self._clock()
        entry = self._values.get(camera)
        if entry is not None and now - entry[0] <= self.ttl_s:
            return entry[1]
        task = self._task_for(camera, compute)
        if task is None:
            return self._stale(camera, now)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, deadline_s))
        except asyncio.TimeoutError:
            return self._stale(camera, now)
        except Exception:  # noqa: BLE001 -- logged in _done; the reference is optional
            return self._stale(camera, now)
