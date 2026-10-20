"""Tests for sprite-sheet composition (scrub/tiling.py).

The sheet's whole contract is positional: cell `n` is the frame at
`start + n*interval`. Placing cells by their position in the input list broke
that silently the moment one cell file was missing.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from marcellus.scrub import tiling

CELL_W, CELL_H = 8, 4
COLS, ROWS = 4, 2


def _cell(path: Path, color: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (CELL_W, CELL_H), color=color).save(path)
    return path


def _pixel_at(sheet: Image.Image, idx: int) -> tuple[int, int, int]:
    row, col = divmod(idx, COLS)
    return sheet.convert("RGB").getpixel((col * CELL_W + 1, row * CELL_H + 1))


def test_cells_land_on_their_declared_index(tmp_path: Path) -> None:
    red, green = (255, 0, 0), (0, 255, 0)
    cells = [
        (0, _cell(tmp_path / "a.jpg", red)),
        (3, _cell(tmp_path / "b.jpg", green)),
    ]
    out = tmp_path / "sheet.jpg"
    tiling.tile_sheet(cells, cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H, out_path=out)

    with Image.open(out) as sheet:
        assert _pixel_at(sheet, 0)[0] > 200  # red stayed at 0
        assert _pixel_at(sheet, 3)[1] > 200  # green landed at 3, not at 1
        # The skipped slot is left black rather than filled by the next frame.
        assert sum(_pixel_at(sheet, 1)) < 60


def test_out_of_range_indices_are_ignored(tmp_path: Path) -> None:
    cells = [
        (0, _cell(tmp_path / "a.jpg", (255, 255, 255))),
        (COLS * ROWS, _cell(tmp_path / "over.jpg", (255, 0, 0))),
        (-1, _cell(tmp_path / "under.jpg", (0, 0, 255))),
    ]
    out = tmp_path / "sheet.jpg"
    tiling.tile_sheet(cells, cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H, out_path=out)
    with Image.open(out) as sheet:
        assert sheet.size == (COLS * CELL_W, ROWS * CELL_H)
        assert min(_pixel_at(sheet, 0)) > 200


def test_webp_variant_writes_a_webp(tmp_path: Path) -> None:
    cells = [(0, _cell(tmp_path / "a.jpg", (10, 20, 30)))]
    out = tmp_path / "sheet.webp"
    tiling.tile_sheet_webp(
        cells, cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H, out_path=out
    )
    with Image.open(out) as sheet:
        assert sheet.format == "WEBP"


# ----- CanvasCache: incremental composition must equal from-files composition -----


def _decoded(path: Path) -> bytes:
    with Image.open(path) as im:
        return im.convert("RGB").tobytes()


def test_canvas_cache_matches_from_files_composition_over_incremental_flushes(
    tmp_path: Path,
) -> None:
    store = tmp_path / "cells"
    store.mkdir()
    key: tiling.CanvasKey = ("cam", 1.0, 0.0, COLS, ROWS, CELL_W, CELL_H, "jpeg")
    cache = tiling.CanvasCache(4)
    geometry = {"cols": COLS, "rows": ROWS, "cell_w": CELL_W, "cell_h": CELL_H}

    def cell(i: int, color: tuple[int, int, int], size: tuple[int, int] | None = None) -> Path:
        p = store / f"{i:03d}.jpg"
        p.unlink(missing_ok=True)  # a replaced cell is a new file, as in the store
        Image.new("RGB", size or (CELL_W, CELL_H), color=color).save(p)
        return p

    def flush(step: int, paths: dict[int, Path]) -> int:
        cells = sorted(paths.items())
        fresh, cached = tmp_path / f"fresh{step}.jpg", tmp_path / f"cached{step}.jpg"
        tiling.tile_sheet(cells, out_path=fresh, **geometry)
        decoded = tiling.tile_sheet(
            cells, out_path=cached, cache=cache, cache_key=key, **geometry
        )
        assert _decoded(fresh) == _decoded(cached), f"flush {step} differs from the files"
        return decoded

    paths: dict[int, Path] = {i: cell(i, (40 * i, 20, 200 - 20 * i)) for i in range(3)}
    assert flush(0, paths) == 3  # cold: everything decoded
    paths[3] = cell(3, (9, 99, 199))
    paths[4] = cell(4, (200, 100, 0), size=(CELL_W * 2, CELL_H * 2))  # resized on paste
    assert flush(1, paths) == 2  # only the two new cells
    assert flush(2, paths) == 0  # nothing changed
    paths[1] = cell(1, (250, 250, 0))  # replaced
    assert flush(3, paths) == 1
    del paths[4]  # removed (e.g. a retired cell): must go back to black
    assert flush(4, paths) == 0
    paths[5] = cell(5, (1, 2, 3))  # past the hole at 4: published run would stop,
    assert flush(5, paths) == 1  # but the tiler itself places by declared index
    assert len(cache) == 1

    cache.evict(key)
    assert len(cache) == 0
    assert flush(6, paths) == len(paths)  # evicted -> rebuilt from files


def test_canvas_cache_is_lru_bounded_and_off_at_zero(tmp_path: Path) -> None:
    geometry = {"cols": COLS, "rows": ROWS, "cell_w": CELL_W, "cell_h": CELL_H}
    cells = [(0, _cell(tmp_path / "a.jpg", (1, 2, 3)))]
    cache = tiling.CanvasCache(2)
    for n in range(3):
        key: tiling.CanvasKey = ("cam", 1.0, float(n), COLS, ROWS, CELL_W, CELL_H, "jpeg")
        tiling.tile_sheet(
            cells, out_path=tmp_path / f"o{n}.jpg", cache=cache, cache_key=key, **geometry
        )
    assert len(cache) == 2
    cache.evict_range("cam", 1.0, 0.0, 1.0)
    assert len(cache) == 1  # sheet 2 remains

    off = tiling.CanvasCache(0)
    n = tiling.tile_sheet(
        cells, out_path=tmp_path / "off.jpg", cache=off, cache_key=key, **geometry
    )
    assert n == 1 and len(off) == 0
