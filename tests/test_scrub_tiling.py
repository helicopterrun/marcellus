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
