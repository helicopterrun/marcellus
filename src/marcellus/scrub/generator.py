"""Continuous scrub-cache generator orchestration (§5).

Ties together: recording enumeration from `frigate.db`, path mapping (§8.2),
GOP-driven sampling (§5.2), cell assignment with gap/drift splitting
(grid.py), tiling (tiling.py), and persistence to the two sidecar tables
(db.py). Designed to be driven either by the continuous ~60s in-process
asyncio task (server.py lifespan, §5.4a) or the `fsc scrub` CLI (§5.7).

Everything on disk lives under `scrub.cache_dir`, including the per-segment
extraction scratch (`.work/`) and the per-sheet cell store (`.cells/`). Both
used to escape it: extraction staged frames in the system temp dir and left
every frame it didn't use behind, and the cell store was never swept at all --
about a gigabyte a day per camera at 1 fps, on the filesystem §8.3 goes out of
its way to keep free. Keeping the scratch inside `cache_dir` also guarantees
the cell writes are same-filesystem hardlinks; across a device boundary they
failed with EXDEV and were silently swallowed, leaving black sheets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from marcellus import db
from marcellus.config import Settings
from marcellus.scrub import ffmpeg_io, grid, tiling
from marcellus.scrub.mapping import map_recording_path

logger = logging.getLogger(__name__)

# GOP-vs-interval tolerance for choosing keyframe-only decode over full-decode
# fps fallback (§5.2): if the measured GOP is within this factor of the target
# interval, keyframe skipping alone gives a close-enough native cadence to
# rely on cell-assignment's own drift check to catch anything that slips.
_GOP_TOLERANCE = 1.3

_WORK_DIRNAME = ".work"
_CELLS_DIRNAME = ".cells"


@dataclass
class SourceProfile:
    """Generator state that outlives a single cycle.

    Mostly what we've measured about each camera's stream -- keyframe spacing
    and display shape -- probed once per camera per process rather than per
    segment or per cycle, since neither changes unless the camera is
    reconfigured and re-probing every cycle cost two ffprobe calls per camera
    against an ~80s cycle.

    Also carries `backfill_cursor` and `live_edge_cursor`, which are scheduler
    state rather than a stream property: each is the camera the next cycle's
    backfill (resp. live-edge) pass starts from, and they live here because
    this is already the object the generation loop keeps between cycles.
    """

    gop_s: dict[str, float]
    aspect: dict[str, float]
    backfill_cursor: int
    live_edge_cursor: int

    def __init__(self) -> None:
        self.gop_s = {}
        self.aspect = {}
        self.backfill_cursor = 0
        self.live_edge_cursor = 0


def _cells_dir(cache_dir: Path, camera: str, interval_s: float, sheet_start: float) -> Path:
    """Per-sheet cell store.

    The sheet start must be rendered losslessly (`grid.fmt_time`, not `%g`):
    at six significant digits every epoch second within the same ~11-day window
    formats to the same string, so *every* sheet of a camera+interval shared one
    directory. Cells are named by their index within a sheet, so sheet N+1's
    cell 000 collided with sheet N's, `_persist_cells` skipped it as already
    present, and the new sheet published the previous sheet's frames.
    """
    return (
        cache_dir
        / camera
        / f"{interval_s:g}"
        / _CELLS_DIRNAME
        / grid.fmt_time(sheet_start)
    )


def _work_root(cache_dir: Path) -> Path:
    work = cache_dir / _WORK_DIRNAME
    work.mkdir(parents=True, exist_ok=True)
    return work


async def _sample_segment(
    seg_path: Path,
    seg_start: float,
    interval_s: float,
    gop_s: float,
    sem: asyncio.Semaphore,
    work_dir: Path,
    *,
    cell_w: int,
    cell_h: int,
) -> list[grid.Frame]:
    """Extract frames from one segment into `work_dir`, returning achieved
    (timestamp, path) pairs. Chooses keyframe-only decode when GOP ~= interval,
    else the full-decode fps fallback (§5.2).

    The caller owns `work_dir` and deletes it once the frames have been either
    accepted into the cell store or discarded, so nothing survives the cycle.
    """
    async with sem:
        if gop_s <= interval_s * _GOP_TOLERANCE:
            extracted = await ffmpeg_io.extract_keyframes_with_pts(
                seg_path, work_dir, cell_w=cell_w, cell_h=cell_h
            )
            frames = [
                grid.Frame(timestamp=seg_start + pts, path=str(path))
                for pts, path in extracted
            ]
            # Keyframes are only guaranteed to be no *sparser* than the GOP, so
            # a tier whose interval is several GOPs wide gets several frames per
            # cell. Thin them to the target cadence here rather than handing
            # colliding frames to cell assignment, which can only respond by
            # splitting the bucket.
            return grid.decimate_to_grid(frames, interval_s)
        jpgs = await ffmpeg_io.extract_fps(
            seg_path, work_dir, interval_s, cell_w=cell_w, cell_h=cell_h
        )
        return [
            grid.Frame(timestamp=seg_start + i * interval_s, path=str(jpgs[i]))
            for i in range(len(jpgs))
        ]


def _publish_sheet_version(
    cache_dir: Path,
    camera: str,
    interval_s: float,
    sheet_start: float,
    count: int,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    fmt: str,
) -> tuple[str, int] | None:
    """Tile the current cell files for this sheet and atomically publish a new
    immutable version (URL includes `count`, §4.3). Returns the on-disk
    relative path stored in scrub_sheets.path and the count actually published,
    or None when there is nothing to publish.

    **The published count is measured from the cell store, never taken from the
    caller**, and the whole grid is scanned rather than the caller's `count`,
    which is only what this pass happened to touch. The tiler composes onto a
    black canvas and places each cell at its own index, so any index inside the
    declared count without a file on disk renders as a black frame -- and the
    count is what tells the client that cell is covered. Deriving the count from
    assignment indices instead meant a cell whose file was never stored (a link
    failure, a segment lost between assignment and persistence) was advertised as
    real imagery and served as black pixels, with nothing in the index saying so.

    Scanning the full grid rather than `count` is what makes a hole recoverable.
    A pass that fills only the missing cell touches one index, so a caller-bounded
    scan would stop one past the hole and never look at the cells beyond it --
    real imagery, already decoded and stored, stranded until retention deleted it.
    Reading the store itself means the sheet extends over everything contiguous
    the moment the hole is filled, at no decode cost.

    Truncating to the contiguous run keeps the sheet consistent with the bucket
    contract it inherits: cell k is `sheet_start + k * interval_s`, which is only
    true while the cells are contiguous from zero. Cells past a hole are kept on
    disk, not discarded.
    """
    cells_dir = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cells: list[tiling.Cell] = []
    for i in range(cols * rows):
        p = cells_dir / f"{i:03d}.jpg"
        if not p.exists():
            break
        cells.append((i, p))
    published = len(cells)
    if published == 0:
        return None
    if published < count:
        logger.warning(
            "scrub: %s %gs sheet %s has no cell %03d -- publishing %d of %d cells rather than "
            "padding the rest black and claiming them",
            camera, interval_s, grid.fmt_time(sheet_start), published, published, count,
        )
    count = published

    rel = grid.sheet_rel_path(camera, interval_s, sheet_start, count, grid.ext_for_format(fmt))
    out_path = cache_dir / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".publish-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tile = tiling.tile_sheet_webp if fmt == "webp" else tiling.tile_sheet
        tile(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h, out_path=tmp)
        os.replace(tmp, out_path)  # atomic publish (mirrors wildlife.py)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return rel, count


def _persist_cells(
    cache_dir: Path, camera: str, interval_s: float, sheet_start: float,
    assignments: list[grid.Assignment],
) -> None:
    cells = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cells.mkdir(parents=True, exist_ok=True)
    for a in assignments:
        dst = cells / f"{a.idx:03d}.jpg"
        if dst.exists():
            continue
        # Link rather than move: one decode now feeds several tiers, and two of
        # them can pick the same frame for their respective grids. Moving it
        # into the first tier's store left the second with a vanished source, a
        # logged failure and a black cell. The scratch dir lives under
        # `cache_dir` (`_work_root`), so this is a same-filesystem link and the
        # caller's TemporaryDirectory still reclaims the original.
        try:
            os.link(a.path, dst)
        except OSError:
            try:
                shutil.copyfile(a.path, dst)
            except OSError as exc:
                # Never silent: a failure here is a cell the sheet will render
                # black, and it used to be swallowed whole.
                logger.warning("scrub: could not store cell %s -> %s: %s", a.path, dst, exc)


def _retire_overclaiming_sheets(
    conn: sqlite3.Connection,
    cache_dir: Path,
    camera: str,
    interval_s: float,
    sheet_start: float,
    kept_count: int,
) -> None:
    """Delete already-published versions of this sheet that claim more cells
    than the cell store can back.

    §4.3 keeps superseded versions servable forever, and that is still true of
    every version published from real imagery. This removes only versions whose
    count was inflated by the pre-fix publish path -- they advertise cells that
    render black. It has to happen here rather than being left to age out,
    because `db.list_scrub_sheets` picks the *highest* count as a sheet's current
    version: leaving a 45-cell claim in the table would keep it at the head of
    the index no matter how honest the version published beside it.
    """
    rows = conn.execute(
        "SELECT path FROM scrub_sheets "
        "WHERE camera = ? AND interval_s = ? AND start_ts = ? AND count > ?",
        (camera, interval_s, sheet_start, kept_count),
    ).fetchall()
    if not rows:
        return
    conn.execute(
        "DELETE FROM scrub_sheets "
        "WHERE camera = ? AND interval_s = ? AND start_ts = ? AND count > ?",
        (camera, interval_s, sheet_start, kept_count),
    )
    conn.commit()
    for row in rows:
        with contextlib.suppress(OSError):
            (cache_dir / row["path"]).unlink()


def _drop_cells_dir(cache_dir: Path, camera: str, interval_s: float, sheet_start: float) -> None:
    """Discard a sheet's cell store once it can never be re-tiled."""
    shutil.rmtree(_cells_dir(cache_dir, camera, interval_s, sheet_start), ignore_errors=True)


