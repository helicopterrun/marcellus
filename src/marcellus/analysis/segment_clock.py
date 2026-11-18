"""Where inside its whole second each Frigate recording segment really began.

Frigate names a recording segment after the wall-clock second its file was
opened (ffmpeg's segment muxer with `-strftime 1`), and `recordings.start_time`
is parsed from that name, so it is always a whole second. The footage inside
starts some fraction `f` of a second later: `true start = start_time + f`, with
`f` in [0, 1). Everything that turns a timestamp into a frame -- Frigate's
`/recordings/<ts>/snapshot.jpg`, the scrub generator's cells, a client placing
time by summing segment durations -- seeks to `ts - start_time` inside the file
and so shows the moment `ts + f`. A detect-clock event therefore lands on the
picture with a shift of `latency - f`, not a constant: `f` walks across the
unit interval as the camera's clock drifts against the host's (on the
reference deployment, one sweep every few hours) and then wraps, a 0-1 s
sawtooth that no per-camera constant can remove.

`f` is never stored, but it is pinned by the segments around it. Consecutive
segments of one camera are contiguous in media time, so within a run
`f(k) = f(n) + S(k) - S(n)`, where `S` accumulates `duration - (next start -
start)` along the run. Every segment adds the constraint `0 <= f(k) < 1`; their
intersection over a window ending at segment `n` is
`[S(n) - min S, 1 + S(n) - max S]`, and the estimate is its midpoint. Whenever
the window holds a filename that rolled over to the next second (a step of 9 or
11 s between names) the interval collapses to the jitter of the file-open
time; on the reference deployment a 5 to 15 minute window pins `f` to within
~0.1 s on most cameras. A window that holds no such rollover leaves the
interval about 1 s wide, and the midpoint is then ~0.5: the correction
(`0.5 - f`, see `analysis.clock_offset`) fades to zero, i.e. to the old
constant-offset behaviour, exactly where the evidence runs out.

The estimate is causal -- a segment's `f` depends only on itself and earlier
segments of the same run -- so it never changes once computed. That keeps the
reel body (ETagged on content) stable, makes the live edge the same code path
as history, and lets the cache below grow incrementally as segments arrive.

Runs break where the name step disagrees with the previous duration by more
than `BREAK_TOLERANCE_S` (a real gap, a restart, a dropped segment); the first
segments after a break are unpinned until a rollover is seen again. A
`start_time` that is not a whole second (a Frigate build that stores precise
starts, or a hand-made fixture) disables the estimate for that segment: the
model does not apply, so nothing is corrected.

Everything is read from Frigate's `recordings` table, read-only.
"""

from __future__ import annotations

import bisect
import math
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

#: Causal windows tried in order, seconds of footage before the segment. The
#: first one whose interval is narrower than `PINNED_WIDTH_S` wins; the last
#: is used regardless. Longer windows pin more segments but accumulate the
#: rounding of every `duration` in them -- measured on the reference
#: deployment, accuracy falls off past ~2 h.
WINDOWS_S: tuple[float, ...] = (300.0, 600.0, 1200.0, 2400.0, 4800.0, 7200.0)
PINNED_WIDTH_S = 0.2
#: |name step - previous duration| above this breaks the run. A rollover is
#: ~1 s; anything larger is a gap or a restart.
BREAK_TOLERANCE_S = 1.5
#: A time this far past a segment's end still uses that segment's `f` (the
#: ~1 s holes a rollover leaves between nominal segment spans).
GAP_TOLERANCE_S = 2.0
#: A time past the newest known segment uses that segment's `f` for this long:
#: events at the live edge outrun the segment that will hold them by a few
#: seconds, and `f` moves by milliseconds per segment between wraps.
LIVE_EDGE_S = 60.0
#: Cache granularity and bound. 256 one-hour blocks (~360 segments each, three
#: floats per segment) is a few MB at most.
BLOCK_S = 3600.0
MAX_BLOCKS = 256
#: How often the live block re-reads the recordings table for new segments.
REFRESH_S = 5.0


def _whole_second(t: float) -> bool:
    return abs(t - round(t)) < 1e-6


