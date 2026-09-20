"""Repair of sheets published before the count-from-cells rule (scrub/repair.py).

The generator can no longer publish a count its cell store can't back, but a
sheet whose span has already passed is never republished -- so the fix alone
does not clean up what is already in the index. These cover both routes the
repair uses to establish the truth: the cell store when it survives, the
published pixels when it doesn't.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from marcellus import db
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.scrub import generator, grid, repair, tiling

COLS, ROWS = 4, 3
CELL_W, CELL_H = 32, 18


@pytest.fixture
def env(tmp_path: Path) -> Settings:
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT NOT NULL, "
        "path TEXT NOT NULL, start_time REAL NOT NULL, end_time REAL NOT NULL, "
        "duration REAL NOT NULL, segment_size REAL NOT NULL);"
    )
    conn.commit()
    conn.close()
    return Settings(
        frigate=FrigateSection(config_path=tmp_path / "cfg.yml", db_path=frigate_db),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            sheet_cols=COLS, sheet_rows=ROWS, cell_w=CELL_W, cell_h=CELL_H,
        ),
    )


def _cell(colour: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (CELL_W, CELL_H), color=colour)


def _publish_overclaiming(
    env: Settings, *, start_ts: float, interval_s: float, declared: int,
    present: list[int], keep_cells: bool, complete: bool = False,
) -> str:
    """Write the sheet an over-claiming publish used to produce: an image tiled
    from `present` only, and a row declaring `declared`."""
    cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
    cells_dir.mkdir(parents=True, exist_ok=True)
    for idx in present:
        # Bright, and varied per cell so nothing depends on a single colour.
        _cell((200, 40 + idx * 3, 90)).save(cells_dir / f"{idx:03d}.jpg", quality=90)
    rel = grid.sheet_rel_path("doorbell", interval_s, start_ts, declared, ".jpg")
    out = env.scrub.cache_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    tiling.tile_sheet(
        [(i, cells_dir / f"{i:03d}.jpg") for i in present],
        cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H, out_path=out,
    )
    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        db.upsert_scrub_sheet(
            conn, camera="doorbell", start_ts=start_ts, interval_s=interval_s,
            cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H,
            count=declared, path=rel, complete=complete,
        )
        conn.commit()
    finally:
        conn.close()
    if not keep_cells:
        generator._drop_cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
    return rel


def test_verify_reads_the_cell_store_when_it_survives(env: Settings) -> None:
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=6,
        present=[0, 1, 2, 4, 5], keep_cells=True,
    )
    verdicts = repair.verify_sheets(env)
    assert len(verdicts) == 1
    assert (verdicts[0].declared, verdicts[0].real) == (6, 3)
    assert verdicts[0].source == "cells", "no decode should be needed while cells exist"


def test_verify_falls_back_to_the_pixels_when_the_cell_store_is_gone(env: Settings) -> None:
    """The case that matters for existing data: a sheet sealed long ago, whose
    cells were dropped, still has to be checkable."""
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=6,
        present=[0, 1, 2, 4, 5], keep_cells=False,
    )
    verdicts = repair.verify_sheets(env)
    assert len(verdicts) == 1
    assert (verdicts[0].declared, verdicts[0].real) == (6, 3)
    assert verdicts[0].source == "pixels"


def test_verify_passes_an_honest_sheet(env: Settings) -> None:
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=4,
        present=[0, 1, 2, 3], keep_cells=False,
    )
    assert repair.verify_sheets(env) == []


def test_repair_republishes_at_the_true_count_and_retires_the_lie(env: Settings) -> None:
    inflated = _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=6,
        present=[0, 1, 2, 4, 5], keep_cells=False,
    )
    result = repair.verify_and_repair(env, repair=True)
    assert result["overclaiming_sheets"] == 1
    assert result["cells_falsely_claimed"] == 3
    assert result["repaired"] == 1

    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()
    assert len(sheets) == 1
    assert sheets[0]["count"] == 3
    assert Path(sheets[0]["path"]).name == grid.sheet_filename(
        1_800_000_000.0, 60.0, 3, ".jpg"
    )
    assert (env.scrub.cache_dir / sheets[0]["path"]).exists()
    assert not (env.scrub.cache_dir / inflated).exists(), "the lie is still servable"
    # Repair is idempotent: a second pass finds nothing left to do.
    assert repair.verify_and_repair(env, repair=True)["overclaiming_sheets"] == 0


def test_repair_removes_a_sheet_with_no_real_cells_at_all(env: Settings) -> None:
    """An all-black sheet declares coverage for a span the cache never sampled;
    there is no honest count to republish it under."""
    rel = _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=4,
        present=[], keep_cells=False,
    )
    result = repair.verify_and_repair(env, repair=True)
    assert (result["repaired"], result["removed"]) == (0, 1)

    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        assert db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000) == []
    finally:
        conn.close()
    assert not (env.scrub.cache_dir / rel).exists()


def test_dark_but_real_footage_is_not_mistaken_for_padding(env: Settings) -> None:
    """Night frames are the false-positive risk in the pixel path.

    A camera in near-darkness still carries sensor noise; the tiler's padding is
    a flat zero. The threshold has to sit between them, and this is the side of
    it that costs the user real coverage if it drifts.
    """
    start_ts, interval_s = 1_800_000_000.0, 60.0
    cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
    cells_dir.mkdir(parents=True, exist_ok=True)
    dark = Image.new("RGB", (CELL_W, CELL_H))
    dark.putdata([(4, 3, 5) if (x + y) % 3 else (2, 2, 3)
                  for y in range(CELL_H) for x in range(CELL_W)])
    for idx in range(4):
        dark.save(cells_dir / f"{idx:03d}.jpg", quality=90)
    rel = grid.sheet_rel_path("doorbell", interval_s, start_ts, 4, ".jpg")
    out = env.scrub.cache_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    tiling.tile_sheet(
        [(i, cells_dir / f"{i:03d}.jpg") for i in range(4)],
        cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H, out_path=out,
    )
    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        db.upsert_scrub_sheet(
            conn, camera="doorbell", start_ts=start_ts, interval_s=interval_s,
            cols=COLS, rows=ROWS, cell_w=CELL_W, cell_h=CELL_H,
            count=4, path=rel, complete=False,
        )
        conn.commit()
    finally:
        conn.close()
    generator._drop_cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)

    assert repair.verify_sheets(env) == [], "dark real footage was read as padding"


def test_verify_can_be_scoped_to_one_camera_and_tier(env: Settings) -> None:
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=6,
        present=[0, 1], keep_cells=True,
    )
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=10.0, declared=6,
        present=[0, 1], keep_cells=True,
    )
    assert len(repair.verify_sheets(env)) == 2
    assert len(repair.verify_sheets(env, interval=60.0)) == 1
    assert repair.verify_sheets(env, camera="nonesuch") == []


def test_repair_leaves_a_larger_version_published_after_the_scan(env: Settings) -> None:
    """Repair may run against a live deployment.

    A sheet whose span is still filling can gain a legitimate larger version
    between the scan and the fix. Retiring "everything above the true count"
    would delete that one; only the version the verdict actually proved false
    may go.
    """
    _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=6,
        present=[0, 1, 2, 4, 5], keep_cells=True,
    )
    verdicts = repair.verify_sheets(env)
    assert len(verdicts) == 1

    # The generator fills the hole and publishes a complete, honest version.
    cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", 60.0, 1_800_000_000.0)
    for idx in range(COLS * ROWS):
        _cell((10, 200, 30)).save(cells_dir / f"{idx:03d}.jpg", quality=90)
    newer = _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=COLS * ROWS,
        present=list(range(COLS * ROWS)), keep_cells=True, complete=True,
    )

    repair.repair_sheet(env, verdicts[0])

    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        counts = sorted(
            r["count"] for r in conn.execute("SELECT count FROM scrub_sheets")
        )
    finally:
        conn.close()
    assert COLS * ROWS in counts, "repair deleted a version published after the scan"
    assert 6 not in counts, "the over-claiming version survived"
    assert (env.scrub.cache_dir / newer).exists()


def test_verify_reports_a_truncated_sheet_as_unreadable_when_cells_survive(
    env: Settings,
) -> None:
    rel = _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=4,
        present=[0, 1, 2, 3], keep_cells=True,
    )
    (env.scrub.cache_dir / rel).write_bytes(b"\x00" * 100)  # truncate in place

    verdicts = repair.verify_sheets(env)
    assert len(verdicts) == 1
    assert verdicts[0].reason == "unreadable"

    result = repair.verify_and_repair(env, repair=True)
    assert result["unreadable_sheets"] == 1
    assert result["repaired"] == 1
    assert result["removed"] == 0

    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()
    assert len(sheets) == 1
    assert sheets[0]["count"] == 4  # cells backed all 4 -> re-rendered whole
    with Image.open(env.scrub.cache_dir / sheets[0]["path"]) as im:
        im.load()  # the repaired file must actually decode
    assert repair.verify_and_repair(env)["unreadable_sheets"] == 0


def test_verify_removes_a_truncated_sheet_when_cells_are_gone(env: Settings) -> None:
    rel = _publish_overclaiming(
        env, start_ts=1_800_000_000.0, interval_s=60.0, declared=4,
        present=[0, 1, 2, 3], keep_cells=False,
    )
    (env.scrub.cache_dir / rel).write_bytes(b"\x00" * 100)  # truncate in place

    verdicts = repair.verify_sheets(env)
    assert len(verdicts) == 1
    assert verdicts[0].reason == "unreadable"

    result = repair.verify_and_repair(env, repair=True)
    assert result["unreadable_sheets"] == 1
    assert (result["repaired"], result["removed"]) == (0, 1)

    conn = db.open_sidecar(env.sidecar.db_path)
    try:
        assert db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000) == []
    finally:
        conn.close()
    assert not (env.scrub.cache_dir / rel).exists()
    assert repair.verify_and_repair(env)["unreadable_sheets"] == 0
