"""Pure grid/URL/cell-assignment math for the scrub cache.

Deliberately free of I/O so the cadence-verification and gap-splitting rules
(docs/scrub-cache-and-proxy-spec.md §4.2, §5.2, and the highest-value test in
§11) can be exercised without ffmpeg or a real segment file.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


def grid_point(start: float, interval: float, k: int) -> float:
    """The k-th grid timestamp for a bucket starting at `start`."""
    return start + k * interval


def cell_index(t: float, start: float, interval: float) -> int:
    """Row-major cell index for wall-clock time `t` (spec §4.3)."""
    return round((t - start) / interval)


def within_bound(achieved: float, start: float, interval: float, k: int) -> bool:
    """True if `achieved` is within interval/2 of grid point k (spec §4.2)."""
    return abs(achieved - grid_point(start, interval, k)) <= interval / 2 + 1e-9


#: Sheet image formats, keyed by the `scrub.format` setting.
EXT_FOR_FORMAT = {"jpeg": ".jpg", "webp": ".webp"}


def ext_for_format(fmt: str) -> str:
    return EXT_FOR_FORMAT.get(fmt.lower(), ".jpg")


def fmt_time(x: float) -> str:
    """Render a wall-clock timestamp for a filename or directory name.

    Never `%g`: at six significant digits every epoch second in the same
    ~11-day window renders identically (1785380400 and 1785380496 both become
    "1.78538e+09"), which silently collapses distinct objects onto one name.

    Bucket/sheet starts land on whole seconds in practice; render as an integer
    when exact (matching the spec's own examples, e.g. "1785380400-1.0-96.jpg"),
    otherwise keep full precision.
    """
    return str(int(x)) if x == int(x) else repr(float(x))


def sheet_filename(start: float, interval: float, count: int, ext: str = ".jpg") -> str:
    """Content-addressed filename -- (start, interval, count) is the whole key
    (spec §4.3 finding 3: count MUST be in the name so a growing live sheet
    never reuses an immutable URL).

    `ext` follows `scrub.format`: a WebP sheet written to a `.jpg` name was
    served as `image/jpeg` (the route types the response off the suffix), and
    the `.webp` URL form the spec describes was unreachable.
    """

    def _fmt_interval(x: float) -> str:
        # Interval always carries a decimal point (spec examples: "1.0", "5.0").
        return f"{float(x):.1f}" if x == int(x) else repr(float(x))

    return f"{fmt_time(start)}-{_fmt_interval(interval)}-{count}{ext}"


def sheet_rel_path(
    camera: str, interval: float, start: float, count: int, ext: str = ".jpg"
) -> str:
    """On-disk path under scrub.cache_dir (spec §8.2)."""
    interval_dir = f"{interval:g}"
    return f"{camera}/{interval_dir}/{sheet_filename(start, interval, count, ext)}"


def sheet_url(camera: str, start: float, interval: float, count: int, ext: str = ".jpg") -> str:
    return f"/v1/scrub/{camera}/sheet/{sheet_filename(start, interval, count, ext)}"


def parse_sheet_spec(spec: str) -> tuple[float, float, int]:
    """Parse "{start}-{interval}-{count}.jpg" -> (start, interval, count).

    Raises ValueError on malformed input.
    """
    if not spec.endswith(".jpg") and not spec.endswith(".webp"):
        raise ValueError(f"unrecognised sheet extension: {spec}")
    stem = spec.rsplit(".", 1)[0]
    parts = stem.split("-")
    if len(parts) != 3:
        raise ValueError(f"malformed sheet spec: {spec}")
    start_s, interval_s, count_s = parts
    return float(start_s), float(interval_s), int(count_s)


def excluded_derived_intervals(
    bucket_rows: Sequence[dict[str, Any]], derived_intervals_s: Sequence[float]
) -> set[float]:
    """Which of `derived_intervals_s` should be dropped from `bucket_rows`.

    A derived interval that also happens to equal a camera's actual
    (GOP-adjusted) finest decode interval is NOT excluded -- it IS that
    camera's coverage, and dropping it would blank coverage entirely for that
    camera (`match_keyframe_cadence` can raise the recent tier's effective
    interval all the way up to a configured derived value).
    """
    derived_set = set(derived_intervals_s)
    finest = min((r["interval_s"] for r in bucket_rows), default=None)
    return {iv for iv in derived_set if iv != finest}


def exclude_derived_buckets(
    bucket_rows: list[dict[str, Any]], derived_intervals_s: Sequence[float]
) -> list[dict[str, Any]]:
    """Drop derived-tier buckets, keeping the coverage/reel one-bucket-per-instant
    contract (used identically by `/v1/scrub/{camera}/coverage` and
    `/v1/reel`).
    """
    exclude = excluded_derived_intervals(bucket_rows, derived_intervals_s)
    if not exclude:
        return bucket_rows
    return [r for r in bucket_rows if r["interval_s"] not in exclude]


@dataclass
class Frame:
    """A single sampled frame: its achieved wall-clock timestamp and source
    image path (already written to a temp location by the extractor)."""

    timestamp: float
    path: str


@dataclass
class Assignment:
    """One accepted cell placement within a bucket."""

    idx: int
    timestamp: float
    path: str


@dataclass
class AssignResult:
    bucket_start: float
    accepted: list[Assignment]
    # If the bucket had to be split (an off-grid frame or a gap), this is the
    # timestamp the *next* bucket should start at, and the remaining frames
    # that belong to it (not yet assigned -- caller re-invokes for them).
    split_at: float | None
    remaining: list[Frame]


def decimate_to_grid(frames: list[Frame], interval: float) -> list[Frame]:
    """Keep at most one frame per `interval`-wide slot, nearest to the slot.

    Keyframe-only decode gives whatever cadence the encoder chose, which is a
    *lower* bound on density, not a match: on this deployment Frigate emits a
    keyframe every ~1s, so the aged tier's 5s grid received five frames per
    cell. `assign_cells` then (correctly) refused to overwrite a cell and split
    the bucket -- on essentially every frame, so each bucket held one cell and
    each sheet was a full 96-cell image with a single frame in it.

    Slots are anchored to the absolute epoch grid (multiples of `interval`)
    rather than to the first frame, so every segment and every bucket agrees on
    where the grid points are and no drift accumulates across a long run.
    """
    if interval <= 0:
        raise ValueError("interval must be positive")
    best: dict[int, Frame] = {}
    for frame in frames:
        k = round(frame.timestamp / interval)
        target = k * interval
        current = best.get(k)
        if current is None or abs(frame.timestamp - target) < abs(current.timestamp - target):
            best[k] = frame
    return [best[k] for k in sorted(best)]


def assign_cells(
    frames: list[Frame],
    bucket_start: float,
    interval: float,
    *,
    max_gap_factor: float = 1.5,
) -> AssignResult:
    """Assign frames to cells of a bucket starting at `bucket_start`, enforcing
    the interval/2 achieved-timestamp bound and splitting on violation or on a
    recording gap (spec §4.2, §5.2) -- never silently rounding, never a
    placeholder cell.

    Cell indices within a bucket must also be *contiguous*. That is the bucket
    row's own contract ("every frame in [start_ts, end_ts) exists within
    interval_s/2 of start_ts + n*interval_s"), and it is what lets a client map
    cell position back to wall-clock time. A jump of a little over one interval
    can otherwise pass both the gap and drift checks -- frames at t=0, 1.4, 2.6
    with interval 1.0 land on cells 0, 1, 3 -- leaving cell 2 with nothing in
    it and every later cell describing a moment it doesn't contain.
    """
    accepted: list[Assignment] = []
    prev_ts: float | None = None
    for i, frame in enumerate(frames):
        # Gap check: an oversized jump since the previous accepted frame means
        # the recording itself had a hole -- split rather than bridge it.
        if prev_ts is not None and (frame.timestamp - prev_ts) > interval * max_gap_factor:
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        idx = cell_index(frame.timestamp, bucket_start, interval)
        if idx < 0:
            # Should not happen for a well-formed caller; treat as unassignable
            # and start a fresh bucket here rather than guess.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        if not within_bound(frame.timestamp, bucket_start, interval, idx):
            # Achieved timestamp drifted off the grid -- split instead of
            # rounding two different moments onto the same cell.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        if accepted and idx != accepted[-1].idx + 1:
            # Either a repeat of the last cell or a skipped one: both break the
            # bucket's contiguity contract, so split rather than record a hole.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        accepted.append(Assignment(idx=idx, timestamp=frame.timestamp, path=frame.path))
        prev_ts = frame.timestamp

    return AssignResult(bucket_start, accepted, None, [])