def _prune_cell_dirs(cache_dir: Path, camera: str | None, cutoff: float, span_cells: int) -> int:
    """Drop cell stores whose sheet ended before `cutoff`.

    Walked from disk rather than from the DB because a cell store outlives the
    row that produced it (rows are deleted first, then their files).

    Also removes now-empty `.cells`/interval/camera parent directories left
    behind once their last sheet dir is gone. `_prune_cell_dirs` runs after
    `prune()` has already unlinked the published sheet files for `cutoff`, so
    by the time this walk finishes an interval/camera dir with nothing left
    really is done, not mid-generation. `os.rmdir` on a directory that still
    has content (a racing generator wrote a new sheet, or the dir predates
    `_CELLS_DIRNAME`) raises OSError, which is suppressed -- leaving it alone
    is the correct outcome there.
    """
    if not cache_dir.is_dir():
        return 0
    removed = 0
    cam_dirs = (
        [cache_dir / camera]
        if camera
        else [d for d in cache_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    )
    for cam_dir in cam_dirs:
        if not cam_dir.is_dir():
            continue
        for interval_dir in cam_dir.iterdir():
            if not interval_dir.is_dir():
                continue
            try:
                interval_s = float(interval_dir.name)
            except ValueError:
                continue
            cells_root = interval_dir / _CELLS_DIRNAME
            if cells_root.is_dir():
                for sheet_dir in cells_root.iterdir():
                    try:
                        sheet_start = float(sheet_dir.name)
                    except ValueError:
                        continue
                    if sheet_start + span_cells * interval_s < cutoff:
                        shutil.rmtree(sheet_dir, ignore_errors=True)
                        removed += 1
                with contextlib.suppress(OSError):
                    cells_root.rmdir()
            with contextlib.suppress(OSError):
                interval_dir.rmdir()
        with contextlib.suppress(OSError):
            cam_dir.rmdir()
    return removed


def _unlink_reporting_failures(path: Path, failures: list[tuple[Path, OSError]]) -> bool:
    """Unlink `path`; already-gone is success, any other `OSError` is recorded
    in `failures` (path + error) for the caller to summarize in one log line
    rather than the old blanket `contextlib.suppress(OSError)`, which made a
    permissions/EBUSY problem indistinguishable from a normal race with
    another prune run.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        failures.append((path, exc))
        return False
    return True


def _log_unlink_failures(context: str, failures: list[tuple[Path, OSError]]) -> None:
    if failures:
        first_path, first_err = failures[0]
        logger.warning(
            "scrub: %s: %d unlink failures, first: %s: %s",
            context, len(failures), first_path, first_err,
        )


_PUBLISH_TMP_MAX_AGE_S = 3600.0


def _reap_orphaned_publish_temp(cache_dir: Path, *, now: float, max_age_s: float) -> int:
    """Unlink `.publish-*` temp files left behind by a hard kill between
    `tempfile.mkstemp` and the `os.replace` that publishes it (see
    `_publish_sheet_version`). Nothing else ever cleans these up, so an
    unattended deployment that takes an OOM kill or a power loss mid-publish
    leaks one forever. Only files older than `max_age_s` are touched -- a
    fresh one may still be mid-write by a live generation pass.
    """
    if not cache_dir.is_dir():
        return 0
    removed = 0
    for interval_dir in cache_dir.glob("*/*"):
        if not interval_dir.is_dir():
            continue
        for tmp in interval_dir.glob(".publish-*"):
            with contextlib.suppress(OSError):
                if not tmp.is_file():
                    continue
                if now - tmp.stat().st_mtime < max_age_s:
                    continue
                tmp.unlink()
                removed += 1
    return removed


def _span_covered_by_aged(
    aged_rows: list[sqlite3.Row], start_ts: float, end_ts: float
) -> bool:
    """Whether `aged_rows` (sorted by `start_ts`) together cover `[start_ts,
    end_ts)` with no gap. Adjacent/overlapping rows merge; a gap anywhere in
    the span, or no aged coverage at all, means the span is not covered.
    """
    covered_through = start_ts
    for row in aged_rows:
        if row["start_ts"] > covered_through:
            break
        covered_through = max(covered_through, row["end_ts"])
        if covered_through >= end_ts:
            return True
    return covered_through >= end_ts


def _retire_stale_recent_buckets(
    sidecar_conn: sqlite3.Connection,
    cache_dir: Path,
    camera: str,
    recent_interval_s: float,
    boundary: float,
    aged_interval_s: float | None = None,
) -> None:
    """Drop any recent-tier bucket/sheet that now falls (even partially)
    before `boundary` (§4.2 non-overlap, §5.5 thinning) -- but only once the
    aged tier already has data covering its span.

    `boundary` (= now - aged_after_h) advances every cycle, so a recent
    bucket created when it was still "recent" eventually ages past it. Once
    that happens the aged tier owns that span at its own coarser interval,
    so the finer recent-tier bucket must be retired rather than left to
    overlap it. A bucket straddling the boundary is retired wholesale too
    (rather than clipped) -- the aged tier will regenerate the whole span
    fresh from the underlying recordings, which is simpler than surgically
    splitting an already-tiled sheet, and correct since the source segments
    are still on disk within retention.

    `aged_interval_s`, when given, gates retirement on the aged tier already
    covering the stale bucket's exact span: backfill runs on its own budget
    and can lag behind the boundary by cycles, and retiring the recent-tier
    data before the aged tier has regenerated it erases that span with
    nothing to refill it from at the finer cadence -- measured live as a
    total loss of the recent tier for cameras whose live edge routinely ate
    the whole tick. Omit it (`None`) to retire unconditionally, e.g. when no
    aged tier is configured for this plan at all.
    """
    stale = sidecar_conn.execute(
        "SELECT start_ts, end_ts FROM scrub_buckets "
        "WHERE camera = ? AND interval_s = ? AND start_ts < ?",
        (camera, recent_interval_s, boundary),
    ).fetchall()
    if not stale:
        return

    if aged_interval_s is not None:
        aged_rows = sidecar_conn.execute(
            "SELECT start_ts, end_ts FROM scrub_buckets "
            "WHERE camera = ? AND interval_s = ? ORDER BY start_ts",
            (camera, aged_interval_s),
        ).fetchall()
        retirable = [
            r["start_ts"]
            for r in stale
            if _span_covered_by_aged(aged_rows, r["start_ts"], r["end_ts"])
        ]
    else:
        retirable = [r["start_ts"] for r in stale]
    if not retirable:
        return

    placeholders = ",".join("?" for _ in retirable)
    sheet_rows = sidecar_conn.execute(
        "SELECT path, start_ts FROM scrub_sheets "
        f"WHERE camera = ? AND interval_s = ? AND start_ts IN ({placeholders})",
        (camera, recent_interval_s, *retirable),
    ).fetchall()
    sidecar_conn.execute(
        "DELETE FROM scrub_buckets "
        f"WHERE camera = ? AND interval_s = ? AND start_ts IN ({placeholders})",
        (camera, recent_interval_s, *retirable),
    )
    sidecar_conn.execute(
        "DELETE FROM scrub_sheets "
        f"WHERE camera = ? AND interval_s = ? AND start_ts IN ({placeholders})",
        (camera, recent_interval_s, *retirable),
    )
    sidecar_conn.commit()
    for r in sheet_rows:
        p = cache_dir / r["path"]
        with contextlib.suppress(OSError):
            p.unlink()
        _drop_cells_dir(cache_dir, camera, recent_interval_s, r["start_ts"])


class _TierWriter:
    """Accumulates frames into one tier's buckets, sheets and cell store.

    One writer per decode pass (recent or aged), fed either by ffmpeg-sampled
    frames (`_generate_tier`/`_generate_tiers`) or by decimated cells cropped
    from an already-published finer tier's sheets (`generate_derived_tier`) --
    the bucket-splitting, sheet-publishing and cell-store bookkeeping below
    doesn't care which.
    """

    def __init__(
        self,
        settings: Settings,
        camera: str,
        *,
        interval_s: float,
        window_start: float,
        window_end: float,
        cell_w: int,
        cell_h: int,
        sidecar_conn: sqlite3.Connection,
    ) -> None:
        self.settings = settings
        self.camera = camera
        self.interval_s = interval_s
        self.window_start = window_start
        self.window_end = window_end
        self.cell_w = cell_w
        self.cell_h = cell_h
        self.conn = sidecar_conn
        scrub = settings.scrub
        self.cols, self.rows = scrub.sheet_cols, scrub.sheet_rows
        self.cells_per_sheet = self.cols * self.rows
        self.new_frames = 0
        self._bucket_start: float | None = None
        self._generated_through = 0.0
        self._last_slot: int | None = None
        # Sheets touched this cycle, with their declared cell count. Publishing
        # is deferred to sheet completion or the end of the cycle: every distinct
        # count is its own immutable object (§4.3), so publishing once per
        # *segment* wrote a full sheet image for every couple of cells -- ~30x
        # the bytes of the sheet it was building, all superseded and all kept
        # until retention.
        self._pending_sheets: dict[float, int] = {}
        self._dirty_sheets: set[float] = set()
        # Sheets published full, whose cell store has therefore been dropped:
        # re-tiling one would publish a blank image over it. Tracked separately
        # from `_pending_sheets` reaching `cells_per_sheet`, which is only the
        # highest index *touched* -- a sheet held short of full by a missing cell
        # must keep accepting cells so the hole can still be filled.
        self._sealed: set[float] = set()

    def resume(self, since: float) -> None:
        """Attach to an open bucket only if it ends where this pass begins.

        With live-edge and backfill passes interleaved, the newest incomplete
        bucket is usually the live one; attaching an older backfill pass to it
        would seal it at the wrong end and hand cell assignment negative
        indices. Geometry is part of the match: a bucket recorded at one cell
        size can't be continued at another, or its row would describe cells it
        doesn't contain and re-tiling would stretch the ones already stored.
        """
        open_bucket = self.conn.execute(
            "SELECT * FROM scrub_buckets WHERE camera = ? AND interval_s = ? AND complete = 0 "
            "AND width = ? AND height = ? "
            "AND generated_through BETWEEN ? AND ? ORDER BY start_ts DESC LIMIT 1",
            (self.camera, self.interval_s, self.cell_w, self.cell_h,
             since - self.interval_s * 1.5, since + self.interval_s * 1.5),
        ).fetchone()
        self._bucket_start = open_bucket["start_ts"] if open_bucket else None
        self._generated_through = open_bucket["generated_through"] if open_bucket else since
        # Grid slot of the newest frame actually stored, or None when nothing has
        # been generated for this tier yet. Distinct from `_generated_through`,
        # which falls back to the window edge -- a moment no frame sits behind,
        # and so not a slot anything should be excluded for.
        self._last_slot = (
            round(open_bucket["generated_through"] / self.interval_s) if open_bucket else None
        )

    async def _flush_sheet(self, sheet_start: float, count: int) -> None:
        """Publish this sheet at whatever count its cell store can actually back.

        `count` is the high-water index touched, an upper bound only; the
        published count comes back from `_publish_sheet_version`, which measures
        the contiguous run of cell files on disk. A sheet whose cells are all
        missing publishes nothing at all rather than an all-black image with a
        row claiming it.
        """
        scrub = self.settings.scrub
        published = await asyncio.to_thread(
            _publish_sheet_version,
            scrub.cache_dir, self.camera, self.interval_s, sheet_start,
            count, self.cols, self.rows, self.cell_w, self.cell_h, scrub.format,
        )
        if published is None:
            return
        rel, real_count = published
        complete = real_count >= self.cells_per_sheet
        db.upsert_scrub_sheet(
            self.conn, camera=self.camera, start_ts=sheet_start, interval_s=self.interval_s,
            cols=self.cols, rows=self.rows, cell_w=self.cell_w, cell_h=self.cell_h,
            count=real_count, path=rel, complete=complete,
        )
        self.conn.commit()
        _retire_overclaiming_sheets(
            self.conn, scrub.cache_dir, self.camera, self.interval_s, sheet_start, real_count
        )
        if complete:
            # Only a sheet published full can lose its cells: a sheet held short
            # by a hole still needs them, so the publish that follows the hole
            # being backfilled can re-tile the cells past it.
            self._sealed.add(sheet_start)
            _drop_cells_dir(scrub.cache_dir, self.camera, self.interval_s, sheet_start)

    async def feed(self, frames: list[grid.Frame]) -> None:
        """Take whatever of `frames` belongs to this tier and store it.

        The caller decodes at the finest interval in play, so a coarser tier
        gets several candidates per slot and has to thin them here -- handing
        colliding frames to `assign_cells` makes it split the bucket on every
        frame.
        """
        scrub = self.settings.scrub
        # Skip anything already represented: compare *grid slots*, not raw
        # timestamps. Each segment is decimated independently, so a slot
        # straddling a segment boundary gets a candidate from both sides, and
        # handing both to cell assignment forces a bucket split at every
        # boundary.
        pending = [
            f
            for f in grid.decimate_to_grid(frames, self.interval_s)
            if (self._last_slot is None or round(f.timestamp / self.interval_s) > self._last_slot)
            and self.window_start <= f.timestamp < self.window_end
        ]
        while pending:
            if self._bucket_start is None:
                self._bucket_start = pending[0].timestamp
            elif (pending[0].timestamp - self._generated_through) > self.interval_s * 1.5 or (
                grid.cell_index(pending[0].timestamp, self._bucket_start, self.interval_s)
                != grid.cell_index(self._generated_through, self._bucket_start, self.interval_s) + 1
            ):
                # Gap since the last accepted frame in THIS bucket (possibly
                # from a prior segment) -- must be caught here too, since
                # assign_cells only sees gaps *within* the frames passed to a
                # single call (§5.2, §11 gap-splits-bucket test).
                #
                # The index comparison catches what the timestamp comparison
                # cannot: a call whose *first* frame lands two or more cells past
                # the bucket's last stored one. `assign_cells` measures
                # contiguity against `accepted[-1]`, which is empty at the start
                # of every call, so a lone frame arriving one slot late was
                # accepted at its own index and the cell between was left with no
                # file -- a black cell in the middle of a sheet the bucket still
                # declares contiguous. Measured on live footage: 9 of 14 sheets
                # in one cycle had exactly this single-slot hole. The rounding
                # boundary is why the timestamp test misses it -- a two-slot jump
                # can achieve a delta just under 1.5x when the frames sit on
                # opposite edges of their cells.
                db.upsert_scrub_bucket(
                    self.conn, camera=self.camera, start_ts=self._bucket_start,
                    end_ts=self._generated_through + self.interval_s,
                    interval_s=self.interval_s,
                    width=self.cell_w, height=self.cell_h,
                    generated_through=self._generated_through, complete=True,
                )
                self.conn.commit()
                self._bucket_start = pending[0].timestamp
            result = grid.assign_cells(pending, self._bucket_start, self.interval_s)
            self.new_frames += len(result.accepted)

            if result.accepted:
                # Route accepted cells into their sheet (a bucket ~30 sheets deep).
                by_sheet: dict[float, list[grid.Assignment]] = {}
                for a in result.accepted:
                    sheet_idx_start = self._bucket_start + (
                        (a.idx // self.cells_per_sheet) * self.cells_per_sheet * self.interval_s
                    )
                    local = grid.Assignment(
                        idx=a.idx - round((sheet_idx_start - self._bucket_start) / self.interval_s),
                        timestamp=a.timestamp, path=a.path,
                    )
                    by_sheet.setdefault(sheet_idx_start, []).append(local)

                for sheet_start, cell_list in by_sheet.items():
                    if sheet_start not in self._pending_sheets:
                        existing = self.conn.execute(
                            "SELECT COALESCE(MAX(count),0) AS c, "
                            "       COALESCE(MAX(complete),0) AS done FROM scrub_sheets "
                            "WHERE camera=? AND interval_s=? AND start_ts=?",
                            (self.camera, self.interval_s, sheet_start),
                        ).fetchone()
                        self._pending_sheets[sheet_start] = int(existing["c"]) if existing else 0
                        if existing and int(existing["done"]):
                            self._sealed.add(sheet_start)
                    if sheet_start in self._sealed:
                        # Published full in an earlier cycle: its cell store is
                        # gone, so re-tiling would publish a blank sheet over it.
                        continue
                    _persist_cells(
                        scrub.cache_dir, self.camera, self.interval_s, sheet_start, cell_list
                    )
                    new_count = min(
                        self.cells_per_sheet,
                        max(self._pending_sheets[sheet_start],
                            max(a.idx for a in cell_list) + 1),
                    )
                    self._pending_sheets[sheet_start] = new_count
                    self._dirty_sheets.add(sheet_start)
                    if new_count >= self.cells_per_sheet:
                        # Every index this sheet has is now accounted for, so
                        # publish immediately rather than waiting for the cycle
                        # flush. Whether that publish *seals* the sheet depends on
                        # the cell store backing all of them -- a later pass that
                        # fills a hole re-dirties it (`_sealed`).
                        await self._flush_sheet(sheet_start, new_count)
                        self._dirty_sheets.discard(sheet_start)

                self._generated_through = max(a.timestamp for a in result.accepted)
                self._last_slot = round(self._generated_through / self.interval_s)
                db.upsert_scrub_bucket(
                    self.conn, camera=self.camera, start_ts=self._bucket_start,
                    end_ts=self._generated_through + self.interval_s,
                    interval_s=self.interval_s,
                    width=self.cell_w, height=self.cell_h,
                    generated_through=self._generated_through, complete=False,
                )
                self.conn.commit()

            if result.split_at is not None:
                # Seal the current bucket and start a fresh one at split_at.
                db.upsert_scrub_bucket(
                    self.conn, camera=self.camera, start_ts=self._bucket_start,
                    end_ts=self._generated_through + self.interval_s,
                    interval_s=self.interval_s,
                    width=self.cell_w, height=self.cell_h,
                    generated_through=self._generated_through, complete=True,
                )
                self.conn.commit()
                self._bucket_start = None
                pending = result.remaining
            else:
                pending = []

    async def flush(self) -> None:
        """One version per still-filling sheet per cycle, not per segment."""
        for sheet_start in sorted(self._dirty_sheets):
            await self._flush_sheet(sheet_start, self._pending_sheets[sheet_start])
        self._dirty_sheets.clear()


async def _tier_writer(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    window_start: float,
    window_end: float,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
) -> _TierWriter:
    cell_w, cell_h = await camera_cell_size(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    return _TierWriter(
        settings, camera,
        interval_s=interval_s, window_start=window_start, window_end=window_end,
        cell_w=cell_w, cell_h=cell_h, sidecar_conn=sidecar_conn,
    )


async def _generate_tier(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    window_start: float,
    window_end: float,
    since: float,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
    deadline: float | None = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Sample `[since, window_end)` for one thinning tier, never emitting frames
    outside `[window_start, window_end)` (§4.2 non-overlap, §5.5 thinning tiers).

    `since` is the caller's, not derived from MAX(generated_through) here: the
    scheduler runs this against the live edge and against holes behind it in the
    same cycle, and those passes have entirely different resume points.

    `deadline` (a `time.monotonic()` instant), when given, is checked between
    segments -- see `_generate_tiers`.

    `newest_first`, when set, picks the *newest* `budget` segments of
    `[since, window_end)` rather than the oldest -- see `_generate_tiers`.
    """
    if window_end <= window_start or budget <= 0:
        return {"camera": camera, "segments": 0, "new_frames": 0}
    writer = await _tier_writer(
        settings, camera, interval_s=interval_s,
        window_start=window_start, window_end=window_end,
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn, profile=profile,
    )
    return await _generate_tiers(
        settings, camera,
        writer=writer,
        drive_interval_s=interval_s,
        window_end=window_end, since=max(min(since, window_end), window_start),
        budget=budget,
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        profile=profile, sem=sem, deadline=deadline, newest_first=newest_first,
    )


async def _generate_tiers(
    settings: Settings,
    camera: str,
    *,
    writer: _TierWriter,
    drive_interval_s: float,
    window_end: float,
    since: float,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
    deadline: float | None = None,
    newest_first: bool = False,
) -> dict[str, Any]:
    """Open each segment in `[since, window_end)` and feed it to `writer`.

    `deadline`, when given, is checked before each segment -- not just between
    camera calls in the caller's own loop. Without this, a single camera's
    whole `budget` (a segment *count*, unbounded in wall time) could still run
    to completion after the caller's deadline had already passed: measured
    live, one camera's 12-segment backfill batch took ~13s on its own, which
    blew straight through a 5s budget floor reserved for the derived-tier pass
    that runs after it in the same cycle -- checking wall clock only between
    cameras was too coarse to protect that floor.

    `newest_first` picks the newest `budget` segments of the span instead of
    the oldest, then still feeds them to `writer` oldest-to-newest (bucket
    assembly is order-dependent). This is what `generate_live_edge` sets when
    a busy span has more segments than the live-edge budget: taking the
    oldest `budget` of them, as the default query does, never reaches the
    handful nearest "now" -- exactly the span the client scrubs blind into --
    and only closes the gap once the resume cursor crawls up to it over
    several cycles. Skipping straight to the newest segments leaves a hole
    behind them instead, which `_backfill_tier` already fills newest-first
    (§5.4), so nothing is left permanently uncovered.
    """
    scrub = settings.scrub
    if budget <= 0:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    # Oldest-first and budgeted: a cold start has no resume point, so `since` is
    # the far edge of the retention window and this query would otherwise return
    # days of segments to chew through before the loop yields.
    order = "start_time DESC" if newest_first else "start_time"
    rows_ = frigate_conn.execute(
        f"SELECT path, start_time, end_time FROM recordings "
        f"WHERE camera = ? AND end_time > ? AND start_time < ? ORDER BY {order} LIMIT ?",
        (camera, since, window_end, max(1, budget)),
    ).fetchall()
    if newest_first:
        rows_ = list(reversed(rows_))
    if not rows_:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    # `camera_gop_seconds` (called by every caller of `_generate_tier` before
    # it, via `generate_live_edge`/`generate_backfill`) always populates this
    # first -- re-probing here would just repeat that call against the same
    # segment.
    gop_s = profile.gop_s[camera]

    writer.resume(since)

    frames_before = writer.new_frames
    cell_w, cell_h = writer.cell_w, writer.cell_h
    missing_segments = 0
    processed = 0
    for row in rows_:
        if deadline is not None and time.monotonic() >= deadline:
            break
        processed += 1
        seg_path = map_recording_path(
            row["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        if not seg_path.exists():
            missing_segments += 1
            continue
        # One scratch dir per segment, inside cache_dir: the extractor writes
        # straight into it and it is removed whether or not the frames were
        # used, so nothing accumulates.
        with tempfile.TemporaryDirectory(
            prefix="extract-", dir=_work_root(scrub.cache_dir)
        ) as td:
            work_dir = Path(td)
            try:
                frames = await _sample_segment(
                    seg_path, row["start_time"], drive_interval_s, gop_s, sem, work_dir,
                    cell_w=cell_w, cell_h=cell_h,
                )
            except ffmpeg_io.FfmpegInterrupted as exc:
                # Shutdown took the child with it; not a fault of the segment,
                # and the hole-finder picks it up next cycle.
                logger.debug("scrub: sampling interrupted for %s: %s", seg_path, exc)
                continue
            except ffmpeg_io.FfmpegError as exc:
                logger.warning("scrub: sampling failed for %s: %s", seg_path, exc)
                continue
            if not frames:
                continue
            await writer.feed(frames)

    await writer.flush()

    if missing_segments:
        logger.warning(
            "scrub: %d/%d segment file(s) for %s did not resolve under "
            "frigate.recordings_path (%s) -- check the recordings mount (§8.2)",
            missing_segments, processed, camera, settings.frigate.recordings_path,
        )
    return {
        "camera": camera,
        "segments": processed,
        "new_frames": writer.new_frames - frames_before,
    }


#: How many holes a single cycle will look at before giving up. A span with no
#: recordings behind it (camera offline, motion-only retention) yields nothing
#: and must not stall every older span behind it forever.
_MAX_HOLES_PER_CYCLE = 50


def uncovered_spans(
    sidecar_conn: sqlite3.Connection,
    camera: str,
    interval_s: float,
    window_start: float,
    window_end: float,
) -> list[tuple[float, float]]:
    """Spans of `[window_start, window_end)` with no bucket behind them.

    Holes narrower than one interval are ignored -- they can't hold a frame.
    """
    rows = sidecar_conn.execute(
        "SELECT start_ts, end_ts FROM scrub_buckets "
        "WHERE camera = ? AND interval_s = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (camera, interval_s, window_start, window_end),
    ).fetchall()
    spans: list[tuple[float, float]] = []
    cursor = window_start
    for row in rows:
        if row["start_ts"] > cursor + interval_s:
            spans.append((cursor, min(row["start_ts"], window_end)))
        cursor = max(cursor, row["end_ts"])
        if cursor >= window_end:
            break
    if cursor < window_end - interval_s:
        spans.append((cursor, window_end))
    return spans


async def _backfill_tier(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    window_start: float,
    window_end: float,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
    deadline: float | None = None,
) -> dict[str, int]:
    """Fill holes in one tier, newest first, each from its newest end.

    §5.4's "backfill fills in behind the live edge": coverage should grow
    backwards from now, contiguously, so a user scrubbing an hour ago is served
    before one scrubbing three days ago. Generation within a hole still runs
    forward (cells accumulate forward), so each pass starts at the oldest of the
    hole's newest `budget` segments and the hole shrinks from the right.

    That start point comes from the recordings table rather than from arithmetic
    on the hole's width: a hole's newest end is frequently dead air (camera
    offline, motion-only retention past the continuous window), and a pass
    aimed there samples nothing and leaves the hole exactly as it found it.

    `deadline`, when given, is passed all the way down to `_generate_tiers`'
    per-segment check -- see there for why the per-camera check alone isn't
    tight enough.
    """
    segments = 0
    frames = 0
    spans = uncovered_spans(sidecar_conn, camera, interval_s, window_start, window_end)
    for hole_start, hole_end in list(reversed(spans))[:_MAX_HOLES_PER_CYCLE]:
        if budget <= 0 or (deadline is not None and time.monotonic() >= deadline):
            break
        newest = frigate_conn.execute(
            "SELECT start_time FROM recordings "
            "WHERE camera = ? AND end_time > ? AND start_time < ? "
            "ORDER BY start_time DESC LIMIT 1 OFFSET ?",
            (camera, hole_start, hole_end, budget - 1),
        ).fetchone()
        result = await _generate_tier(
            settings, camera,
            interval_s=interval_s,
            window_start=hole_start, window_end=hole_end,
            # Fewer segments in the hole than the budget -> take it whole.
            since=newest["start_time"] if newest else hole_start,
            budget=budget,
            frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
            profile=profile, sem=sem, deadline=deadline,
        )
        segments += result["segments"]
        frames += result["new_frames"]
        budget -= result["segments"]
    return {"segments": segments, "new_frames": frames}


def _newest_segment(
    settings: Settings, camera: str, frigate_conn: sqlite3.Connection
) -> Path | None:
    """A segment to probe this camera's stream properties from."""
    row = frigate_conn.execute(
        "SELECT path FROM recordings WHERE camera = ? ORDER BY end_time DESC LIMIT 1",
        (camera,),
    ).fetchone()
    if row is None:
        return None
    seg = map_recording_path(
        row["path"], settings.frigate.media_path, settings.frigate.recordings_path
    )
    return seg if seg.exists() else None


#: Segments pooled to measure a camera's keyframe spacing. One is not enough:
#: a 10s segment holding a 5s GOP contains two keyframes, so it offers a single
#: spacing sample that reads 4.5 or 5.0 purely by where the segment was cut.
#: Measured on this deployment, one camera produced both from consecutive
#: segments -- and since the interval keys every bucket, sheet and directory,
#: that spawned a second tier for the same camera.
_GOP_PROBE_SEGMENTS = 5


async def camera_gop_seconds(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    profile: SourceProfile,
) -> float | None:
    """Median keyframe spacing for `camera`, pooled across recent segments."""
    if camera in profile.gop_s:
        return profile.gop_s[camera]

    rows = frigate_conn.execute(
        "SELECT path FROM recordings WHERE camera = ? ORDER BY end_time DESC LIMIT ?",
        (camera, _GOP_PROBE_SEGMENTS),
    ).fetchall()
    deltas: list[float] = []
    fallback: float | None = None
    for row in rows:
        seg = map_recording_path(
            row["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        if not seg.exists():
            continue
        try:
            found = await ffmpeg_io.probe_keyframe_deltas(seg)
        except ffmpeg_io.FfmpegError:
            continue
        if found:
            deltas.extend(found)
        elif fallback is None:
            # No keyframe pair anywhere in the segment: one per segment at most.
            with contextlib.suppress(ffmpeg_io.FfmpegError):
                fallback = await ffmpeg_io.probe_gop_seconds(seg)

    if not deltas:
        if fallback is None:
            return None
        profile.gop_s[camera] = fallback
        return fallback

    deltas.sort()
    gop = deltas[len(deltas) // 2]
    profile.gop_s[camera] = gop
    return gop


async def camera_cell_size(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    profile: SourceProfile,
) -> tuple[int, int]:
    """Cell dimensions for `camera`: `cell_w` wide, height from the source shape.

    Scaling every camera to a fixed `cell_w x cell_h` squeezes anything whose
    aspect ratio isn't that of the configured cell -- a 4:3 camera rendered into
    a 16:9 cell comes out anamorphically narrowed, and nothing downstream can
    undo it because the pixels are already wrong. The height is therefore
    derived from the source's display aspect, and travels per sheet in the
    `cell_w`/`cell_h` metadata clients already read.

    Falls back to the configured `cell_h` when the shape can't be measured.
    """
    scrub = settings.scrub
    if not scrub.preserve_source_aspect:
        return scrub.cell_w, scrub.cell_h

    aspect = profile.aspect.get(camera)
    if aspect is None:
        seg = _newest_segment(settings, camera, frigate_conn)
        if seg is not None:
            try:
                measured = await ffmpeg_io.probe_display_aspect(seg)
            except ffmpeg_io.FfmpegError:
                measured = None
            if measured:
                profile.aspect[camera] = aspect = measured
    if not aspect:
        return scrub.cell_w, scrub.cell_h

    height = round(scrub.cell_w / aspect)
    height += height % 2  # even dimensions keep the scaler on its fast path
    return scrub.cell_w, max(2, height)


def tier_plan(
    settings: Settings, now: float, gop_s: float | None
) -> list[tuple[float, float, float]]:
    """`[(interval, window_start, window_end), ...]`, newest tier first.

    Normally two tiers: `recent_interval_s` within `aged_after_h` of now, the
    coarser `aged_interval_s` behind it (§5.5).

    When the camera's own keyframe spacing is coarser than `recent_interval_s`,
    the recent tier uses the keyframe cadence instead. Forcing a finer interval
    than the source provides means decoding every frame -- about five times the
    cost of keyframe extraction -- to synthesise stills the encoder never made
    distinct. If that lands at or past `aged_interval_s` the two tiers would
    describe the same cadence, so they collapse into one covering the whole
    retention window; there is nothing left for thinning to save.

    Returns only decode tiers -- at most `(recent, ...)` or `(recent, ...),
    (aged, ...)`. Derived tiers (`scrub.derived_intervals_s`) are not decode
    tiers at all: they're generated afterwards by decimating already-published
    sheets from whichever of these tiers is finest over a given span, and
    never appear in this plan (see `generate_derived_tier`).
    """
    scrub = settings.scrub
    retention_cutoff = now - scrub.retention_days * 86400
    boundary = max(now - scrub.aged_after_h * 3600, retention_cutoff)

    recent = scrub.recent_interval_s
    if scrub.match_keyframe_cadence and gop_s and gop_s > recent * _GOP_TOLERANCE:
        # Snapped to a half-second grid, never used raw. A measured GOP is a
        # median of observed spacings, so it comes out as 4.995056 rather than
        # 5.0 -- and the interval is part of every bucket key, sheet filename
        # and cache directory name. An unsnapped value would put that noise in
        # the URLs, and re-measuring slightly differently later would strand
        # everything generated under the previous one as a separate tier.
        recent = max(recent, round(gop_s * 2.0) / 2.0)

    if recent >= scrub.aged_interval_s:
        plan = [(recent, retention_cutoff, now)]
    else:
        plan = [(recent, boundary, now)]
        if boundary > retention_cutoff:
            plan.append((scrub.aged_interval_s, retention_cutoff, boundary))

    return plan


async def generate_camera(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Advance `camera`'s scrub cache by one generation cycle (§5.4), across
    both thinning tiers (§5.5).

    Spans within `aged_after_h` of `now` are generated at the recent tier's
    `recent_interval_s`; older spans (down to `retention_days`) at the
    coarser `aged_interval_s`. The two tiers never overlap (§4.2): a recent
    bucket that ages past the boundary is retired (`_retire_stale_recent_buckets`)
    rather than left to coexist with the aged bucket that now covers its
    span, and a segment straddling the boundary is naturally split between
    the two tier passes below (each pass only accepts frames inside its own
    `[window_start, window_end)`).

    **The live edge is serviced first, out of its own budget.** Walking each
    tier forward from the far edge of its window -- which is what "resume from
    MAX(generated_through)" amounts to on a cold cache -- meant the recent tier
    started 24h back and advanced 20 min of footage per cycle while a cycle cost
    ~36 min of wall clock across ten cameras. It lost ground continuously and
    never reached now, so `generated_through` sat a day in the past and the
    client's "is this generated?" check answered no for the one window people
    actually scrub. Holding the edge costs ~6 segments per camera per minute;
    everything left over goes to backfill, which fills in behind it.
    """
    scrub = settings.scrub
    retention_cutoff = now - scrub.retention_days * 86400
    aged_boundary = now - scrub.aged_after_h * 3600
    # Clamp: if aged_after_h is large enough to push the boundary before the
    # retention cutoff, there's no aged window at all -- everything in
    # retention is "recent".
    effective_boundary = max(aged_boundary, retention_cutoff)

    live = await generate_live_edge(
        settings, camera, frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        now=now, profile=profile, sem=sem,
    )
    back = await generate_backfill(
        settings, camera, budget=scrub.backfill_segments_per_cycle,
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        now=now, profile=profile, sem=sem,
    )
    # Retire after backfill, not before: retirement is gated on the aged
    # tier already covering the stale span, and it's this same cycle's
    # backfill call above that's most likely to have just produced it.
    _retire_stale_recent_buckets(
        sidecar_conn, scrub.cache_dir, camera, scrub.recent_interval_s,
        effective_boundary, aged_interval_s=scrub.aged_interval_s,
    )
    return {
        "camera": camera,
        "segments": live["segments"] + back["segments"],
        "new_frames": live["new_frames"] + back["new_frames"],
        "backfilled": back["backfilled"],
    }


async def generate_live_edge(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Hold `camera`'s recent tier against wall clock. Cheap and always first.

    The reel opens at the live edge, so that is where sprites have to exist:
    `generated_through` sitting in the past means a client's "is this
    generated?" check answers no for the one window people actually scrub.

    `deadline` (a `time.monotonic()` instant), when given, is threaded
    through to `_generate_tier`/`_generate_tiers`, which check it between
    segments -- never mid-segment. `generate_cycle` sets this so a slow live
    edge stops with time left for backfill's own guaranteed share, rather
    than consuming the whole tick and leaving backfill nothing (see
    `scrub.backfill_min_share_s`).
    """
    scrub = settings.scrub
    gop_s = await camera_gop_seconds(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    plan = tier_plan(settings, now, gop_s)
    interval_s, window_start, window_end = plan[0]
    # Only retire when a coarser aged tier actually exists to supersede this
    # one -- a collapsed (recent-only) plan would otherwise delete its own
    # buckets.
    if len(plan) > 1:
        _retire_stale_recent_buckets(
            sidecar_conn, scrub.cache_dir, camera, interval_s, window_start,
            aged_interval_s=scrub.aged_interval_s,
        )

    # Resume from where the recent tier actually reaches, but
    #    never crawl up from further back than `live_edge_lookback_s` -- a
    #    stale cache jumps to the edge and lets backfill close the gap behind
    #    it, rather than making every recent scrub wait for a day of history.
    recent_through = db.latest_generated_through(sidecar_conn, camera, interval_s)
    live_since = max(window_start, now - scrub.live_edge_lookback_s)
    if recent_through is not None and recent_through > live_since:
        live_since = recent_through
    result = await _generate_tier(
        settings, camera,
        interval_s=interval_s,
        window_start=window_start, window_end=window_end,
        since=live_since,
        budget=max(1, scrub.live_edge_segments),
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        profile=profile, sem=sem, newest_first=True, deadline=deadline,
    )
    return {"camera": camera, **result}


async def generate_backfill(
    settings: Settings,
    camera: str,
    *,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Fill history behind the live edge with whatever budget is left.

    Recent tier first -- it is what someone scrubbing the last day sees -- then
    the coarser aged tier.

    Every tier gets a guaranteed slice of the budget rather than drawing from one
    shared pool. Under the shared pool the tiers after the first hungry one were
    never reached at all: on this deployment the aged tier had tens of hours of
    holes on nine of ten cameras and consumed the whole budget every cycle, so a
    second tier could sit empty indefinitely. Unused slices roll forward, so a
    tier with no holes still donates its share to the next.

    `deadline` (a `time.monotonic()` instant) is checked per-segment, not just
    once per camera in the caller's own loop -- a single tier's segment budget
    is otherwise unbounded in wall time, and measured live, one 12-segment
    batch alone took ~13s, which blew straight through a floor reserved for
    the pass that runs after backfill in the same cycle.
    """
    gop_s = await camera_gop_seconds(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    plan = [(i, ws, we) for i, ws, we in tier_plan(settings, now, gop_s) if we > ws]
    if not plan or budget <= 0:
        return {"camera": camera, "segments": 0, "new_frames": 0, "backfilled": False}

    per_tier = max(1, budget // len(plan))
    segments = 0
    frames = 0
    remaining = budget
    # Two rounds. The first hands every tier a floor, so no tier can be starved
    # by a hungrier one ahead of it. The second offers whatever nobody needed
    # back in plan order, so an idle tier still donates to the recent tier --
    # a floor alone would have permanently halved the live tier's share on a
    # deployment whose aged window holds no recordings at all.
    for floor_only in (True, False):
        for interval_s, w_start, w_end in plan:
            if remaining <= 0 or (deadline is not None and time.monotonic() >= deadline):
                break
            alloc = min(remaining, per_tier) if floor_only else remaining
            result = await _backfill_tier(
                settings, camera,
                interval_s=interval_s,
                window_start=w_start, window_end=w_end,
                budget=alloc,
                frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
                profile=profile, sem=sem, deadline=deadline,
            )
            segments += result["segments"]
            frames += result["new_frames"]
            remaining -= result["segments"]

    return {
        "camera": camera,
        "segments": segments,
        "new_frames": frames,
        # Used its whole share: there is more history behind this camera, so the
        # loop shouldn't sit idle for a full interval before the next cycle.
        "backfilled": budget > 0 and remaining <= 0,
    }


def _is_whole_multiple(numerator: float, denominator: float) -> bool:
    if denominator <= 0:
        return False
    ratio = numerator / denominator
    return abs(ratio - round(ratio)) < 1e-6 and round(ratio) >= 1


async def _decimate_source(
    sidecar_conn: sqlite3.Connection,
    cache_dir: Path,
    camera: str,
    *,
    finer_interval_s: float,
    derived_interval_s: float,
    window_start: float,
    window_end: float,
    work_dir: Path,
    deadline: float | None = None,
) -> list[grid.Frame]:
    """Select every Nth already-published cell of `finer_interval_s`'s sheets
    landing on `derived_interval_s`'s epoch grid, cropped to its own temp file.

    No ffmpeg involved -- the source is a finer decode tier's own published
    sheet image, sliced with PIL. Grid alignment is exact: a finer tier's
    sheet `start_ts` is itself epoch-grid-aligned (bucket contiguity
    guarantees it), so cell k's timestamp is exactly
    `start_ts + k * finer_interval_s`, and it lands on the derived grid iff
    `round(cell_t / finer_interval_s) % ratio == 0`.

    `deadline`, when given, is checked once per source sheet. A single call
    can otherwise cover thousands of sheets (a coarse derived interval
    decimating from the whole retention window on its first-ever run) and
    run for as long as opening and cropping every one of them takes, with no
    regard for the caller's own budget -- the same class of problem the
    per-segment backfill deadline check exists to fix, one level up.
    """
    ratio = round(derived_interval_s / finer_interval_s)
    sheets = db.list_scrub_sheets(
        sidecar_conn, camera, window_start, window_end, interval=finer_interval_s
    )
    frames: list[grid.Frame] = []
    for sheet in sheets:
        if deadline is not None and time.monotonic() >= deadline:
            break
        img_path = cache_dir / sheet["path"]
        if not img_path.exists():
            continue
        cols = sheet["cols"]
        cell_w, cell_h = sheet["cell_w"], sheet["cell_h"]
        wanted: list[tuple[int, float]] = []
        for k in range(sheet["count"]):
            cell_t = sheet["start_ts"] + k * finer_interval_s
            if not (window_start <= cell_t < window_end):
                continue
            if round(cell_t / finer_interval_s) % ratio:
                continue
            wanted.append((k, cell_t))
        if not wanted:
            continue

        def _slice_sheet(
            img_path: Path = img_path,
            wanted: list[tuple[int, float]] = wanted,
            cols: int = cols,
            cell_w: int = cell_w,
            cell_h: int = cell_h,
            base: int = len(frames),
        ) -> list[grid.Frame]:
            # PIL decode + N crops + N JPEG encodes per sheet is real CPU and
            # disk time; on a worker thread so the event loop (proxy video
            # range requests included) keeps serving while a derived tier
            # catches up.
            out_frames: list[grid.Frame] = []
            with Image.open(img_path) as im:
                rgb = im.convert("RGB")
                for k, cell_t in wanted:
                    row, col = divmod(k, cols)
                    crop = rgb.crop(
                        (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
                    )
                    out = work_dir / f"{k:06d}-{base + len(out_frames):06d}.jpg"
                    crop.save(out, format="JPEG", quality=90)
                    out_frames.append(grid.Frame(timestamp=cell_t, path=str(out)))
            return out_frames

        try:
            frames.extend(await asyncio.to_thread(_slice_sheet))
        except OSError as exc:
            logger.warning(
                "scrub: could not read source sheet %s for decimation: %s", img_path, exc
            )
            continue
    frames.sort(key=lambda f: f.timestamp)
    return frames


async def generate_derived_tier(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Advance one derived tier for `camera` by decimating already-published
    decode-tier sheets (recent/aged) rather than sampling ffmpeg.

    Records buckets/sheets exactly like a decode tier, via the same
    `_TierWriter` -- the only difference is where the frames come from.
    """
    scrub = settings.scrub
    retention_cutoff = now - scrub.retention_days * 86400
    gop_s = profile.gop_s.get(camera)
    if gop_s is None:
        # Decimation runs after live-edge/backfill in generate_cycle, which
        # always probe first -- but a direct caller (CLI, test) can hit this
        # against a cold profile, so fall back rather than assume it's set.
        gop_s = await camera_gop_seconds(
            settings, camera, frigate_conn=frigate_conn, profile=profile
        )
    decode_plan = [
        (iv, ws, we)
        for iv, ws, we in tier_plan(settings, now, gop_s)
        if we > ws and iv <= interval_s and _is_whole_multiple(interval_s, iv)
    ]
    if not decode_plan:
        return {"camera": camera, "interval_s": interval_s, "new_frames": 0}

    cell_w, cell_h = await camera_cell_size(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    writer = _TierWriter(
        settings, camera, interval_s=interval_s,
        window_start=retention_cutoff, window_end=now,
        cell_w=cell_w, cell_h=cell_h, sidecar_conn=sidecar_conn,
    )
    since = db.latest_generated_through(sidecar_conn, camera, interval_s)
    resume_since = since if since is not None else retention_cutoff
    writer.resume(resume_since)

    new_frames = 0
    with tempfile.TemporaryDirectory(prefix="derive-", dir=_work_root(scrub.cache_dir)) as td:
        work_dir = Path(td)
        for finer_interval, seg_ws, seg_we in decode_plan:
            span_start = max(seg_ws, retention_cutoff, resume_since)
            span_end = min(seg_we, now)
            if span_end <= span_start:
                continue
            frames = await _decimate_source(
                sidecar_conn, scrub.cache_dir, camera,
                finer_interval_s=finer_interval, derived_interval_s=interval_s,
                window_start=span_start, window_end=span_end, work_dir=work_dir,
                deadline=deadline,
            )
            if frames:
                await writer.feed(frames)
                new_frames += len(frames)
            if deadline is not None and time.monotonic() >= deadline:
                break
    await writer.flush()
    return {"camera": camera, "interval_s": interval_s, "new_frames": new_frames}


async def generate_derived(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    deadline: float,
) -> dict[str, Any]:
    """Advance every configured derived tier for `camera`, stopping at `deadline`.

    Runs last in a generation cycle (after live-edge and backfill), consuming
    only whatever's left of the tick's deadline -- decimation is cheap PIL
    crops off already-published sheets, not ffmpeg, so it never competes with
    the two decode passes for the concurrency-limited resource.
    """
    total_frames = 0
    tiers_touched = 0
    for interval_s in settings.scrub.derived_intervals_s:
        if time.monotonic() >= deadline:
            break
        result = await generate_derived_tier(
            settings, camera, interval_s=interval_s,
            frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
            now=now, profile=profile, deadline=deadline,
        )
        total_frames += result["new_frames"]
        tiers_touched += 1
    return {"camera": camera, "new_frames": total_frames, "tiers_touched": tiers_touched}


async def generate_cycle(
    settings: Settings,
    *,
    now: float | None = None,
    profile: SourceProfile | None = None,
    backfill_deadline: float | None = None,
) -> list[dict[str, Any]]:
    """One generation pass across all opted-in cameras (§5.4).

    Pass a `profile` to keep measured stream properties between cycles; without
    one every cycle re-probes each camera's GOP and aspect, which is two ffprobe
    calls per camera against an ~80s cycle. Callers that want isolation (tests,
    one-shot CLI runs) simply omit it.

    `backfill_deadline` is a `time.monotonic()` instant the backfill phase must
    stop by, on top of its own `backfill_time_budget_s`. The generation loop
    passes its next tick, which is what keeps the trailing-window pass on cadence:
    backfill gets the time left in the tick rather than the tick getting whatever
    is left after backfill. Omit it and the phase is bounded by its own budget
    alone, which is what the CLI's one-shot run wants.
    """
    import time as _time

    now = now if now is not None else _time.time()
    started = _time.monotonic()
    scrub = settings.scrub
    sem = asyncio.Semaphore(scrub.ffmpeg_concurrency)
    profile = profile if profile is not None else SourceProfile()

    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        cameras = list(scrub.cameras) or [
            r["camera"] for r in conn.execute("SELECT DISTINCT camera FROM recordings").fetchall()
        ]
        if not scrub.cameras:
            # Renamed cameras leave recordings rows under the old name for the
            # whole retention window; don't generate for ghosts.
            from marcellus.zones import configured_camera_names

            configured = configured_camera_names(settings.frigate.config_path)
            if configured is not None:
                cameras = [c for c in cameras if c in configured]
        if not cameras:
            return []

        totals: dict[str, dict[str, Any]] = {
            c: {"camera": c, "segments": 0, "new_frames": 0, "backfilled": False}
            for c in cameras
        }

        def _merge(camera: str, result: dict[str, Any]) -> None:
            totals[camera]["segments"] += result.get("segments", 0)
            totals[camera]["new_frames"] += result.get("new_frames", 0)
            totals[camera]["backfilled"] |= result.get("backfilled", False)

        # Pass 1: every camera's live edge, before any camera's history. Doing
        # both per camera meant the last camera in the list waited out every
        # earlier camera's backfill before its edge was touched at all.
        #
        # Bounded by `live_edge_deadline`, checked between cameras (never
        # mid-segment -- `_generate_tiers` only checks between segments) so
        # that even a live edge slow enough to eat the whole tick (measured
        # live: 16-20s cycles against a 20s tick, decoding every frame for
        # cameras whose GOP is coarser than `recent_interval_s`) still leaves
        # `backfill_min_share_s` on the clock for Pass 2. Without this, the
        # 1s cameras never got their aged tier: backfill's window had always
        # already closed by the time Pass 1 finished.
        #
        # Uses the caller's tick (`backfill_deadline`) when given, else this
        # cycle's own `live_edge_interval_s` from now -- deliberately *not*
        # `backfill_time_budget_s`, which is a separate, much shorter knob
        # that must not also throttle the live edge.
        tick_end = (
            backfill_deadline if backfill_deadline is not None
            else started + scrub.live_edge_interval_s
        )
        live_edge_deadline = tick_end - scrub.backfill_min_share_s
        start_le = profile.live_edge_cursor % len(cameras)
        order_le = cameras[start_le:] + cameras[:start_le]
        served_le = 0
        for camera in order_le:
            # Always run at least one camera per cycle, even past the
            # deadline, so the live edge itself can never be starved.
            if served_le > 0 and _time.monotonic() >= live_edge_deadline:
                break
            served_le += 1
            try:
                _merge(camera, await generate_live_edge(
                    settings, camera, frigate_conn=conn, sidecar_conn=conn,
                    now=now, profile=profile, sem=sem, deadline=live_edge_deadline,
                ))
            except Exception:
                logger.exception("scrub: live edge failed for camera %s", camera)
                totals[camera]["error"] = True
        profile.live_edge_cursor = start_le + served_le
        if served_le < len(cameras):
            logger.info(
                "scrub: live-edge cut short (%d/%d cameras done)", served_le, len(cameras)
            )

        # Pass 2: backfill on genuine leftovers. Bounded by wall clock as well
        # as segment count -- how long a segment takes depends on the box, and
        # an over-long cycle delays the next live-edge pass, which is what let
        # the edge slip behind in the first place.
        share = max(1, scrub.backfill_segments_per_cycle // len(cameras))
        deadline = _time.monotonic() + scrub.backfill_time_budget_s
        if backfill_deadline is not None:
            deadline = min(deadline, backfill_deadline)
        # Backfill gets everything up to `deadline` minus a reserved floor for
        # Pass 3 below -- without this, backfill's own demand doesn't reliably
        # hit zero (a couple of cameras can have a persistent small trickle of
        # real holes every cycle) and it consumes the whole shared deadline
        # every time, so decimation never runs at all. Measured live: backfill
        # alone burned the full 22s default on 4 of 10 cameras, and derived-tier
        # generation got zero cycles across several minutes of real operation.
        # Capped at half of backfill's own budget: a reserve configured larger
        # than the budget itself (or a test/deployment with a tiny budget)
        # must not eliminate backfill's own cushion entirely, which would
        # break its "the first camera in rotation is always attempted" floor
        # -- clamping `max(monotonic(), ...)` to "now" collapses that cushion
        # to zero and races the very next statement's own clock read.
        reserve = min(scrub.derive_time_reserve_s, scrub.backfill_time_budget_s / 2)
        backfill_deadline_own = deadline - reserve
        # Start where the last cycle stopped. The deadline below routinely cuts
        # this loop off part-way -- one camera spending its whole share can take
        # half the budget -- and restarting at cameras[0] every cycle meant the
        # tail was never reached at all: measured on this deployment, four of ten
        # cameras had never been backfilled once, each still sitting behind a
        # single untouched 72h hole.
        start = profile.backfill_cursor % len(cameras)
        order = cameras[start:] + cameras[:start]
        served = 0
        for camera in order:
            if _time.monotonic() >= backfill_deadline_own:
                break
            served += 1
            try:
                _merge(camera, await generate_backfill(
                    settings, camera, budget=share, frigate_conn=conn, sidecar_conn=conn,
                    now=now, profile=profile, sem=sem, deadline=backfill_deadline_own,
                ))
            except Exception:
                logger.exception("scrub: backfill failed for camera %s", camera)
                totals[camera]["error"] = True
        profile.backfill_cursor = start + served

        # Pass 3: derived tiers, decimated from already-published decode-tier
        # sheets -- no ffmpeg, so this eats whatever live-edge+backfill left of
        # the tick's deadline, plus the floor reserved above (priority:
        # live-edge > backfill > decimation, but decimation is guaranteed to
        # run every cycle rather than only when backfill happens to idle).
        for camera in cameras:
            if _time.monotonic() >= deadline:
                break
            try:
                derived_result = await generate_derived(
                    settings, camera, frigate_conn=conn, sidecar_conn=conn,
                    now=now, profile=profile, deadline=deadline,
                )
                totals[camera]["new_frames"] += derived_result["new_frames"]
            except Exception:
                logger.exception("scrub: derived-tier generation failed for camera %s", camera)
                totals[camera]["error"] = True

        # One line per cycle. Whether the edge is being held is otherwise only
        # visible by querying the DB and inferring it from timestamps, and the
        # cycle's own duration is the floor on how stale the edge can get: a
        # camera is only touched once per cycle.
        elapsed = _time.monotonic() - started
        live_total = sum(r["segments"] for r in totals.values())
        # Worst across cameras of each camera's *newest* bucket. Taking the max
        # over every row instead measures the oldest backfill bucket in the
        # table, which has nothing to do with whether the edge is being held.
        # Across every interval, not just the configured one: a camera whose
        # source is coarser generates at its own cadence, so filtering on
        # `recent_interval_s` would read its stale rows from before that and
        # report a lag that only ever grows.
        # Only the cameras this cycle generates for: rows cached under a
        # camera's pre-rename name freeze at the rename and would otherwise
        # report a phantom ever-growing lag.
        placeholders = ",".join("?" for _ in cameras)
        lag_row = conn.execute(
            "SELECT MIN(newest) AS furthest FROM ("
            "  SELECT camera, MAX(generated_through) AS newest FROM scrub_buckets"
            f"  WHERE camera IN ({placeholders}) GROUP BY camera"
            ")",
            cameras,
        ).fetchone()
        # Against wall clock at log time, not the cycle's own `now`: a camera
        # serviced at the start of a 500s cycle is 500s stale by the end, and
        # measuring from `now` would report it as current.
        worst_lag = (
            _time.time() - lag_row["furthest"]
            if lag_row and lag_row["furthest"] is not None
            else float("nan")
        )
        # Per-tier coverage, so a tier that has stopped filling is visible in the
        # log rather than only by querying the DB -- which is how the coarse
        # tiers were found sitting empty for a day after they shipped.
        tier_rows = conn.execute(
            "SELECT interval_s, COUNT(*) AS buckets, "
            "       SUM(end_ts - start_ts) / 3600.0 AS hours "
            "FROM scrub_buckets GROUP BY interval_s ORDER BY interval_s"
        ).fetchall()
        tiers = " ".join(
            f"{grid.fmt_time(r['interval_s'])}s={r['hours'] or 0:.1f}h" for r in tier_rows
        )
        logger.info(
            "scrub: cycle %.0fs, %d cameras (%d backfilled), %d segments, %d frames; "
            "coverage %s; worst live-edge lag %.0fs",
            elapsed, len(cameras), served, live_total,
            sum(r["new_frames"] for r in totals.values()), tiers or "none", worst_lag,
        )
        if worst_lag > elapsed * 3 and elapsed > 0:
            logger.warning(
                "scrub: live edge is %.0fs behind after a %.0fs cycle -- generation is not "
                "keeping up. Narrow scrub.cameras, or raise scrub.recent_interval_s to at "
                "least the cameras' GOP so keyframe extraction is used instead of a full "
                "decode (see the cycle cost in this log).",
                worst_lag, elapsed,
            )
        return list(totals.values())
    finally:
        conn.close()


def sweep_superseded_versions(
    conn: sqlite3.Connection,
    cache_dir: Path,
    *,
    camera: str | None,
    grace_s: float,
    now: float | None = None,
) -> int:
    """Drop still-filling sheet versions that a larger version has replaced.

    A sheet is published once per generation tick while it fills, and every
    version is its own immutable object (§4.3), so the intermediate ones pile up
    until retention removes them days later -- roughly three times the tier's
    steady-state size at the default tick, measured on real footage. Only
    versions that are *both* superseded (a larger count exists for the same
    camera/interval/start) and older than `grace_s` are removed, so a URL from a
    client's most recent index fetch still resolves.

    Complete sheets are never touched: nothing can supersede a full sheet, and
    they are the versions clients keep longest.

    **The grace runs from when a version stopped being current, not from when it
    was published**, and those are far apart whenever a sheet sits as the only
    version of its span for a while -- which is normal, since backfill's
    round-robin may not return to a given hole for many cycles. Measuring from
    the version's own mtime made a sheet that had been current for two hours
    instantly sweepable the moment something superseded it, so a client that
    indexed it a minute earlier got a 404: precisely the case the grace exists
    to prevent.

    There is no supersession timestamp in the schema, but there doesn't need to
    be one. A version stops being current when the version that replaced it is
    published, so the test is simply **how long the current version has been
    current** -- once that exceeds the grace, every index fetch within the grace
    window returned the current URL and nobody can still be holding an older one.
    That is read from the current version's file mtime, which is written exactly
    once, at publication.

    A row whose file has already gone is swept regardless -- it can serve
    nothing, and leaving it would keep it in the index as a 404.
    """
    now = now if now is not None else time.time()
    # The camera filter is applied inside the grouped subquery as well as
    # outside it, so both halves see the same rows.
    inner_filter = "WHERE camera = ?" if camera is not None else ""
    outer_filter = "AND s.camera = ?" if camera is not None else ""
    params: list[Any] = [camera, camera] if camera is not None else []
    rows = conn.execute(
        f"""
        SELECT s.camera, s.start_ts, s.interval_s, s.count, s.path,
               current.path AS current_path
        FROM scrub_sheets s
        JOIN (
            SELECT camera, interval_s, start_ts, MAX(count) AS max_count
            FROM scrub_sheets
            {inner_filter}
            GROUP BY camera, interval_s, start_ts
        ) latest
        ON s.camera = latest.camera AND s.interval_s = latest.interval_s
           AND s.start_ts = latest.start_ts
        JOIN scrub_sheets current
        ON current.camera = s.camera AND current.interval_s = s.interval_s
           AND current.start_ts = s.start_ts AND current.count = latest.max_count
        WHERE s.count < latest.max_count AND s.complete = 0 {outer_filter}
        """,
        params,
    ).fetchall()

    removed = 0
    failures: list[tuple[Path, OSError]] = []
    for row in rows:
        path = cache_dir / row["path"]
        if path.exists():
            try:
                superseded_for = now - (cache_dir / row["current_path"]).stat().st_mtime
            except OSError:
                # The current version's file is gone, so this span is already
                # serving 404s and the grace protects nothing.
                superseded_for = float("inf")
            if superseded_for < grace_s:
                continue
        conn.execute(
            "DELETE FROM scrub_sheets WHERE camera = ? AND start_ts = ? AND interval_s = ? "
            "AND count = ?",
            (row["camera"], row["start_ts"], row["interval_s"], row["count"]),
        )
        _unlink_reporting_failures(path, failures)
        removed += 1
    if removed:
        conn.commit()
    _log_unlink_failures("sweep_superseded_versions", failures)
    return removed


def prune(
    settings: Settings, *, camera: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """Drop sheets/buckets past retention_days, oldest-first (§5.5, §5.7), and
    sweep superseded still-filling sheet versions (`sweep_superseded_versions`).
    """
    import time as _time

    now = now if now is not None else _time.time()
    cutoff = now - settings.scrub.retention_days * 86400
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        paths = db.delete_scrub_sheets_before(camera, cutoff, conn)
        n_buckets = db.delete_scrub_buckets_before(conn, camera, cutoff)
        conn.commit()
        n_superseded = sweep_superseded_versions(
            conn, settings.scrub.cache_dir, camera=camera,
            grace_s=settings.scrub.sheet_version_grace_s, now=now,
        )
    finally:
        conn.close()

    n_files = 0
    unlink_failures: list[tuple[Path, OSError]] = []
    for rel in paths:
        p = settings.scrub.cache_dir / rel
        if _unlink_reporting_failures(p, unlink_failures):
            n_files += 1
    _log_unlink_failures("prune", unlink_failures)
    n_cell_dirs = _prune_cell_dirs(
        settings.scrub.cache_dir,
        camera,
        cutoff,
        settings.scrub.sheet_cols * settings.scrub.sheet_rows,
    )
    n_publish_tmp = _reap_orphaned_publish_temp(
        settings.scrub.cache_dir, now=now, max_age_s=_PUBLISH_TMP_MAX_AGE_S
    )
    return {
        "sheets_deleted": len(paths),
        "files_deleted": n_files,
        "buckets_deleted": n_buckets,
        "cell_dirs_deleted": n_cell_dirs,
        "superseded_versions_deleted": n_superseded,
        "publish_tmp_deleted": n_publish_tmp,
        "unlink_failures": len(unlink_failures),
    }


def drop_intervals(
    settings: Settings, intervals: Sequence[float], *, camera: str | None = None
) -> dict[str, Any]:
    """One-shot sweep: unconditionally delete every bucket/sheet at exactly
    the given `interval_s` value(s), regardless of retention.

    For migrating a deployment off an interval that no longer means anything
    -- e.g. old `coarse_intervals_s` data (the whole-window-overlap piggyback
    mechanism this replaced) at defaults like 10.0/60.0 that either have no
    successor in `derived_intervals_s` or would carry data that doesn't obey
    the new decimation tier's grid-alignment invariants. Reachable as
    `fsc scrub prune --drop-interval`.
    """
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        paths: list[str] = []
        n_buckets = 0
        cell_dir_rows: list[tuple[str, float, float]] = []
        for interval_s in intervals:
            if camera is None:
                sheet_rows = conn.execute(
                    "SELECT path, camera, start_ts FROM scrub_sheets WHERE interval_s = ?",
                    (interval_s,),
                ).fetchall()
                conn.execute("DELETE FROM scrub_sheets WHERE interval_s = ?", (interval_s,))
                cur = conn.execute("DELETE FROM scrub_buckets WHERE interval_s = ?", (interval_s,))
            else:
                sheet_rows = conn.execute(
                    "SELECT path, camera, start_ts FROM scrub_sheets "
                    "WHERE camera = ? AND interval_s = ?",
                    (camera, interval_s),
                ).fetchall()
                conn.execute(
                    "DELETE FROM scrub_sheets WHERE camera = ? AND interval_s = ?",
                    (camera, interval_s),
                )
                cur = conn.execute(
                    "DELETE FROM scrub_buckets WHERE camera = ? AND interval_s = ?",
                    (camera, interval_s),
                )
            n_buckets += cur.rowcount
            paths.extend(r["path"] for r in sheet_rows)
            cell_dir_rows.extend((r["camera"], r["start_ts"], interval_s) for r in sheet_rows)
        conn.commit()
    finally:
        conn.close()

    n_files = 0
    for rel in paths:
        p = settings.scrub.cache_dir / rel
        with contextlib.suppress(OSError):
            p.unlink()
            n_files += 1
    for cam, start_ts, interval_s in cell_dir_rows:
        shutil.rmtree(
            _cells_dir(settings.scrub.cache_dir, cam, interval_s, start_ts), ignore_errors=True
        )
    return {
        "intervals": list(intervals),
        "sheets_deleted": len(paths),
        "files_deleted": n_files,
        "buckets_deleted": n_buckets,
    }