class FractionEstimator:
    """Streaming, causal estimate of `f` for one camera's segments.

    Feed segments in ascending `start` order with `push`; each call returns
    `(f, width)` for that segment, or None when the model does not apply. Uses
    one monotonic min/max deque pair per window, so the cost per segment is
    amortised O(len(WINDOWS_S)).
    """

    __slots__ = ("_prev", "_s", "_mins", "_maxs")

    def __init__(self) -> None:
        self._prev: tuple[float, float] | None = None
        self._s = 0.0
        self._mins: list[deque[tuple[float, float]]] = [deque() for _ in WINDOWS_S]
        self._maxs: list[deque[tuple[float, float]]] = [deque() for _ in WINDOWS_S]

    def _reset(self) -> None:
        self._s = 0.0
        for d in self._mins:
            d.clear()
        for d in self._maxs:
            d.clear()

    def push(self, start: float, duration: float) -> tuple[float, float] | None:
        if not (_whole_second(start) and math.isfinite(duration) and duration > 0):
            self._prev = None
            self._reset()
            return None
        prev = self._prev
        if prev is None:
            self._reset()
        else:
            step = start - prev[0]
            residual = step - prev[1]
            if abs(residual) > BREAK_TOLERANCE_S:
                self._reset()
            else:
                self._s -= residual
        self._prev = (start, duration)
        s = self._s
        result: tuple[float, float] | None = None
        for w, (mins, maxs) in enumerate(zip(self._mins, self._maxs, strict=True)):
            while mins and mins[-1][1] >= s:
                mins.pop()
            mins.append((start, s))
            while maxs and maxs[-1][1] <= s:
                maxs.pop()
            maxs.append((start, s))
            horizon = start - WINDOWS_S[w]
            while mins[0][0] < horizon:
                mins.popleft()
            while maxs[0][0] < horizon:
                maxs.popleft()
            if result is None:
                lo = s - mins[0][1]
                hi = 1.0 + s - maxs[0][1]
                width = hi - lo
                if width <= PINNED_WIDTH_S or w == len(WINDOWS_S) - 1:
                    result = (round((lo + hi) / 2.0, 4), round(width, 4))
        return result


def estimate(segments: Iterable[tuple[float, float]]) -> list[tuple[float, float] | None]:
    """`(f, width)` per `(start, duration)` segment, in order (pure; for tests
    and offline analysis)."""
    est = FractionEstimator()
    return [est.push(s, d) for s, d in segments]


@dataclass
class _Block:
    starts: list[float] = field(default_factory=list)
    ends: list[float] = field(default_factory=list)
    fs: list[float | None] = field(default_factory=list)
    widths: list[float | None] = field(default_factory=list)
    complete: bool = False

    def add(self, start: float, duration: float, res: tuple[float, float] | None) -> None:
        self.starts.append(start)
        self.ends.append(start + duration)
        self.fs.append(res[0] if res else None)
        self.widths.append(res[1] if res else None)


@dataclass
class _Tail:
    """The live block's estimator state, so new segments extend it in place."""

    block: int
    estimator: FractionEstimator
    last_start: float
    checked_at: float


#: `rows(camera, since, until)` -> `(start, duration)` ascending, start in
#: [since, until) (`until` None = no upper bound).
_RowsFn = Callable[[str, float, float | None], list[tuple[float, float]]]
#: `newest(camera)` -> the newest segment's start, or None.
_NewestFn = Callable[[str], float | None]


