"""Find and fix sheets whose index entry claims more cells than they render.

The generator no longer publishes a count its cell store can't back
(`generator._publish_sheet_version`), but that only governs sheets published
*from now on*. A sheet whose span has already passed is never re-published --
backfill fills spans with no bucket behind them, and these spans have one -- so
an inflated count written before the fix stays in the index forever, handing the
client black pixels for a cell it was told is real. This module is the one-shot
repair for that existing data, reachable as `fsc scrub verify [--repair]`.

Two ways to establish the true count, cheapest first:

1. **From the cell store**, when it still exists: the files are authoritative
   and no decoding is needed. This is the same rule publication now applies --
   the contiguous run of cells present from zero.
2. **From the published image**, when the store is gone (dropped when a sheet
   completed, or swept by retention): decode the sheet and find the first cell
   that is the tiler's black padding.

Detection in (2) is deliberately conservative about what counts as padding, and
the asymmetry of the two mistakes is what sets the threshold.

Be clear about the size of a false positive, because it is not one cell. The
count is a contiguous run from zero -- that is the whole contract -- so reading
a real frame at index k as padding truncates the sheet there and gives up
indices k..95 as well, even where those hold real imagery. That is intended (a
sheet cannot declare a run it doesn't have), but it means a drifting threshold
costs coverage in sheet-sized chunks, not cell-sized ones. What it costs the
*client* is bounded: unclaimed spans come from Frigate's preview-frames cache,
which holds real stills of the same scene. Calling padding "real" is the
unbounded mistake -- it leaves the black frame on screen, which is the bug.

So the test is strict about black (padding is pasted as exactly 0 and JPEG keeps
a flat block flat) while the margin below keeps a lit neighbour's ringing from
rescuing a genuinely padded cell.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageStat

from marcellus import db
from marcellus.config import Settings
from marcellus.scrub import generator, grid, tiling

logger = logging.getLogger(__name__)

#: Mean channel sum (R+G+B) below which a cell's interior is the tiler's black
#: canvas. A padded cell is pasted as exactly (0,0,0) and JPEG keeps a flat
#: block flat, so its interior mean sits at ~0; the darkest real footage
#: measured on this deployment's night cameras still carries sensor noise well
#: above this.
_PADDING_MEAN_SUM = 3.0

#: Fraction of each cell trimmed from every edge before measuring. JPEG ringing
#: from a lit neighbour bleeds a few pixels across the boundary -- measured at a
#: peak channel of 34 against a neighbour's 255 -- and averaging it in would
#: lift a padded cell above the threshold on exactly the sheets that matter,
#: the ones with real imagery beside the hole.
_CELL_INSET = 0.15


@dataclass
class SheetVerdict:
    """One sheet's declared count against the count it can actually back."""

    camera: str
    start_ts: float
    interval_s: float
    declared: int
    real: int
    path: str
    source: str  # "cells" | "pixels"
    reason: str = "inflated"  # "inflated" | "unreadable"


def _true_count_from_cells(cache_dir: Path, row: dict[str, Any]) -> int | None:
    """Contiguous cells present in the sheet's store, or None if it's gone."""
    cells_dir = generator._cells_dir(
        cache_dir, row["camera"], row["interval_s"], row["start_ts"]
    )
    if not cells_dir.is_dir():
        return None
    count = 0
    while count < row["count"] and (cells_dir / f"{count:03d}.jpg").exists():
        count += 1
    return count


