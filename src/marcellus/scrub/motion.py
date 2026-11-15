"""Motion totalization (docs/scrub-cache-and-proxy-spec.md §4.6).

Frigate's `/api/review/activity/motion` is fine per-call but has two measured
cliffs: `scale=3600` returns all zeros over multi-day windows, and short
windows return short answers. The fix is server-side: fetch at a safe scale
(<=300) and aggregate/zero-fill to whatever grid the caller actually asked
for, so `/v1/motion` always covers the full requested `[start, end)`.

Kept pure (no httpx here) so it's testable against synthetic points.
"""

from __future__ import annotations

MAX_SAFE_SCALE = 300.0


def safe_fetch_scale(requested_scale: float) -> int:
    """Never ask Frigate for a scale wider than the safe ceiling -- widen our
    own aggregation instead of trusting Frigate's degraded wide-scale answer.

    Frigate's `scale` is an `int` (a fractional value is a 422), so the result
    is rounded to a whole number of seconds, minimum 1."""
    return max(1, round(min(requested_scale, MAX_SAFE_SCALE)))


def aggregate_motion(
    points: list[tuple[float, float]],
    start: float,
    end: float,
    scale: float,
    fetch_scale: float | None = None,
) -> list[float]:
    """Bucket raw (timestamp, motion) points onto a `scale`-second grid
    covering the full `[start, end)`, zero-filled wherever there is no data.

    `value[i]` covers `[start + i*scale, start + (i+1)*scale)` (spec §4.5).
    Multiple points landing in the same bucket are averaged (matches
    Frigate's own "normalised 0-100" semantics -- summing would blow past
    100 and misrepresent a single continuous burst as more motion than it
    scored).

    `fetch_scale` is the bucket width Frigate was asked for. When it is finer
    than `scale`, a fetch bucket with no point counts as zero motion: Frigate
    0.17 returned those gap rows and 0.18 drops them, so filling them here
    makes a coarse bucket read the same on both. (A bucket Frigate could not
    answer at all is the caller's `motion_unavailable`, not zero.)
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    n = max(0, int((end - start) / scale + 0.9999999))
    sums = [0.0] * n
    counts = [0] * n
    seen: set[int] = set()
    for ts, value in points:
        if ts < start or ts >= end:
            continue
        i = int((ts - start) / scale)
        if 0 <= i < n:
            sums[i] += value
            counts[i] += 1
            if fetch_scale and fetch_scale > 0:
                seen.add(int((ts - start) / fetch_scale))
    if fetch_scale and 0 < fetch_scale < scale:
        # Expected fetch-grid slots Frigate left out count as zero motion.
        m = int((end - start) / fetch_scale + 0.9999999)
        for k in range(m):
            if k in seen:
                continue
            i = int(k * fetch_scale / scale)
            if 0 <= i < n:
                counts[i] += 1
    return [(sums[i] / counts[i]) if counts[i] else 0.0 for i in range(n)]