class SegmentClock:
    """Bounded per-camera cache of `f`, keyed by one-hour blocks.

    A block whose end is behind the newest segment is immutable. The newest
    block is the camera's *tail*: it keeps its estimator, and a lookup at most
    every `REFRESH_S` reads only the rows newer than the last one it saw.
    """

    def __init__(
        self,
        rows: _RowsFn,
        newest: _NewestFn,
        *,
        max_blocks: int = MAX_BLOCKS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rows = rows
        self._newest = newest
        self._max_blocks = max_blocks
        self._clock = clock
        self._blocks: OrderedDict[tuple[str, int], _Block] = OrderedDict()
        self._tails: dict[str, _Tail] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._blocks)

    # -- block management (lock held) ---------------------------------------

    def _store(self, camera: str, idx: int, block: _Block) -> None:
        key = (camera, idx)
        self._blocks[key] = block
        self._blocks.move_to_end(key)
        while len(self._blocks) > self._max_blocks:
            (cam, old), _ = self._blocks.popitem(last=False)
            tail = self._tails.get(cam)
            if tail is not None and tail.block == old:
                del self._tails[cam]

    def _load(self, camera: str, idx: int) -> _Block:
        b0 = idx * BLOCK_S
        b1 = b0 + BLOCK_S
        newest = self._newest(camera)
        est = FractionEstimator()
        block = _Block(complete=newest is not None and newest >= b1)
        last: tuple[float, float] | None = None
        for start, duration in self._rows(camera, b0 - WINDOWS_S[-1] - 30.0, b1):
            res = est.push(start, duration)
            if start >= b0:
                block.add(start, duration, res)
            last = (start, duration)
        if not block.complete:
            # The newest block with data (or one past the newest segment):
            # it becomes the camera's tail and grows in place from here.
            self._tails[camera] = _Tail(
                block=idx,
                estimator=est,
                last_start=last[0] if last else -math.inf,
                checked_at=self._clock(),
            )
        self._store(camera, idx, block)
        return block

    def _refresh_tail(self, camera: str) -> None:
        tail = self._tails.get(camera)
        if tail is None or self._clock() - tail.checked_at < REFRESH_S:
            return
        tail.checked_at = self._clock()
        newest = self._newest(camera)
        if newest is None or newest <= tail.last_start:
            return
        block = self._blocks.get((camera, tail.block))
        if block is None or newest - tail.last_start > 2 * BLOCK_S:
            # Evicted, empty, or left idle for hours: a cold load of the
            # block that is wanted is cheaper than replaying the backlog.
            self._blocks.pop((camera, tail.block), None)
            del self._tails[camera]
            return
        for start, duration in self._rows(camera, tail.last_start + 0.5, None):
            idx = int(start // BLOCK_S)
            if idx != tail.block:
                block.complete = True
                block = _Block()
                tail.block = idx
                self._store(camera, idx, block)
                if self._tails.get(camera) is not tail:
                    return  # evicted by its own insertion (tiny max_blocks)
            block.add(start, duration, tail.estimator.push(start, duration))
            tail.last_start = start

    def _block(self, camera: str, idx: int) -> _Block:
        tail = self._tails.get(camera)
        if tail is not None and idx >= tail.block:
            self._refresh_tail(camera)
            tail = self._tails.get(camera)
            if tail is not None and idx > tail.block:
                # Past the newest segment: there is nothing to read yet.
                return _Block()
        block = self._blocks.get((camera, idx))
        if block is None:
            block = self._load(camera, idx)
        else:
            self._blocks.move_to_end((camera, idx))
        return block

    # -- lookups ------------------------------------------------------------

    def _segment_at(self, camera: str, t: float) -> tuple[_Block, int, bool] | None:
        """The segment `t` falls in (or just after), and whether it is the
        newest one known for the camera."""
        idx = int(t // BLOCK_S)
        block = self._block(camera, idx)
        i = bisect.bisect_right(block.starts, t) - 1
        if i < 0:
            block = self._block(camera, idx - 1)
            i = len(block.starts) - 1
            if i < 0:
                return None
            newest = not block.complete
            nxt = self._blocks.get((camera, idx))
            if nxt is not None and nxt.starts:
                newest = False
            return block, i, newest
        newest = i == len(block.starts) - 1 and not block.complete
        return block, i, newest

    def fraction(self, camera: str, t: float) -> float | None:
        """Estimated `f` of the segment holding record-clock time `t`, or
        None when no segment can vouch for it (gap, no rows, model off)."""
        with self._lock:
            hit = self._segment_at(camera, t)
            if hit is None:
                return None
            block, i, newest = hit
            past_end = t - block.ends[i]
            if past_end > GAP_TOLERANCE_S and not (newest and past_end <= LIVE_EDGE_S):
                return None
            return block.fs[i]

    def segments(self, camera: str, start: float, end: float) -> list[dict[str, Any]]:
        """Diagnostics: every cached segment starting in [start, end)."""
        out: list[dict[str, Any]] = []
        with self._lock:
            for idx in range(int(start // BLOCK_S), int(end // BLOCK_S) + 1):
                block = self._block(camera, idx)
                cols = zip(block.starts, block.ends, block.fs, block.widths, strict=True)
                for s, e, f, w in cols:
                    if start <= s < end:
                        out.append(
                            {"start": s, "duration": round(e - s, 4), "f": f, "width": w}
                        )
        return out


# -- process-wide instances, one per Frigate DB --------------------------------

_clocks: dict[str, SegmentClock] = {}
_clocks_lock = threading.Lock()


def _db_source(db_path: str) -> tuple[_RowsFn, _NewestFn]:
    from marcellus import db

    def _query(sql: str, params: list[Any]) -> list[Any]:
        try:
            conn = db.open_frigate_ro(db_path)
        except Exception:  # noqa: BLE001 -- no DB means no correction, never a 500
            return []
        try:
            return list(conn.execute(sql, params))
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def rows(camera: str, since: float, until: float | None) -> list[tuple[float, float]]:
        sql = "SELECT start_time, duration FROM recordings WHERE camera = ? AND start_time >= ?"
        params: list[Any] = [camera, since]
        if until is not None:
            sql += " AND start_time < ?"
            params.append(until)
        out = []
        for r in _query(sql + " ORDER BY start_time", params):
            try:
                out.append((float(r[0]), float(r[1])))
            except (TypeError, ValueError):
                continue
        return out

    def newest(camera: str) -> float | None:
        got = _query("SELECT MAX(start_time) FROM recordings WHERE camera = ?", [camera])
        try:
            return float(got[0][0]) if got and got[0][0] is not None else None
        except (TypeError, ValueError):
            return None

    return rows, newest


def clock_for(db_path: str) -> SegmentClock:
    with _clocks_lock:
        clock = _clocks.get(db_path)
        if clock is None:
            rows, newest = _db_source(db_path)
            clock = SegmentClock(rows, newest)
            _clocks[db_path] = clock
        return clock


def reset() -> None:
    """Drop every cached estimate (tests; a Frigate DB swap)."""
    with _clocks_lock:
        _clocks.clear()
