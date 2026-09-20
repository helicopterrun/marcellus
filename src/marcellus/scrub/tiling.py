"""Compose per-frame JPEGs into a row-major sprite sheet (§5.3).

Uses Pillow rather than a second ffmpeg pass for the live/still-filling
sheet, which needs to be re-tiled every generation cycle with a variable cell
count -- exactly the case §5.3 recommends Pillow for.

Cells are placed by their **declared index**, never by their position in the
list. Placing by position meant one missing cell file silently shifted every
later frame one slot earlier, so the sheet's cell->timestamp mapping -- the
whole contract a client reads it through -- came out wrong from the hole
onward, with nothing in the response to signal it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PIL import Image

#: (cell index within the sheet, image path on disk)
Cell = tuple[int, Path]


def _compose(
    cells: Sequence[Cell], *, cols: int, rows: int, cell_w: int, cell_h: int
) -> Image.Image:
    canvas = Image.new("RGB", (cols * cell_w, rows * cell_h), color=(0, 0, 0))
    for idx, path in cells:
        if not 0 <= idx < cols * rows:
            continue
        with Image.open(path) as raw:
            im = raw.convert("RGB")
            if im.size != (cell_w, cell_h):
                im = im.resize((cell_w, cell_h))
            row, col = divmod(idx, cols)
            canvas.paste(im, (col * cell_w, row * cell_h))
    return canvas


def tile_sheet(
    cells: Sequence[Cell],
    *,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    out_path: Path,
    quality: int = 85,
) -> None:
    """Paste already-scaled frames into a row-major montage at their own cell
    indices and write it as JPEG. Never invents a cell for a missing index --
    callers only pass cells they actually have (never a placeholder cell, §4.2).
    """
    canvas = _compose(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="JPEG", quality=quality)


def tile_sheet_webp(
    cells: Sequence[Cell],
    *,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    out_path: Path,
    quality: int = 75,
) -> None:
    """WebP variant (§5.3 opt-in alt format). `-lossless 0` mandatory --
    Pillow's default `lossless=False` already gives lossy encoding, but this
    is spelled out because omitting the equivalent ffmpeg flag on this same
    codec silently produced a near-lossless ~1MB file in the spec's own
    measurement (M4). Never flip `lossless=True` here without re-measuring.
    """
    canvas = _compose(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="WEBP", quality=quality, lossless=False)
