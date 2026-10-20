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

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

#: (cell index within the sheet, image path on disk)
Cell = tuple[int, Path]

#: Identifies one version of a cell file: path plus inode and mtime (and size,
#: for filesystems with coarse mtimes). Cells are written once and never edited
#: in place -- a replaced or re-created cell is a new file -- so a changed
#: identity is exactly "this slot needs pasting again".
CellId = tuple[str, int, int, int]

#: (camera, interval_s, sheet_start, cols, rows, cell_w, cell_h, fmt)
CanvasKey = tuple[str, float, float, int, int, int, int, str]


def _paste_cell(
    canvas: Image.Image, idx: int, path: Path, *, cols: int, cell_w: int, cell_h: int
) -> None:
    with Image.open(path) as raw:
        im = raw.convert("RGB")
        if im.size != (cell_w, cell_h):
            im = im.resize((cell_w, cell_h))
        row, col = divmod(idx, cols)
        canvas.paste(im, (col * cell_w, row * cell_h))


def _new_canvas(cols: int, rows: int, cell_w: int, cell_h: int) -> Image.Image:
    return Image.new("RGB", (cols * cell_w, rows * cell_h), color=(0, 0, 0))


def _compose(
    cells: Sequence[Cell], *, cols: int, rows: int, cell_w: int, cell_h: int
) -> Image.Image:
    canvas = _new_canvas(cols, rows, cell_w, cell_h)
    for idx, path in cells:
        if not 0 <= idx < cols * rows:
            continue
        _paste_cell(canvas, idx, path, cols=cols, cell_w=cell_w, cell_h=cell_h)
    return canvas


def _cell_id(path: Path) -> CellId:
    st = os.stat(path)
    return (str(path), st.st_ino, st.st_mtime_ns, st.st_size)


@dataclass
class _CanvasEntry:
    canvas: Image.Image
    #: slot index -> identity of the cell file currently pasted there.
    slots: dict[int, CellId] = field(default_factory=dict)
    #: Held while composing *and* encoding: the canvas is mutated in place, so
    #: two flushes of one sheet must never overlap. The generator is serial per
    #: camera, so this is belt and braces rather than contention.
    lock: threading.Lock = field(default_factory=threading.Lock)


class CanvasCache:
    """Bounded LRU of in-progress sheet canvases, so a flush of a still-growing
    sheet decodes only the cells that are new or changed since its last flush
    instead of every cell in it, then encodes once.

    A canvas is ~37 MB at the prod geometry (5760x2160 RGB), so the bound is a
    count of sheets (`scrub.canvas_cache_sheets`), 0 meaning off. Output is
    pixel-identical to composing from the files: same paste positions, same
    black background, and a slot that has lost its cell is painted black again.
    """

    def __init__(self, max_sheets: int = 0) -> None:
        self.max_sheets = max_sheets
        self._entries: OrderedDict[CanvasKey, _CanvasEntry] = OrderedDict()
        self._lock = threading.Lock()

    def set_capacity(self, max_sheets: int) -> None:
        with self._lock:
            self.max_sheets = max(0, max_sheets)
            self._trim()

    def __len__(self) -> int:
        return len(self._entries)

    def _trim(self) -> None:
        while len(self._entries) > self.max_sheets:
            self._entries.popitem(last=False)

    def evict(self, key: CanvasKey) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def evict_range(self, camera: str, interval_s: float, start: float, end: float) -> None:
        """Drop every canvas of this camera+tier whose sheet starts in
        `[start, end]` -- a sealed bucket takes no more cells."""
        with self._lock:
            for key in [
                k for k in self._entries
                if k[0] == camera and k[1] == interval_s and start <= k[2] <= end
            ]:
                del self._entries[key]

    def _entry(self, key: CanvasKey) -> _CanvasEntry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                _, _, _, cols, rows, cell_w, cell_h, _ = key
                entry = _CanvasEntry(_new_canvas(cols, rows, cell_w, cell_h))
                self._entries[key] = entry
            self._entries.move_to_end(key)
            self._trim()
            return entry

    def compose_and_save(
        self,
        key: CanvasKey,
        cells: Sequence[Cell],
        *,
        save: Callable[[Image.Image], None],
        cols: int,
        rows: int,
        cell_w: int,
        cell_h: int,
    ) -> int:
        """Bring the cached canvas for `key` up to date with `cells`, call
        `save(canvas)` while still holding the sheet's lock, and return how many
        cells were actually decoded."""
        entry = self._entry(key)
        decoded = 0
        with entry.lock:
            wanted: dict[int, tuple[Path, CellId]] = {}
            for idx, path in cells:
                if 0 <= idx < cols * rows:
                    wanted[idx] = (path, _cell_id(path))
            for idx in [i for i in entry.slots if i not in wanted]:
                row, col = divmod(idx, cols)
                entry.canvas.paste(
                    (0, 0, 0),
                    (col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h),
                )
                del entry.slots[idx]
            try:
                for idx, (path, ident) in wanted.items():
                    if entry.slots.get(idx) == ident:
                        continue
                    # Forget the slot first: a decode that fails half-way must
                    # not leave an identity claiming pixels that were not pasted.
                    entry.slots.pop(idx, None)
                    _paste_cell(entry.canvas, idx, path, cols=cols, cell_w=cell_w, cell_h=cell_h)
                    entry.slots[idx] = ident
                    decoded += 1
                save(entry.canvas)
            except BaseException:
                # Don't trust a canvas that may be half-updated.
                self.evict(key)
                raise
        return decoded


def _tile(
    cells: Sequence[Cell],
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    cache: CanvasCache | None,
    cache_key: CanvasKey | None,
    save: Callable[[Image.Image], None],
) -> int:
    if cache is not None and cache_key is not None and cache.max_sheets > 0:
        return cache.compose_and_save(
            cache_key, cells, save=save, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h
        )
    save(_compose(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h))
    return sum(1 for idx, _ in cells if 0 <= idx < cols * rows)


def tile_sheet(
    cells: Sequence[Cell],
    *,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    out_path: Path,
    quality: int = 85,
    cache: CanvasCache | None = None,
    cache_key: CanvasKey | None = None,
) -> int:
    """Paste already-scaled frames into a row-major montage at their own cell
    indices and write it as JPEG. Never invents a cell for a missing index --
    callers only pass cells they actually have (never a placeholder cell, §4.2).

    With `cache` and `cache_key`, only cells new or changed since this sheet's
    last call are decoded (see `CanvasCache`). Returns the number of cells
    decoded either way.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _save(canvas: Image.Image) -> None:
        canvas.save(out_path, format="JPEG", quality=quality)

    return _tile(cells, cols, rows, cell_w, cell_h, cache, cache_key, _save)


def tile_sheet_webp(
    cells: Sequence[Cell],
    *,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    out_path: Path,
    quality: int = 75,
    cache: CanvasCache | None = None,
    cache_key: CanvasKey | None = None,
) -> int:
    """WebP variant (§5.3 opt-in alt format). `-lossless 0` mandatory --
    Pillow's default `lossless=False` already gives lossy encoding, but this
    is spelled out because omitting the equivalent ffmpeg flag on this same
    codec silently produced a near-lossless ~1MB file in the spec's own
    measurement (M4). Never flip `lossless=True` here without re-measuring.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _save(canvas: Image.Image) -> None:
        canvas.save(out_path, format="WEBP", quality=quality, lossless=False)

    return _tile(cells, cols, rows, cell_w, cell_h, cache, cache_key, _save)