def _true_count_from_pixels(cache_dir: Path, row: dict[str, Any]) -> int | None:
    """Cells before the first black-padded one, read from the published image.

    Decodes at reduced scale via JPEG's DCT scaling (`Image.draft`): a padded
    cell is uniform, so an eighth-scale decode identifies it exactly as well as
    a full one for a fraction of the work -- these deployments hold tens of
    thousands of sheets.
    """
    path = cache_dir / row["path"]
    if not path.exists():
        return None
    cols: int = row["cols"]
    declared: int = row["count"]
    try:
        with Image.open(path) as im:
            # Only worth drafting when an eighth-scale cell is still big enough
            # to measure an interior in; below that the inset rounds cells away
            # to nothing and a full-scale read is cheap anyway.
            if im.width // 8 >= cols * 8:
                im.draft("RGB", (im.width // 8, im.height // 8))
            rgb = im.convert("RGB")
            # Cell geometry is derived from the *decoded* size, which draft() may
            # have scaled by any of 1/2, 1/4, 1/8 -- never from the configured
            # cell_w/cell_h, which describe the full-size image.
            cw = rgb.width / cols
            ch = rgb.height / row["rows"]
            inset_x, inset_y = cw * _CELL_INSET, ch * _CELL_INSET
            for idx in range(declared):
                col, rowi = idx % cols, idx // cols
                box = (
                    int(col * cw + inset_x), int(rowi * ch + inset_y),
                    max(int(col * cw + inset_x) + 1, int((col + 1) * cw - inset_x)),
                    max(int(rowi * ch + inset_y) + 1, int((rowi + 1) * ch - inset_y)),
                )
                if sum(ImageStat.Stat(rgb.crop(box)).mean) < _PADDING_MEAN_SUM:
                    return idx
    except OSError as exc:
        logger.warning("scrub: could not read sheet %s: %s", path, exc)
        return None
    return declared


def _sheet_is_corrupt(path: Path) -> bool:
    """Whether the published file itself fails to decode -- truncated or
    otherwise corrupt -- distinct from an inflated-but-decodable count.

    Checked independently of the cell store: a sheet's declared count can
    still match what its cells back while the published bytes are broken, and
    the client only ever sees the published file.
    """
    try:
        with Image.open(path) as im:
            im.load()
    except OSError:
        return True
    return False


def verify_sheets(
    settings: Settings, *, camera: str | None = None, interval: float | None = None
) -> list[SheetVerdict]:
    """Every published sheet whose declared count exceeds what it can render.

    Read-only. Sheets backed by a cell store are checked without decoding
    anything; the rest are decoded at reduced scale.
    """
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        sql = "SELECT * FROM scrub_sheets"
        clauses: list[str] = []
        params: list[Any] = []
        if camera is not None:
            clauses.append("camera = ?")
            params.append(camera)
        if interval is not None:
            clauses.append("interval_s = ?")
            params.append(interval)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = [
            dict(r)
            for r in conn.execute(sql + " ORDER BY camera, interval_s, start_ts", params)
        ]
    finally:
        conn.close()

    cache_dir = settings.scrub.cache_dir
    verdicts: list[SheetVerdict] = []
    for row in rows:
        path = cache_dir / row["path"]
        if path.exists() and _sheet_is_corrupt(path):
            logger.warning("scrub: sheet %s is corrupt/unreadable", path)
            verdicts.append(
                SheetVerdict(
                    camera=row["camera"], start_ts=row["start_ts"],
                    interval_s=row["interval_s"], declared=row["count"], real=0,
                    path=row["path"], source="pixels", reason="unreadable",
                )
            )
            continue

        real = _true_count_from_cells(cache_dir, row)
        source = "cells"
        if real is None:
            real = _true_count_from_pixels(cache_dir, row)
            source = "pixels"
        if real is None:
            continue  # no cells and no image: retention's problem, not ours
        if real < row["count"]:
            verdicts.append(
                SheetVerdict(
                    camera=row["camera"], start_ts=row["start_ts"],
                    interval_s=row["interval_s"], declared=row["count"], real=real,
                    path=row["path"], source=source, reason="inflated",
                )
            )
    return verdicts


def repair_sheet(settings: Settings, verdict: SheetVerdict) -> str | None:
    """Republish `verdict`'s sheet at its true count and retire the inflated one.

    Returns the new on-disk path, or None when the sheet had no real cells at
    all and was removed outright.

    The image itself is byte-identical either way -- the canvas is always
    cols x rows cells, so a sheet's pixels don't depend on its count -- which is
    why a sheet whose cell store is gone can still be repaired: the honest
    version is the same picture under the name that tells the truth about it.
    """
    cache_dir = settings.scrub.cache_dir
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = db.get_scrub_sheet(
            conn, verdict.camera, verdict.start_ts, verdict.interval_s, verdict.declared
        )
        if row is None:
            return None

        if verdict.reason == "unreadable":
            return _repair_unreadable(conn, cache_dir, verdict, row)

        if verdict.real == 0:
            conn.execute(
                "DELETE FROM scrub_sheets WHERE camera = ? AND start_ts = ? "
                "AND interval_s = ? AND count = ?",
                (verdict.camera, verdict.start_ts, verdict.interval_s, verdict.declared),
            )
            conn.commit()
            with contextlib.suppress(OSError):
                (cache_dir / row["path"]).unlink()
            return None

        ext = Path(row["path"]).suffix
        rel = grid.sheet_rel_path(
            verdict.camera, verdict.interval_s, verdict.start_ts, verdict.real, ext
        )
        source, target = cache_dir / row["path"], cache_dir / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Hardlink: same bytes under an honest name, and the inflated file is
            # unlinked below, so this does not double the sheet's disk footprint.
            try:
                target.hardlink_to(source)
            except OSError:
                target.write_bytes(source.read_bytes())

        db.upsert_scrub_sheet(
            conn, camera=verdict.camera, start_ts=verdict.start_ts,
            interval_s=verdict.interval_s, cols=row["cols"], rows=row["rows"],
            cell_w=row["cell_w"], cell_h=row["cell_h"], count=verdict.real, path=rel,
            complete=verdict.real >= row["cols"] * row["rows"],
        )
        conn.commit()
        # Retire exactly the version this verdict proved false -- not every
        # version above the true count. The generator may have published a
        # larger, legitimate version between the scan and this call (the sheet's
        # span can still be filling), and that one is not ours to delete. Any
        # other over-claiming version has its own verdict in the same scan.
        conn.execute(
            "DELETE FROM scrub_sheets WHERE camera = ? AND start_ts = ? AND interval_s = ? "
            "AND count = ?",
            (verdict.camera, verdict.start_ts, verdict.interval_s, verdict.declared),
        )
        conn.commit()
        if source != target:
            with contextlib.suppress(OSError):
                source.unlink()
        return rel
    finally:
        conn.close()


def _repair_unreadable(
    conn: Any, cache_dir: Path, verdict: SheetVerdict, row: dict[str, Any]
) -> str | None:
    """A corrupt/truncated sheet file: re-render from the cell store when it
    survives (same as the generator would have produced); otherwise there is
    no honest bytes left to serve, so drop it and let the generator's normal
    backfill/regeneration path fill the hole."""
    cells_dir = generator._cells_dir(
        cache_dir, verdict.camera, verdict.interval_s, verdict.start_ts
    )
    count = 0
    while count < row["count"] and (cells_dir / f"{count:03d}.jpg").exists():
        count += 1

    old_path = cache_dir / row["path"]
    if count == 0:
        conn.execute(
            "DELETE FROM scrub_sheets WHERE camera = ? AND start_ts = ? "
            "AND interval_s = ? AND count = ?",
            (verdict.camera, verdict.start_ts, verdict.interval_s, verdict.declared),
        )
        conn.commit()
        with contextlib.suppress(OSError):
            old_path.unlink()
        return None

    ext = Path(row["path"]).suffix
    rel = grid.sheet_rel_path(verdict.camera, verdict.interval_s, verdict.start_ts, count, ext)
    target = cache_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tiling.tile_sheet(
        [(i, cells_dir / f"{i:03d}.jpg") for i in range(count)],
        cols=row["cols"], rows=row["rows"], cell_w=row["cell_w"], cell_h=row["cell_h"],
        out_path=target,
    )
    db.upsert_scrub_sheet(
        conn, camera=verdict.camera, start_ts=verdict.start_ts, interval_s=verdict.interval_s,
        cols=row["cols"], rows=row["rows"], cell_w=row["cell_w"], cell_h=row["cell_h"],
        count=count, path=rel, complete=count >= row["cols"] * row["rows"],
    )
    conn.commit()
    if count != verdict.declared:
        # A different count published under a different name -- the old,
        # declared-count row/file is now a stale duplicate; retire it. When
        # the count is unchanged (rel == row["path"]) the upsert above *is*
        # that same row rewritten in place, so there is nothing left to
        # delete -- doing so anyway would delete the row we just wrote.
        conn.execute(
            "DELETE FROM scrub_sheets WHERE camera = ? AND start_ts = ? AND interval_s = ? "
            "AND count = ?",
            (verdict.camera, verdict.start_ts, verdict.interval_s, verdict.declared),
        )
        conn.commit()
        if target != old_path:
            with contextlib.suppress(OSError):
                old_path.unlink()
    return rel


def verify_and_repair(
    settings: Settings, *, camera: str | None = None, interval: float | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """Scan for over-claiming and unreadable sheets, optionally fixing each one."""
    verdicts = verify_sheets(settings, camera=camera, interval=interval)
    inflated = [v for v in verdicts if v.reason == "inflated"]
    unreadable = [v for v in verdicts if v.reason == "unreadable"]
    repaired = 0
    removed = 0
    if repair:
        for verdict in verdicts:
            result = repair_sheet(settings, verdict)
            if result is None:
                removed += 1
            else:
                repaired += 1
            logger.info(
                "scrub: repaired %s %gs sheet %s (%s) -- declared %d, real %d (from %s)",
                verdict.camera, verdict.interval_s, grid.fmt_time(verdict.start_ts),
                verdict.reason, verdict.declared, verdict.real, verdict.source,
            )
    return {
        "overclaiming_sheets": len(inflated),
        "cells_falsely_claimed": sum(v.declared - v.real for v in inflated),
        "unreadable_sheets": len(unreadable),
        "repaired": repaired,
        "removed": removed,
        "sheets": [
            {
                "camera": v.camera, "start": v.start_ts, "interval": v.interval_s,
                "declared": v.declared, "real": v.real, "source": v.source, "path": v.path,
                "reason": v.reason,
            }
            for v in inflated
        ],
        "unreadable": [
            {
                "camera": v.camera, "start": v.start_ts, "interval": v.interval_s,
                "declared": v.declared, "path": v.path,
            }
            for v in unreadable
        ],
    }
