"""Tests for the pure cell-assignment / cadence-verification logic
(scrub/grid.py). This is the highest-value test in the suite per the client-
side review (docs/scrub-cache-and-proxy-spec.md §11): the cadence guarantee
is silent until two series collide on one cell.
"""

from __future__ import annotations

from marcellus.scrub import grid


def test_achieved_timestamp_within_bound_all_accepted() -> None:
    interval = 1.0
    start = 1000.0
    frames = [grid.Frame(timestamp=start + k * interval, path=f"f{k}.jpg") for k in range(10)]
    result = grid.assign_cells(frames, start, interval)
    assert result.split_at is None
    assert len(result.accepted) == 10
    for a in result.accepted:
        assert grid.within_bound(a.timestamp, start, interval, a.idx)


def test_achieved_timestamp_within_bound_uses_own_interval_not_hardcoded_1s() -> None:
    """Same guarantee as above, but at the aged tier's own coarser interval
    (5.0s, §5.5) -- the /2 bound must scale with `interval`, not be hardcoded
    to the recent tier's 1.0s (would wrongly reject valid aged-tier frames,
    or wrongly accept off-grid ones)."""
    interval = 5.0
    start = 2000.0
    frames = [grid.Frame(timestamp=start + k * interval, path=f"f{k}.jpg") for k in range(6)]
    result = grid.assign_cells(frames, start, interval)
    assert result.split_at is None
    assert len(result.accepted) == 6
    for a in result.accepted:
        assert grid.within_bound(a.timestamp, start, interval, a.idx)
        # A 1.0s-scaled bound would be too tight for these 5.0s-spaced
        # frames were the check hardcoded -- pin the actual math directly.
        assert abs(a.timestamp - grid.grid_point(start, interval, a.idx)) <= interval / 2

    # A frame drifted by 2.5s (exactly interval/2 at 5.0s) is still within
    # bound; one micro-epsilon further out is not -- proves the bound tracks
    # `interval`, not a fixed 1.0s/0.5s constant.
    edge = grid.Frame(timestamp=start + 6 * interval + interval / 2, path="edge.jpg")
    assert grid.within_bound(edge.timestamp, start, interval, 6)
    too_far = grid.Frame(timestamp=start + 6 * interval + interval / 2 + 0.1, path="far.jpg")
    assert not grid.within_bound(too_far.timestamp, start, interval, 6)


def test_drift_off_grid_splits_bucket() -> None:
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 1.0, path="f1.jpg"),
        # Drifted more than interval/2 off the grid point for idx=2 (1002.0):
        # never silently round two different moments onto the same cell.
        grid.Frame(timestamp=start + 2.6, path="f2.jpg"),
        grid.Frame(timestamp=start + 3.6, path="f3.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 2
    assert result.split_at == start + 2.6
    assert [f.path for f in result.remaining] == ["f2.jpg", "f3.jpg"]

    # Re-invoking on the remaining frames with a fresh bucket start succeeds.
    result2 = grid.assign_cells(result.remaining, result.split_at, interval)
    assert result2.split_at is None
    assert len(result2.accepted) == 2


def test_recording_gap_splits_bucket_rather_than_bridging() -> None:
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 1.0, path="f1.jpg"),
        # A 30s gap (recording hole) -- must split, never fabricate frames
        # for the missing span (no placeholder/black cell, spec §4.2).
        grid.Frame(timestamp=start + 31.0, path="f2.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 2
    assert result.split_at == start + 31.0
    assert len(result.remaining) == 1


def test_no_placeholder_cells_ever_emitted() -> None:
    """A gap never produces an accepted cell for the missing span -- only
    real frames appear in `accepted`."""
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start, path="f0.jpg"),
        grid.Frame(timestamp=start + 50.0, path="f1.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 1  # not 51 -- no fabricated in-between cells
    assert result.accepted[0].idx == 0


def test_sheet_url_immutable_across_growing_count() -> None:
    url_a = grid.sheet_url("doorbell", 1785380400, 1.0, 12)
    url_b = grid.sheet_url("doorbell", 1785380400, 1.0, 40)
    assert url_a != url_b
    assert url_a == "/v1/scrub/doorbell/sheet/1785380400-1.0-12.jpg"
    assert url_b == "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg"


def test_sheet_spec_roundtrip() -> None:
    start, interval, count = grid.parse_sheet_spec("1785380400-1.0-96.jpg")
    assert (start, interval, count) == (1785380400.0, 1.0, 96)
    assert grid.sheet_filename(start, interval, count) == "1785380400-1.0-96.jpg"


def test_cell_index_row_major() -> None:
    cols = 12
    idx = grid.cell_index(1005.0, 1000.0, 1.0)  # 5th cell
    row, col = divmod(idx, cols)
    assert (row, col) == (0, 5)
    idx2 = grid.cell_index(1013.0, 1000.0, 1.0)
    row2, col2 = divmod(idx2, cols)
    assert (row2, col2) == (1, 1)


def test_skipped_cell_index_splits_bucket() -> None:
    """A jump of just over one interval passes both the gap check (<=1.5x) and
    the drift check, yet lands on a non-consecutive cell -- which would leave a
    hole in the sheet and shift every later cell's meaning. It must split.
    """
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 1.4, path="f1.jpg"),  # cell 1, within bound
        grid.Frame(timestamp=start + 2.6, path="f2.jpg"),  # cell 3 -- skips cell 2
    ]
    result = grid.assign_cells(frames, start, interval)
    assert [a.idx for a in result.accepted] == [0, 1]
    assert result.split_at == start + 2.6
    assert [f.path for f in result.remaining] == ["f2.jpg"]


