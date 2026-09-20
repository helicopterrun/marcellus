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


def safe_fetch_scale(requested_scale: float) -> float:
    """Never ask Frigate for a scale wider than the safe ceiling -- widen our
    own aggregation instead of trusting Frigate's degraded wide-scale answer."""
    return min(requested_scale, MAX_SAFE_SCALE)


def aggregate_motion(
    points: list[tuple[float, float]],
    start: float,
    end: float,
    scale: float,
) -> list[float]:
    """Bucket raw (timestamp, motion) points onto a `scale`-second grid
    covering the full `[start, end)`, zero-filled wherever there is no data.

    `value[i]` covers `[start + i*scale, start + (i+1)*scale)` (spec §4.5).
    Multiple points landing in the same bucket are averaged (matches
    Frigate's own "normalised 0-100" semantics -- summing would blow past
    100 and misrepresent a single continuous burst as more motion than it
    scored).
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    n = max(0, int((end - start) / scale + 0.9999999))
    sums = [0.0] * n
    counts = [0] * n
    for ts, value in points:
        if ts < start or ts >= end:
            continue
        i = int((ts - start) / scale)
        if 0 <= i < n:
            sums[i] += value
            counts[i] += 1
    return [(sums[i] / counts[i]) if counts[i] else 0.0 for i in range(n)]
