"""Tests for docs/scrub-cache-and-proxy-spec.md §4.6 motion totalization."""

from __future__ import annotations

from marcellus.scrub.motion import MAX_SAFE_SCALE, aggregate_motion, safe_fetch_scale


def test_safe_fetch_scale_caps_at_ceiling() -> None:
    assert safe_fetch_scale(3600) == MAX_SAFE_SCALE
    assert safe_fetch_scale(60) == 60


def test_motion_totalizes_full_requested_range_zero_filled() -> None:
    """A short-window / coarse-scale upstream response is aggregated &
    zero-filled to cover the FULL requested range (the two measured
    cliffs)."""
    start = 1000.0
    end = 1000.0 + 1800  # 1800s window
    scale = 60.0
    # Upstream only returned 10 buckets worth of points (540s), not the full
    # 1800s -- the measured short-window cliff.
    points = [(start + i * 60, 50.0) for i in range(9)]
    values = aggregate_motion(points, start, end, scale)
    assert len(values) == 30  # full 1800/60, not truncated to 9-10
    assert values[:9] == [50.0] * 9
    assert values[9:] == [0.0] * 21  # zero-filled, not omitted


def test_motion_zero_when_no_data_at_all() -> None:
    values = aggregate_motion([], 0.0, 300.0, 60.0)
    assert values == [0.0] * 5


def test_motion_ignores_points_outside_range() -> None:
    points = [(-100.0, 90.0), (50.0, 40.0), (10000.0, 90.0)]
    values = aggregate_motion(points, 0.0, 100.0, 50.0)
    assert values == [0.0, 40.0]  # 50.0 lands in bucket index 1 ([50,100))