def test_repeated_cell_index_still_splits() -> None:
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 0.2, path="dup.jpg"),  # also cell 0
    ]
    result = grid.assign_cells(frames, start, interval)
    assert [a.idx for a in result.accepted] == [0]
    assert result.split_at == start + 0.2


def test_accepted_indices_are_always_contiguous() -> None:
    """The bucket row's contract: every grid point in [start_ts, end_ts) has a
    frame behind it, so a client can map cell position back to wall-clock."""
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + t, path=f"f{i}.jpg")
        for i, t in enumerate([0.0, 1.1, 2.0, 3.4, 4.5, 5.4])
    ]
    result = grid.assign_cells(frames, start, interval)
    idxs = [a.idx for a in result.accepted]
    assert idxs == list(range(len(idxs)))


def test_sheet_filename_carries_the_format_extension() -> None:
    """A WebP sheet written to a `.jpg` name was served as image/jpeg, because
    the route types the response off the suffix."""
    start = 1_785_380_400.0
    assert grid.sheet_filename(start, 1.0, 96, ".webp") == "1785380400-1.0-96.webp"
    assert grid.ext_for_format("webp") == ".webp"
    assert grid.ext_for_format("jpeg") == ".jpg"
    assert grid.sheet_rel_path("doorbell", 1.0, start, 96, ".webp") == (
        "doorbell/1/1785380400-1.0-96.webp"
    )
    assert grid.sheet_url("doorbell", start, 1.0, 96, ".webp") == (
        "/v1/scrub/doorbell/sheet/1785380400-1.0-96.webp"
    )
    # Round-trips through the parser the route uses.
    assert grid.parse_sheet_spec("1785380400-1.0-96.webp") == (1785380400.0, 1.0, 96)


def test_decimate_thins_dense_keyframes_to_the_target_cadence() -> None:
    """Keyframe decode gives the encoder's cadence, not the tier's: with a 1s
    GOP and a 5s aged tier, five frames arrived per cell. assign_cells could
    only answer by splitting, so every bucket held one cell and every sheet was
    a 96-cell image containing a single frame."""
    interval = 5.0
    frames = [grid.Frame(timestamp=1000.0 + i, path=f"f{i}.jpg") for i in range(11)]
    kept = grid.decimate_to_grid(frames, interval)

    assert [f.timestamp for f in kept] == [1000.0, 1005.0, 1010.0]
    # And the thinned series now assigns to contiguous cells without splitting.
    result = grid.assign_cells(kept, kept[0].timestamp, interval)
    assert result.split_at is None
    assert [a.idx for a in result.accepted] == [0, 1, 2]


def test_decimate_picks_the_frame_nearest_each_grid_point() -> None:
    interval = 5.0
    frames = [
        grid.Frame(timestamp=1000.0, path="a.jpg"),
        grid.Frame(timestamp=1004.0, path="b.jpg"),  # 1s from the 1005 grid point
        grid.Frame(timestamp=1006.0, path="c.jpg"),  # also 1s -- first wins
        grid.Frame(timestamp=1010.2, path="d.jpg"),
    ]
    kept = grid.decimate_to_grid(frames, interval)
    assert [f.path for f in kept] == ["a.jpg", "b.jpg", "d.jpg"]


def test_decimate_is_a_no_op_when_frames_are_already_at_cadence() -> None:
    interval = 1.0
    frames = [grid.Frame(timestamp=1000.0 + i, path=f"f{i}.jpg") for i in range(10)]
    assert grid.decimate_to_grid(frames, interval) == frames


def test_decimate_keeps_sparse_frames_untouched() -> None:
    """A gap must survive decimation so the bucket still splits on it."""
    interval = 1.0
    frames = [
        grid.Frame(timestamp=1000.0, path="a.jpg"),
        grid.Frame(timestamp=1001.0, path="b.jpg"),
        grid.Frame(timestamp=1030.0, path="c.jpg"),
    ]
    assert grid.decimate_to_grid(frames, interval) == frames


def test_decimate_uses_an_absolute_grid_so_segments_agree() -> None:
    """Anchoring on each segment's own first frame would let the achieved
    cadence drift away from the grid over a long run. Every kept frame stays
    within interval/2 of its slot, whichever segment produced it."""
    interval = 5.0
    seg_a = [grid.Frame(timestamp=1000.0 + i, path=f"a{i}.jpg") for i in range(10)]
    seg_b = [grid.Frame(timestamp=1010.0 + i, path=f"b{i}.jpg") for i in range(10)]
    kept = grid.decimate_to_grid(seg_a, interval) + grid.decimate_to_grid(seg_b, interval)
    for f in kept:
        assert abs(f.timestamp - round(f.timestamp / interval) * interval) <= interval / 2

    # A segment boundary can still put two frames in one slot (each segment is
    # decimated on its own); the generator drops the repeat by slot -- see
    # test_scrub_generator.test_aged_tier_fills_whole_sheets.
    slots = [round(f.timestamp / interval) for f in kept]
    assert sorted(slots) == slots
