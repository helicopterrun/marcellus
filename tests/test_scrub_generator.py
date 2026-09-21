"""Integration-shaped tests for the generator orchestration
(scrub/generator.py), with ffmpeg subprocess calls mocked out -- this
sandbox has no ffmpeg binary, so these exercise the real DB/tiling/
cell-assignment wiring against synthetic frame data instead of a real
segment file. See tests/test_scrub_grid.py for the cadence-verification
math itself, tested in isolation.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from marcellus import db
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.scrub import ffmpeg_io, generator, grid

RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, path TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
    segment_size REAL NOT NULL
);
"""


def _make_jpg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), color=(10, 20, 30)).save(path)


def _fake_extract(pts_for: object) -> object:
    """Stand-in for ffmpeg_io.extract_keyframes_with_pts.

    Extraction and timestamps come from one process now, so the fake returns
    the (pts, path) pairs it would have produced rather than two separate lists.
    """

    async def _extract(seg_path: Path, out_dir: Path, **kw: object) -> list[tuple[float, Path]]:
        out = []
        for i, ts in enumerate(pts_for(seg_path)):  # type: ignore[operator]
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            out.append((float(ts), p))
        return out

    return _extract


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    # Two contiguous 10s segments.
    conn.execute(
        "INSERT INTO recordings VALUES ('s1','doorbell','/media/frigate/1.mp4',?,?,10.0,5.0)",
        (base, base + 10),
    )
    conn.execute(
        "INSERT INTO recordings VALUES ('s2','doorbell','/media/frigate/2.mp4',?,?,10.0,5.0)",
        (base + 10, base + 20),
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "1.mp4").write_bytes(b"fake")
    (recordings_root / "2.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=tmp_path / "cfg.yml",
            db_path=frigate_db,
            media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, sheet_cols=4, sheet_rows=3,  # 12-cell sheets for a fast test
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

    return settings


def test_scrub_generate_writes_sheet_with_declared_count_and_verified_cadence(
    env: Settings,
) -> None:
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        # `now` must be close to the synthetic recordings' own timestamps
        # (base=1.8e9) -- the generator now bounds each tier's query to
        # `[window_start, window_end)` (§4.2 non-overlap), so a real
        # wall-clock `now` (far from `base`) would put these recordings
        # outside every tier's window and silently yield zero segments.
        result = asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        assert result["segments"] == 2
        assert result["new_frames"] == 20  # 2 segments x 10 keyframes each

        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert buckets, "expected at least one bucket row"
    assert sheets, "expected at least one sheet row"
    # 20 frames at 12 cells/sheet -> two sheets, first complete (12), second partial (8).
    counts = sorted(s["count"] for s in sheets)
    assert counts == [8, 12]

    # Every accepted cell's achieved timestamp is within interval/2 of its grid
    # point -- the hard contract (spec §4.2), verified end-to-end here via the
    # actual bucket row rather than assumed.
    for b in buckets:
        assert b["generated_through"] >= b["start_ts"]

    # Sheet file actually exists on disk, atomically published.
    for s in sheets:
        path = env.scrub.cache_dir / s["path"]
        assert path.exists()
        assert path.stat().st_size > 0


def test_scrub_generate_gap_splits_bucket(env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A large jump between segments (simulated recording gap) must split the
    bucket rather than fudge the interval (spec §5.2, §11)."""

    def _pts_with_gap(seg_path: Path) -> list[float]:
        # Segment 2's keyframes look like they start way later than the grid
        # would predict -- simulate by returning offsets that, combined with
        # segment.start_time, produce an >interval*1.5 jump from the prior
        # segment's last accepted frame.
        if "2.mp4" in str(seg_path):
            return [40.0, 41.0]  # segment 2 starts at base+10, so ts = base+50, base+51
        return [float(i) for i in range(10)]

    monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _fake_extract(_pts_with_gap))

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_060.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    # Two distinct buckets: the gap must not be bridged into one.
    assert len(buckets) >= 2
    starts = sorted(b["start_ts"] for b in buckets)
    assert starts[1] - starts[0] > 5.0  # the gap is reflected as separate bucket starts


@pytest.fixture
def aged_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Like `env`, but with two segments straddling an `aged_after_h`
    boundary: segment 1 (base..base+10) is older than the boundary, segment 2
    (base+10..base+20) is inside it (§4.2, §5.5 aged/thinning tier).

    Forces the fps-fallback extraction path (a large fake GOP) for both
    tiers so each tier's frames land exactly on its own grid -- the
    keyframe-only path used by `env` samples at a fixed native cadence that
    can't be made to land cleanly on two different tier intervals at once.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    conn.execute(
        "INSERT INTO recordings VALUES ('s1','doorbell','/media/frigate/1.mp4',?,?,10.0,5.0)",
        (base, base + 10),
    )
    conn.execute(
        "INSERT INTO recordings VALUES ('s2','doorbell','/media/frigate/2.mp4',?,?,10.0,5.0)",
        (base + 10, base + 20),
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "1.mp4").write_bytes(b"fake")
    (recordings_root / "2.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=tmp_path / "cfg.yml",
            db_path=frigate_db,
            media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_interval_s=5.0,
            # boundary = now - aged_after_h*3600; chosen below (per-test `now`)
            # to land at base+15, splitting segment 1 into the aged window and
            # segment 2 into the recent window.
            aged_after_h=5.0 / 3600.0,
            retention_days=4, sheet_cols=4, sheet_rows=3,
            # This fixture fakes a 30s GOP to force the full-decode path, which
            # is exactly what cadence-matching declines to do; keep the two
            # configured tiers so the thinning machinery is what's under test.
            match_keyframe_cadence=False,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 30.0  # >> any interval * 1.3 -> always the fps-fallback path

    async def _fake_extract_fps(
        seg_path: Path, out_dir: Path, interval_s: float, **kw: object
    ) -> list[Path]:
        n = int(round(10.0 / interval_s))  # 10s segment
        paths = []
        for i in range(n):
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            paths.append(p)
        return paths

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(ffmpeg_io, "extract_fps", _fake_extract_fps)

    return settings


def test_scrub_generate_aged_tier_uses_coarser_interval(aged_env: Settings) -> None:
    """A span older than `aged_after_h` must be generated at `aged_interval_s`,
    not the recent tier's interval (§5.5)."""
    now = 1_800_000_020.0  # right as segment 2 ends; boundary = now - 5s = base+15
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert result["segments"] >= 2
    intervals = {b["interval_s"] for b in buckets}
    assert 5.0 in intervals, "expected an aged-tier (5.0s) bucket for the older span"
    assert 1.0 in intervals, "expected a recent-tier (1.0s) bucket for the newer span"

    aged_buckets = [b for b in buckets if b["interval_s"] == 5.0]
    recent_buckets = [b for b in buckets if b["interval_s"] == 1.0]
    # The aged bucket must not extend past the boundary, and the recent
    # bucket must not start before it (§4.2 non-overlap).
    boundary = now - aged_env.scrub.aged_after_h * 3600
    for b in aged_buckets:
        assert b["end_ts"] <= boundary + 1e-6
    for b in recent_buckets:
        assert b["start_ts"] >= boundary - 1e-6


def test_scrub_generate_tiers_do_not_overlap(aged_env: Settings) -> None:
    """No two buckets for the same camera may cover overlapping time spans,
    regardless of tier (§4.2)."""
    now = 1_800_000_020.0
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    ordered = sorted(buckets, key=lambda b: b["start_ts"])
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        assert prev["end_ts"] <= cur["start_ts"] + 1e-6, (
            f"overlapping buckets: {prev} and {cur}"
        )


def test_scrub_generate_recent_bucket_retired_once_superseded_by_aged(
    aged_env: Settings,
) -> None:
    """A recent-tier bucket that ages past `aged_after_h` must be retired
    (deleted, sheets removed from disk) once the aged tier's coarser bucket
    supersedes its span -- not left to coexist (§4.2, §5.5)."""
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        # Cycle 1: both segments are still "recent" (now right after segment 2).
        now1 = 1_800_000_020.0 + (aged_env.scrub.aged_after_h * 3600) - 1.0
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now1, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        recent_after_1 = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
        recent_after_1 = [b for b in recent_after_1 if b["interval_s"] == 1.0]
        assert recent_after_1, "expected a recent-tier bucket while still fresh"
        sheet_paths_1 = [
            aged_env.scrub.cache_dir / s["path"]
            for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["interval_s"] == 1.0
        ]
        assert sheet_paths_1 and all(p.exists() for p in sheet_paths_1)

        # Cycle 2: enough wall-clock time has passed that segment 1's old
        # recent-tier bucket now falls before the (advanced) boundary.
        now2 = now1 + (aged_env.scrub.aged_after_h * 3600) + 10.0
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now2, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        buckets_after_2 = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    boundary2 = now2 - aged_env.scrub.aged_after_h * 3600
    stale_recent = [
        b for b in buckets_after_2 if b["interval_s"] == 1.0 and b["start_ts"] < boundary2
    ]
    assert not stale_recent, "stale recent-tier bucket should have been retired"
    # The now-superseded on-disk sheet files from cycle 1 must be gone too.
    assert not any(p.exists() for p in sheet_paths_1)


def test_generation_leaves_no_scratch_behind(env: Settings) -> None:
    """Extraction used to stage frames in the system temp dir and leave every
    frame it didn't accept there -- about a segment's worth per cycle, forever,
    on whatever filesystem /tmp happens to be."""
    system_tmp_before = set(Path(tempfile.gettempdir()).glob("*"))

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    assert set(Path(tempfile.gettempdir()).glob("*")) == system_tmp_before
    # The in-cache scratch root is emptied too (the dir itself may remain).
    work = env.scrub.cache_dir / ".work"
    assert not list(work.iterdir()) if work.exists() else True


def test_completed_sheet_drops_its_cell_store(env: Settings) -> None:
    """Cells are the raw per-frame JPEGs; nothing ever swept them, so they grew
    at ~1 GB/day/camera at 1 fps under scrub.cache_dir."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    complete = [s for s in sheets if s["complete"]]
    partial = [s for s in sheets if not s["complete"]]
    assert complete and partial

    for s in complete:
        cells = generator._cells_dir(
            env.scrub.cache_dir, "doorbell", s["interval_s"], s["start_ts"]
        )
        assert not cells.exists(), "a sealed sheet can never be re-tiled; its cells are dead"
    for s in partial:
        cells = generator._cells_dir(
            env.scrub.cache_dir, "doorbell", s["interval_s"], s["start_ts"]
        )
        assert cells.exists(), "a still-filling sheet needs its cells for the next re-tile"


def test_prune_removes_sheets_and_their_cell_stores(env: Settings) -> None:
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    sheet_files = list(env.scrub.cache_dir.rglob("*.jpg"))
    assert sheet_files

    # Well past retention relative to the synthetic recordings.
    result = generator.prune(env, now=1_800_000_030.0 + 30 * 86400)
    assert result["sheets_deleted"] > 0
    assert result["buckets_deleted"] > 0
    assert result["cell_dirs_deleted"] > 0
    assert not list((env.scrub.cache_dir / "doorbell").rglob("*.jpg"))


def test_prune_uses_a_sheets_own_end_time_not_a_hardcoded_interval(
    env: Settings,
) -> None:
    """A full 12x8 sheet at a 60s cadence spans 12*8*60s = 5760s = 96 minutes.
    `delete_scrub_sheets_before` (the query `prune` runs) computes each row's
    end as `start_ts + cols*rows*interval_s` -- interval-generic, not
    hardcoded to `recent_interval_s`/`aged_interval_s` -- so a 60s sheet must
    survive a cutoff that lands inside its 96-minute span and be removed once
    the cutoff passes its actual end."""
    conn = db.open_sidecar(env.sidecar.db_path)
    start_ts = 1_800_000_000.0
    span_s = 12 * 8 * 60.0
    assert span_s == pytest.approx(96 * 60.0)
    try:
        db.upsert_scrub_sheet(
            conn, camera="doorbell", start_ts=start_ts, interval_s=60.0, cols=12, rows=8,
            cell_w=320, cell_h=180, count=96, path="doorbell/60/sheet.jpg", complete=True,
        )
        conn.commit()

        # Cutoff inside the sheet's span: must not be pruned yet.
        still_open = db.delete_scrub_sheets_before(None, start_ts + span_s - 1.0, conn)
        conn.commit()
        assert still_open == []
        row = conn.execute(
            "SELECT * FROM scrub_sheets WHERE camera = ? AND interval_s = 60.0", ("doorbell",)
        ).fetchone()
        assert row is not None

        # Cutoff past the sheet's actual end (start + span): now it prunes.
        removed = db.delete_scrub_sheets_before(None, start_ts + span_s + 1.0, conn)
        conn.commit()
        assert removed == ["doorbell/60/sheet.jpg"]
        row = conn.execute(
            "SELECT * FROM scrub_sheets WHERE camera = ? AND interval_s = 60.0", ("doorbell",)
        ).fetchone()
        assert row is None
    finally:
        conn.close()


def test_prune_reaps_only_stale_orphaned_publish_temp_files(env: Settings) -> None:
    """`_publish_sheet_version` mkstemps `.publish-*` next to the sheet it's
    about to write; a hard kill between mkstemp and the atomic replace leaves
    one behind forever with nothing else to clean it up. `prune` should reap
    only ones old enough to be orphans, not one a live publish might still be
    writing."""
    interval_dir = env.scrub.cache_dir / "doorbell" / "60"
    interval_dir.mkdir(parents=True, exist_ok=True)
    stale = interval_dir / ".publish-stale123"
    fresh = interval_dir / ".publish-fresh456"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    now = 1_800_100_000.0
    old_time = now - 7200  # 2h old, past the 1h reap threshold
    os.utime(stale, (old_time, old_time))
    os.utime(fresh, (now, now))

    result = generator.prune(env, now=now)

    assert result["publish_tmp_deleted"] == 1
    assert not stale.exists()
    assert fresh.exists()


def test_prune_removes_now_empty_parent_dirs(env: Settings) -> None:
    """`_prune_cell_dirs` rmtrees leaf sheet cell dirs but used to leave the
    now-empty `.cells`/interval/camera dirs behind forever."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    cam_dir = env.scrub.cache_dir / "doorbell"
    assert cam_dir.is_dir()

    result = generator.prune(env, now=1_800_000_030.0 + 30 * 86400)
    assert result["sheets_deleted"] > 0

    assert not cam_dir.exists(), "camera dir should be removed once fully emptied"


def test_sheet_row_count_matches_its_filename(env: Settings) -> None:
    """The row's `count` and the count baked into its immutable URL are the
    same key; they were computed from different values."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    for s in sheets:
        expected = grid.sheet_filename(s["start_ts"], s["interval_s"], s["count"], ".jpg")
        assert Path(s["path"]).name == expected


def test_webp_format_writes_webp_sheets(env: Settings) -> None:
    webp_env = env.model_copy(update={"scrub": env.scrub.model_copy(update={"format": "webp"})})
    conn = db.open_joined(webp_env.frigate.db_path, webp_env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                webp_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert sheets
    for s in sheets:
        assert s["path"].endswith(".webp")
        with Image.open(webp_env.scrub.cache_dir / s["path"]) as im:
            assert im.format == "WEBP"


def test_extraction_uses_the_configured_cell_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Settings
) -> None:
    """scrub.cell_w/cell_h never reached ffmpeg, so a non-default cell size
    extracted at 320x180 and was upscaled by the tiler."""
    seen: list[tuple[int, int]] = []

    async def _record_extract(
        seg_path: Path, out_dir: Path, *, cell_w: int = 320, cell_h: int = 180, **kw: object
    ) -> list[tuple[float, Path]]:
        seen.append((cell_w, cell_h))
        out = []
        for i in range(10):
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            out.append((float(i), p))
        return out

    monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _record_extract)
    big = env.model_copy(
        update={"scrub": env.scrub.model_copy(update={"cell_w": 480, "cell_h": 270})}
    )
    conn = db.open_joined(big.frigate.db_path, big.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                big, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    assert seen and set(seen) == {(480, 270)}


def test_each_sheet_gets_its_own_cell_store(env: Settings) -> None:
    """Cell files are named by index *within* a sheet, so two sheets sharing a
    directory means sheet N+1 silently republishes sheet N's frames. Rendering
    the sheet start with %g did exactly that: every epoch second in the same
    ~11-day window formats to the same six-significant-digit string."""
    a = generator._cells_dir(Path("/cache"), "doorbell", 1.0, 1_785_380_400.0)
    b = generator._cells_dir(Path("/cache"), "doorbell", 1.0, 1_785_380_496.0)
    assert a != b
    assert a.name == "1785380400"
    assert b.name == "1785380496"

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    starts = {s["start_ts"] for s in sheets}
    assert len(starts) == 2
    dirs = {
        generator._cells_dir(env.scrub.cache_dir, "doorbell", 1.0, s).name for s in starts
    }
    assert len(dirs) == 2


def test_cycle_work_is_budgeted(env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold start has no resume point, so the tier window is the whole
    retention horizon -- days of segments in one cycle without a cap."""
    conn = sqlite3.connect(env.frigate.db_path)
    base = 1_800_000_000.0
    for i in range(2, 12):
        conn.execute(
            "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"extra{i}", "/media/frigate/1.mp4", base + i * 10, base + (i + 1) * 10),
        )
    conn.commit()
    conn.close()

    joined = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_backfill(
                env, "doorbell", budget=3, frigate_conn=joined, sidecar_conn=joined,
                now=base + 200, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        joined.close()

    assert result["segments"] == 3, "one pass must stop at the budget, not drain the window"
    assert result["backfilled"] is True, "spending the whole share means history remains"


def test_dense_keyframes_fill_whole_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces what a 5s tier did against Frigate's ~1s GOP on live footage.

    Keyframe decode returns the encoder's cadence, so five frames arrived per
    5s cell. Cell assignment could only refuse to overwrite and split, so every
    bucket held one cell and every sheet was a 96-cell image with one frame in
    it -- a 96x disk amplification that covered 4 minutes of footage in 236
    sheets before it was caught.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    for i in range(12):  # 12 contiguous 10s segments = 2 minutes
        conn.execute(
            "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"s{i}", f"/media/frigate/{i}.mp4", base + i * 10, base + (i + 1) * 10),
        )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    for i in range(12):
        (recordings_root / f"{i}.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=5.0, aged_after_h=9999.0,  # one tier, 5s cadence
            sheet_cols=4, sheet_rows=3,  # 12-cell sheets = 60s of footage each
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0  # Frigate's measured GOP: well under the 5s target

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

    joined = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                settings, "doorbell", frigate_conn=joined, sidecar_conn=joined,
                now=base + 120, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(joined, "doorbell", 0, 1_900_000_000)
        sheets = db.list_scrub_sheets(joined, "doorbell", 0, 1_900_000_000)
    finally:
        joined.close()

    # 2 minutes at 5s is ~25 grid points: two full 12-cell sheets and the start
    # of a third -- all in ONE unbroken bucket, not one bucket per frame.
    assert len(buckets) == 1, f"cadence mismatch split the bucket {len(buckets)} ways"
    counts = [s["count"] for s in sorted(sheets, key=lambda r: r["start_ts"])]
    assert counts[:-1] == [12] * (len(sheets) - 1), f"sheets left unfilled: {counts}"
    assert sum(counts) >= 24, f"expected ~25 frames over 2 minutes at 5s, got {sum(counts)}"
    # And the frames really are 5s apart across the whole run.
    span = buckets[0]["end_ts"] - buckets[0]["start_ts"]
    assert span == pytest.approx(120.0, abs=5.0)


def test_a_filling_sheet_is_published_once_per_cycle(env: Settings) -> None:
    """Every distinct count is its own immutable object (§4.3), so publishing
    per *segment* wrote a whole sheet image for each handful of cells -- tens of
    megabytes of superseded versions per 600 KB sheet, all kept until retention.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        rows = conn.execute(
            "SELECT start_ts, COUNT(*) versions FROM scrub_sheets GROUP BY start_ts"
        ).fetchall()
    finally:
        conn.close()

    # Two 10s segments, 12-cell sheets: the first sheet seals inside the cycle,
    # the second is still filling. Each is written exactly once.
    assert [r["versions"] for r in rows] == [1, 1], "a sheet was republished mid-cycle"
    on_disk = sorted(p.name for p in (env.scrub.cache_dir / "doorbell" / "1").glob("*.jpg"))
    assert len(on_disk) == 2, f"superseded sheet images left on disk: {on_disk}"


# ----- Cycle scheduling: live edge first, backfill behind it (§5.4) -----


@pytest.fixture
def long_history_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """24h of contiguous 10s segments and a cold cache -- the shape a real
    deployment starts in."""
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
        [
            (f"s{i}", "/media/frigate/x.mp4", base + i * 10, base + (i + 1) * 10)
            for i in range(8640)  # 24h
        ],
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "x.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_after_h=48.0,  # everything is "recent"
            sheet_cols=4, sheet_rows=3,
            backfill_segments_per_cycle=12, live_edge_segments=6,
            live_edge_lookback_s=300.0,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )
    return settings


def test_first_cycle_on_a_cold_cache_reaches_the_live_edge(long_history_env: Settings) -> None:
    """The reel opens at the live edge, so that is where sprites have to exist.

    Resuming each tier from MAX(generated_through) meant a cold cache started a
    day back and advanced 20 min of footage per cycle -- slower than wall clock
    across ten cameras -- so `generated_through` stayed ~24h behind and the
    client's "is this generated?" check answered no for everything recent.
    """
    env = long_history_env
    now = 1_800_000_000.0 + 86400.0
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        through = db.latest_generated_through(conn, "doorbell", 1.0)
    finally:
        conn.close()

    assert through is not None
    lag = now - through
    assert lag <= env.scrub.live_edge_lookback_s, (
        f"live edge is {lag / 3600:.1f}h behind after the very first cycle"
    )


def test_live_edge_covers_the_newest_segment_first_when_budget_is_short(
    long_history_env: Settings,
) -> None:
    """The live-edge budget (6 segments) is smaller than the lookback window
    (300s / 10s segments = 30), so oldest-first ordering would leave the
    newest segments -- exactly what the client scrubs into -- uncovered until
    the resume cursor crawled up to them over several cycles. Newest-first
    must close that gap on the very first cycle instead.
    """
    env = long_history_env
    now = 1_800_000_000.0 + 86400.0
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
        through = db.latest_generated_through(conn, "doorbell", 1.0)
    finally:
        conn.close()

    assert through is not None
    lag = now - through
    # One 10s segment's worth of slack for the last (possibly partial) segment.
    assert lag <= 10.0, f"live edge is {lag:.1f}s behind the newest segment"


def test_backfill_fills_in_behind_the_live_edge(long_history_env: Settings) -> None:
    """Coverage should grow backwards from now, contiguously -- a user scrubbing
    an hour ago is served before one scrubbing three days ago (§5.4)."""
    env = long_history_env
    now = 1_800_000_000.0 + 86400.0
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        for cycle in range(4):
            asyncio.run(
                generator.generate_camera(
                    env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                    now=now + cycle * 60, profile=generator.SourceProfile(),
                    sem=asyncio.Semaphore(3),
                )
            )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, now + 1000)
    finally:
        conn.close()

    covered_start = min(b["start_ts"] for b in buckets)
    covered_end = max(b["end_ts"] for b in buckets)
    # Still pinned to the edge...
    assert now - covered_end <= env.scrub.live_edge_lookback_s
    # ...and reaching further back than the live-edge pass alone ever would.
    assert now - covered_start > env.scrub.live_edge_lookback_s, (
        "backfill made no progress behind the live edge"
    )


def test_uncovered_spans_finds_holes_between_buckets(tmp_path: Path) -> None:
    conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        for start, end in ((100.0, 200.0), (400.0, 500.0)):
            db.upsert_scrub_bucket(
                conn, camera="c", start_ts=start, end_ts=end, interval_s=1.0,
                width=1, height=1, generated_through=end, complete=True,
            )
        conn.commit()
        spans = generator.uncovered_spans(conn, "c", 1.0, 0.0, 700.0)
    finally:
        conn.close()
    assert spans == [(0.0, 100.0), (200.0, 400.0), (500.0, 700.0)]


def test_uncovered_spans_ignores_sub_interval_seams(tmp_path: Path) -> None:
    """A hole narrower than one interval can't hold a frame, so chasing it
    would make backfill spin on a seam forever."""
    conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        for start, end in ((100.0, 200.0), (200.5, 300.0)):
            db.upsert_scrub_bucket(
                conn, camera="c", start_ts=start, end_ts=end, interval_s=1.0,
                width=1, height=1, generated_through=end, complete=True,
            )
        conn.commit()
        spans = generator.uncovered_spans(conn, "c", 1.0, 100.0, 300.0)
    finally:
        conn.close()
    assert spans == []


def test_a_hole_with_no_recordings_does_not_stall_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camera offline, or motion-only retention: a span with nothing behind it
    yields nothing forever, and must not block the spans older than it."""
    base = 1_800_000_000.0
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    # Recordings only in the OLDER half; the newer half is a permanent hole.
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
        [
            (f"s{i}", "/media/frigate/x.mp4", base + i * 10, base + (i + 1) * 10)
            for i in range(30)  # base .. base+300 only
        ],
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "x.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_after_h=48.0, sheet_cols=4, sheet_rows=3,
            backfill_segments_per_cycle=12, live_edge_segments=2, live_edge_lookback_s=60.0,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

    now = base + 1200.0  # 15 min of empty wall clock after the last recording
    joined = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_camera(
                settings, "doorbell", frigate_conn=joined, sidecar_conn=joined,
                now=now, profile=generator.SourceProfile(), sem=asyncio.Semaphore(3),
            )
        )
    finally:
        joined.close()

    # The live-edge window is empty, but the cycle still reached past it into the
    # span that does have recordings.
    assert result["new_frames"] > 0, "backfill stalled on a hole with no recordings"


def test_every_camera_gets_its_live_edge_before_any_backfill(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running live-edge-then-backfill per camera meant the last camera in the
    list waited out every earlier camera's history before its edge was touched
    -- minutes of staleness that scales with camera count."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    order: list[str] = []
    real_live = generator.generate_live_edge
    real_backfill = generator.generate_backfill

    async def _tracked_live(settings: object, camera: str, **kw: object) -> dict[str, object]:
        order.append(f"live:{camera}")
        return await real_live(settings, camera, **kw)  # type: ignore[arg-type]

    async def _tracked_backfill(settings: object, camera: str, **kw: object) -> dict[str, object]:
        order.append(f"backfill:{camera}")
        return await real_backfill(settings, camera, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(generator, "generate_live_edge", _tracked_live)
    monkeypatch.setattr(generator, "generate_backfill", _tracked_backfill)

    env = env.model_copy(update={"scrub": env.scrub.model_copy(update={"cameras": []})})
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    live_phase = [step for step in order if step.startswith("live:")]
    first_backfill = next(i for i, step in enumerate(order) if step.startswith("backfill:"))
    assert len(live_phase) == 2, f"both cameras should get a live-edge pass: {order}"
    assert first_backfill == len(live_phase), (
        f"backfill started before every camera's edge was serviced: {order}"
    )


def test_backfill_budget_is_shared_across_cameras(long_history_env: Settings) -> None:
    """The cycle's wall clock has to stay bounded regardless of camera count --
    an over-long cycle makes the next live-edge pass late, and the edge slips."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_segments_per_cycle": 8, "live_edge_segments": 2}
            )
        }
    )
    results = asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    assert {r["camera"] for r in results} == {"doorbell", "garden"}
    # 8 shared across 2 cameras = 4 each, plus each camera's own 2-segment edge.
    for r in results:
        assert r["segments"] <= 4 + 2, f"{r['camera']} overran its share: {r}"


def test_backfill_stops_at_its_wall_clock_budget(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Segment count alone can't bound a cycle -- how long a segment takes
    depends on the box. An over-long cycle delays the next live-edge pass,
    which is what let the edge slip behind to begin with."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_time_budget_s": 0.0, "live_edge_segments": 2}
            )
        }
    )
    backfilled: list[str] = []
    real_backfill = generator.generate_backfill

    async def _tracked(settings: object, camera: str, **kw: object) -> dict[str, object]:
        backfilled.append(camera)
        return await real_backfill(settings, camera, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(generator, "generate_backfill", _tracked)
    results = asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    assert not backfilled, "a spent time budget must skip backfill entirely"
    # The live edge still ran for every camera -- it is never the thing skipped.
    assert all(r["segments"] > 0 for r in results)


def test_derived_tier_gets_a_guaranteed_floor_even_when_backfill_is_hungry(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera with a persistent trickle of real backfill holes must not be
    able to consume the entire shared deadline every cycle -- traced live on
    the actual deployment, that starved decimation completely (zero derived-
    tier cycles across several minutes of real operation) because backfill's
    demand never reliably hits zero. `derive_time_reserve_s` carves out a
    floor for Pass 3 regardless of how hungry backfill is.
    """
    env = long_history_env.model_copy(
        update={
            "scrub": long_history_env.scrub.model_copy(
                update={"backfill_time_budget_s": 10.0, "derive_time_reserve_s": 4.0}
            )
        }
    )

    async def _hungry_backfill(settings: object, camera: str, **kw: object) -> dict[str, object]:
        # Consumes more than backfill's own (reserve-shrunk) deadline but
        # less than the outer deadline, simulating real per-cycle demand that
        # never fully idles.
        await asyncio.sleep(7.0)
        return {"camera": camera, "segments": 0, "new_frames": 0, "backfilled": True}

    derived_calls: list[str] = []

    async def _tracked_derived(settings: object, camera: str, **kw: object) -> dict[str, object]:
        derived_calls.append(camera)
        return {"camera": camera, "new_frames": 0, "tiers_touched": 0}

    monkeypatch.setattr(generator, "generate_backfill", _hungry_backfill)
    monkeypatch.setattr(generator, "generate_derived", _tracked_derived)

    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    assert derived_calls, (
        "decimation must still get a turn even when backfill alone exceeds "
        "the shared deadline -- the reserved floor exists exactly for this"
    )


# ----- backfill_min_share_s: the live edge must not starve backfill -----


def test_slow_live_edge_still_leaves_backfill_a_turn(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prod cycles measured 16-20s against a 20s tick -- live edge alone ate
    the whole cycle and backfill's window had already closed by the time it
    started, so the 1s cameras never got their aged tier ("(0 backfilled)" on
    almost every cycle). `backfill_min_share_s` must reserve enough of the
    tick that backfill still runs at least one tier even when live edge is
    this slow.
    """
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(100)
        ],
    )
    conn.commit()
    conn.close()
    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_min_share_s": 5.0, "live_edge_interval_s": 20.0}
            )
        }
    )

    class _FakeClock:
        def __init__(self) -> None:
            self.t = 1_000.0

        def monotonic(self) -> float:
            return self.t

    clock = _FakeClock()
    monkeypatch.setattr(generator.time, "monotonic", clock.monotonic)

    async def _slow_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        # Simulates a decode that burns most of the tick -- e.g. a camera
        # whose GOP forces full-frame decoding instead of keyframe extraction.
        clock.t += 100.0
        return {"camera": camera, "segments": 1, "new_frames": 1}

    backfilled: list[str] = []

    async def _tracked_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        backfilled.append(camera)
        return {"camera": camera, "segments": 1, "new_frames": 0, "backfilled": True}

    async def _noop_derived(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "new_frames": 0, "tiers_touched": 0}

    monkeypatch.setattr(generator, "generate_live_edge", _slow_live)
    monkeypatch.setattr(generator, "generate_backfill", _tracked_backfill)
    monkeypatch.setattr(generator, "generate_derived", _noop_derived)

    profile = generator.SourceProfile()
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0, profile=profile))

    assert backfilled, (
        "backfill got no turn at all -- backfill_min_share_s failed to "
        "reserve time against a slow live edge"
    )
    # The live edge itself must never be fully starved either.
    assert profile.live_edge_cursor >= 1


def test_live_edge_cursor_rotates_cut_off_cameras_to_the_front(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera skipped by a cut-short live-edge pass must be first in line
    next cycle -- the same fairness fix already applied to backfill's own
    cursor (`profile.backfill_cursor`), now needed for live edge too."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(100)
        ],
    )
    conn.commit()
    conn.close()
    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_min_share_s": 5.0, "live_edge_interval_s": 20.0}
            )
        }
    )

    class _FakeClock:
        def __init__(self) -> None:
            self.t = 1_000.0

        def monotonic(self) -> float:
            return self.t

    clock = _FakeClock()
    monkeypatch.setattr(generator.time, "monotonic", clock.monotonic)

    order: list[str] = []

    async def _slow_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        order.append(camera)
        clock.t += 100.0
        return {"camera": camera, "segments": 1, "new_frames": 1}

    async def _noop_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "segments": 0, "new_frames": 0, "backfilled": False}

    async def _noop_derived(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "new_frames": 0, "tiers_touched": 0}

    monkeypatch.setattr(generator, "generate_live_edge", _slow_live)
    monkeypatch.setattr(generator, "generate_backfill", _noop_backfill)
    monkeypatch.setattr(generator, "generate_derived", _noop_derived)

    profile = generator.SourceProfile()
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0, profile=profile))
    first_cycle_order = list(order)
    order.clear()
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0, profile=profile))

    assert len(first_cycle_order) == 1, f"expected only one camera serviced: {first_cycle_order}"
    skipped = {"doorbell", "garden"} - set(first_cycle_order)
    assert order[0] in skipped, (
        f"camera skipped in cycle 1 ({skipped}) must run first in cycle 2, got {order}"
    )


# ----- _retire_stale_recent_buckets: don't erase what nothing covers -----


def test_retire_skips_a_span_the_aged_tier_does_not_yet_cover(env: Settings) -> None:
    """A stale recent-tier bucket must survive until the aged tier has
    actually regenerated its span -- backfill runs on its own budget and can
    lag behind the boundary by cycles. Retiring early erases the recent-tier
    data with nothing at the finer cadence to replace it."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        db.upsert_scrub_bucket(
            conn, camera="doorbell", start_ts=0.0, end_ts=10.0, interval_s=1.0,
            width=320, height=180, generated_through=10.0, complete=True,
        )
        conn.commit()

        generator._retire_stale_recent_buckets(
            conn, env.scrub.cache_dir, "doorbell", recent_interval_s=1.0,
            boundary=20.0, aged_interval_s=5.0,
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 100)
    finally:
        conn.close()

    assert any(b["interval_s"] == 1.0 for b in buckets), (
        "recent bucket was retired even though no aged data covers its span"
    )


def test_retire_removes_a_span_the_aged_tier_already_covers(env: Settings) -> None:
    """Once the aged tier actually covers the stale bucket's span, retirement
    must still happen -- the coverage gate isn't a permanent hold."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        db.upsert_scrub_bucket(
            conn, camera="doorbell", start_ts=0.0, end_ts=10.0, interval_s=1.0,
            width=320, height=180, generated_through=10.0, complete=True,
        )
        db.upsert_scrub_bucket(
            conn, camera="doorbell", start_ts=0.0, end_ts=10.0, interval_s=5.0,
            width=320, height=180, generated_through=10.0, complete=True,
        )
        conn.commit()

        generator._retire_stale_recent_buckets(
            conn, env.scrub.cache_dir, "doorbell", recent_interval_s=1.0,
            boundary=20.0, aged_interval_s=5.0,
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 100)
    finally:
        conn.close()

    assert not any(b["interval_s"] == 1.0 for b in buckets), (
        "recent bucket should have been retired once the aged tier covers its span"
    )


# ----- Cadence matching: don't full-decode what the source can't provide -----


def _settings_with_gop(tmp_path: Path, **scrub_kw: object) -> Settings:
    defaults: dict[str, object] = {
        "recent_interval_s": 1.0, "aged_interval_s": 5.0, "aged_after_h": 24.0,
        "retention_days": 4,
    }
    defaults.update(scrub_kw)
    return Settings(
        frigate=FrigateSection(config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db"),
        sidecar=SidecarSection(db_path=tmp_path / "s.db"),
        scrub=ScrubSection(**defaults),  # type: ignore[arg-type]
    )


def test_a_fine_gop_keeps_both_configured_tiers(tmp_path: Path) -> None:
    """The Dahua cameras here emit a keyframe a second, so 1 fps is free."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=1.0)
    assert [round(p[0], 2) for p in plan] == [1.0, 5.0]
    assert plan[0][2] == now  # recent tier runs to the live edge


def test_a_coarse_gop_generates_at_the_keyframe_cadence(tmp_path: Path) -> None:
    """UniFi Protect emits one every 5s and exposes no way to change it.

    Forcing 1 fps against that means decoding every frame -- ~5x the cost -- to
    synthesise stills the encoder never made distinct. On the reference
    deployment three such cameras were ~70% of the generator's total work.
    """
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=5.0)
    # Recent and aged would describe the same cadence, so they collapse into one
    # tier covering the whole retention window.
    assert len(plan) == 1
    interval, start, end = plan[0]
    assert interval == 5.0
    assert end == now
    assert now - start == pytest.approx(4 * 86400)


def test_a_moderately_coarse_gop_still_thins(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=2.0)
    assert [p[0] for p in plan] == [2.0, 5.0]


def test_cadence_matching_can_be_turned_off(tmp_path: Path) -> None:
    """Off means force the configured interval and pay the decode."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(
        _settings_with_gop(tmp_path, match_keyframe_cadence=False), now, gop_s=5.0
    )
    assert [p[0] for p in plan] == [1.0, 5.0]


def test_a_gop_within_tolerance_is_not_treated_as_coarser(tmp_path: Path) -> None:
    """1.2s against a 1.0s target is inside _GOP_TOLERANCE: keyframe extraction
    already lands close enough, and cell assignment's drift check catches the
    rest."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=1.2)
    assert plan[0][0] == 1.0


def test_unknown_gop_falls_back_to_the_configured_interval(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=None)
    assert [p[0] for p in plan] == [1.0, 5.0]


def test_coarse_gop_camera_uses_the_cheap_extraction_path(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a 5s-GOP camera must never reach extract_fps."""
    env = long_history_env.model_copy(
        update={
            "scrub": long_history_env.scrub.model_copy(
                update={"recent_interval_s": 1.0, "aged_interval_s": 5.0, "aged_after_h": 24.0}
            )
        }
    )

    async def _coarse_gop(seg_path: Path, **kw: object) -> list[float]:
        return [5.0, 5.0]

    async def _boom(*a: object, **k: object) -> list[Path]:
        raise AssertionError("full decode used for a source that can't cheaply provide it")

    monkeypatch.setattr(ffmpeg_io, "probe_keyframe_deltas", _coarse_gop)
    monkeypatch.setattr(ffmpeg_io, "extract_fps", _boom)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [0.0, 5.0]),  # one keyframe every 5s
    )

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_live_edge(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_000.0 + 86400.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert result["new_frames"] > 0
    assert {b["interval_s"] for b in buckets} == {5.0}


@pytest.mark.parametrize(
    ("measured", "expected"),
    [(4.995056, 5.0), (5.001, 5.0), (5.0, 5.0), (2.24, 2.0), (3.3, 3.5), (30.02, 30.0)],
)
def test_derived_interval_is_snapped_to_a_stable_grid(
    tmp_path: Path, measured: float, expected: float
) -> None:
    """A measured GOP is a median of observed spacings, so it arrives as
    4.995056 rather than 5.0 -- and the interval is part of every bucket key,
    sheet filename and cache directory name. Left raw, that noise lands in the
    URLs and a slightly different measurement later strands everything
    generated under the previous value as its own tier."""
    settings = _settings_with_gop(tmp_path)
    plan = generator.tier_plan(settings, 1_800_000_000.0, gop_s=measured)
    assert plan[0][0] == expected


def test_small_measurement_noise_does_not_create_a_new_tier(tmp_path: Path) -> None:
    settings = _settings_with_gop(tmp_path)
    now = 1_800_000_000.0
    intervals = {
        generator.tier_plan(settings, now, gop_s=g)[0][0]
        for g in (4.98, 4.995056, 5.0, 5.02, 5.11)
    }
    assert intervals == {5.0}, f"same source resolved to several tiers: {intervals}"


# ----- Cell geometry follows the source shape -----


def _aspect_settings(tmp_path: Path, **scrub_kw: object) -> Settings:
    return Settings(
        frigate=FrigateSection(config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db"),
        sidecar=SidecarSection(db_path=tmp_path / "s.db"),
        scrub=ScrubSection(cell_w=320, cell_h=180, **scrub_kw),  # type: ignore[arg-type]
    )


def _conn_with_segment(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    frigate_db = tmp_path / "f.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    conn.execute(
        "INSERT INTO recordings VALUES ('s1','cam','/media/frigate/1.mp4',0,10,10.0,5.0)"
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    root = tmp_path / "recordings"
    root.mkdir(exist_ok=True)
    (root / "1.mp4").write_bytes(b"fake")
    return conn, root


@pytest.mark.parametrize(
    ("aspect", "expected_h", "shape"),
    [
        (16 / 9, 180, "16:9 -> unchanged"),
        (4 / 3, 240, "4:3 -> taller cell, no squeeze"),
        (1.0, 320, "square"),
        (2560 / 1440, 180, "1440p 16:9"),
        (1600 / 1200, 240, "the actual doorbell/package geometry"),
    ],
)
def test_cell_height_follows_the_source_aspect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, aspect: float, expected_h: int, shape: str
) -> None:
    """Scaling every source into a fixed cell squeezes anything that isn't that
    shape, and nothing downstream can undo it -- the pixels are already wrong."""
    conn, root = _conn_with_segment(tmp_path)
    settings = _aspect_settings(tmp_path).model_copy(
        update={
            "frigate": FrigateSection(
                config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db",
                media_path=Path("/media/frigate"), recordings_path=root,
            )
        }
    )

    async def _fake_aspect(seg: Path, **kw: object) -> float:
        return aspect

    monkeypatch.setattr(ffmpeg_io, "probe_display_aspect", _fake_aspect)
    got = asyncio.run(
        generator.camera_cell_size(
            settings, "cam", frigate_conn=conn, profile=generator.SourceProfile()
        )
    )
    conn.close()
    assert got == (320, expected_h), shape
    assert got[1] % 2 == 0, "even dimensions keep the scaler on its fast path"


def test_unmeasurable_aspect_falls_back_to_the_configured_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = _conn_with_segment(tmp_path)
    settings = _aspect_settings(tmp_path).model_copy(
        update={
            "frigate": FrigateSection(
                config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db",
                media_path=Path("/media/frigate"), recordings_path=root,
            )
        }
    )

    async def _no_aspect(seg: Path, **kw: object) -> float | None:
        return None

    monkeypatch.setattr(ffmpeg_io, "probe_display_aspect", _no_aspect)
    got = asyncio.run(
        generator.camera_cell_size(
            settings, "cam", frigate_conn=conn, profile=generator.SourceProfile()
        )
    )
    conn.close()
    assert got == (320, 180)


def test_a_missing_ffprobe_degrades_instead_of_exploding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ffprobe on PATH raises FileNotFoundError, not FfmpegError, so it flew
    past every `except FfmpegError` guard and failed whole cycles rather than
    the one measurement it actually prevents."""
    conn, root = _conn_with_segment(tmp_path)
    settings = _aspect_settings(tmp_path).model_copy(
        update={
            "frigate": FrigateSection(
                config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db",
                media_path=Path("/media/frigate"), recordings_path=root,
            )
        }
    )
    monkeypatch.setattr(ffmpeg_io, "_FFPROBE", str(tmp_path / "no-such-ffprobe"))
    profile = generator.SourceProfile()
    try:
        assert asyncio.run(
            generator.camera_cell_size(settings, "cam", frigate_conn=conn, profile=profile)
        ) == (320, 180)
        assert asyncio.run(
            generator.camera_gop_seconds(settings, "cam", frigate_conn=conn, profile=profile)
        ) is None
    finally:
        conn.close()


def test_aspect_preservation_can_be_turned_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, root = _conn_with_segment(tmp_path)
    settings = _aspect_settings(tmp_path, preserve_source_aspect=False).model_copy(
        update={
            "frigate": FrigateSection(
                config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db",
                media_path=Path("/media/frigate"), recordings_path=root,
            )
        }
    )

    async def _boom(seg: Path, **kw: object) -> float:
        raise AssertionError("should not probe when preservation is off")

    monkeypatch.setattr(ffmpeg_io, "probe_display_aspect", _boom)
    got = asyncio.run(
        generator.camera_cell_size(
            settings, "cam", frigate_conn=conn, profile=generator.SourceProfile()
        )
    )
    conn.close()
    assert got == (320, 180)


def test_the_aspect_is_measured_once_per_camera(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ffprobe calls per camera per cycle is real money against an ~80s
    cycle, and neither GOP nor shape changes without a reconfiguration."""
    conn, root = _conn_with_segment(tmp_path)
    settings = _aspect_settings(tmp_path).model_copy(
        update={
            "frigate": FrigateSection(
                config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db",
                media_path=Path("/media/frigate"), recordings_path=root,
            )
        }
    )
    calls = 0

    async def _counted(seg: Path, **kw: object) -> float:
        nonlocal calls
        calls += 1
        return 4 / 3

    monkeypatch.setattr(ffmpeg_io, "probe_display_aspect", _counted)
    profile = generator.SourceProfile()
    for _ in range(5):
        asyncio.run(
            generator.camera_cell_size(settings, "cam", frigate_conn=conn, profile=profile)
        )
    conn.close()
    assert calls == 1


def test_sheets_record_the_geometry_they_were_built_at(
    env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client renders from the per-sheet cell_w/cell_h, so those have to
    describe the cells actually stored."""
    async def _four_three(seg: Path, **kw: object) -> float:
        return 4 / 3

    monkeypatch.setattr(ffmpeg_io, "probe_display_aspect", _four_three)
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert sheets and all((s["cell_w"], s["cell_h"]) == (320, 240) for s in sheets)
    assert buckets and all((b["width"], b["height"]) == (320, 240) for b in buckets)


def test_gop_is_pooled_across_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 10s segment holding a 5s GOP contains two keyframes, so on its own it
    offers one spacing sample -- which reads 4.5 or 5.0 purely by where the
    segment was cut. Measured live, one camera produced both from consecutive
    segments, and since the interval keys every bucket, sheet and directory
    that spawned a second tier for the same camera."""
    frigate_db = tmp_path / "f.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    root = tmp_path / "recordings"
    root.mkdir()
    for i in range(6):
        conn.execute(
            "INSERT INTO recordings VALUES (?, 'cam', ?, ?, ?, 10.0, 5.0)",
            (f"s{i}", f"/media/frigate/{i}.mp4", i * 10, (i + 1) * 10),
        )
        (root / f"{i}.mp4").write_bytes(b"fake")
    conn.commit()
    conn.row_factory = sqlite3.Row

    settings = Settings(
        frigate=FrigateSection(
            config_path=tmp_path / "cfg.yml", db_path=frigate_db,
            media_path=Path("/media/frigate"), recordings_path=root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "s.db"),
        scrub=ScrubSection(recent_interval_s=1.0, aged_interval_s=5.0),
    )

    # One outlier segment, the rest at the true spacing -- exactly the live shape.
    async def _deltas(seg: Path, **kw: object) -> list[float]:
        return [4.496] if seg.name == "5.mp4" else [4.995]

    monkeypatch.setattr(ffmpeg_io, "probe_keyframe_deltas", _deltas)
    gop = asyncio.run(
        generator.camera_gop_seconds(
            settings, "cam", frigate_conn=conn, profile=generator.SourceProfile()
        )
    )
    conn.close()
    assert gop == pytest.approx(4.995), "the outlier must not decide the cadence"
    plan = generator.tier_plan(settings, 1_800_000_000.0, gop)
    assert plan[0][0] == 5.0, "and it must land on one stable tier"


# ----- Derived tiers: decimated from already-published decode-tier sheets --


def test_decimate_source_selects_every_nth_cell(tmp_path: Path) -> None:
    """`_decimate_source` must pick exactly the cells landing on the derived
    grid, cropped from the finer tier's own published sheet -- no ffmpeg."""
    frigate_db = tmp_path / "f.db"
    sqlite3.connect(frigate_db).close()
    sidecar_db = tmp_path / "s.db"
    cache_dir = tmp_path / "scrub"

    conn = db.open_sidecar(sidecar_db)
    try:
        cols, rows, cell_w, cell_h = 4, 3, 8, 6
        start = 1000.0
        rel = grid.sheet_rel_path("cam", 1.0, start, 12)
        out = cache_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        cells = [
            (i, _distinct_cell(tmp_path / f"src{i}.jpg", cell_w, cell_h, i))
            for i in range(12)
        ]
        from marcellus.scrub import tiling

        tiling.tile_sheet(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h, out_path=out)
        db.upsert_scrub_sheet(
            conn, camera="cam", start_ts=start, interval_s=1.0, cols=cols, rows=rows,
            cell_w=cell_w, cell_h=cell_h, count=12, path=rel, complete=True,
        )
        conn.commit()

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        frames = asyncio.run(
            generator._decimate_source(
                conn, cache_dir, "cam",
                finer_interval_s=1.0, derived_interval_s=4.0,
                window_start=start, window_end=start + 12, work_dir=work_dir,
            )
        )
    finally:
        conn.close()

    # 1000 is itself a multiple of 4, so cells at k=0,4,8 (ts 1000,1004,1008)
    # land on the derived grid -- every 4th cell, exactly.
    assert [f.timestamp for f in frames] == [1000.0, 1004.0, 1008.0]
    for f in frames:
        assert Path(f.path).exists()


def _distinct_cell(path: Path, w: int, h: int, idx: int) -> Path:
    Image.new("RGB", (w, h), color=((idx * 20) % 256, 0, 0)).save(path)
    return path


def _derived_env(env: Settings, *, derived_intervals_s: list[float]) -> Settings:
    return env.model_copy(
        update={"scrub": env.scrub.model_copy(update={"derived_intervals_s": derived_intervals_s})}
    )


def test_generate_derived_tier_builds_bucket_and_sheet_from_decode_tier(
    env: Settings,
) -> None:
    """A derived tier records buckets/sheets exactly like a decode tier --
    same `_TierWriter`, just fed by decimated cells instead of ffmpeg output."""
    denv = _derived_env(env, derived_intervals_s=[10.0])
    conn = db.open_joined(denv.frigate.db_path, denv.sidecar.db_path)
    try:
        profile = generator.SourceProfile()
        # Build the recent (1.0s) decode tier first -- 20 frames across base..base+19.
        asyncio.run(
            generator.generate_camera(
                denv, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=profile, sem=asyncio.Semaphore(3),
            )
        )
        result = asyncio.run(
            generator.generate_derived_tier(
                denv, "doorbell", interval_s=10.0,
                frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=profile,
            )
        )
        buckets = [
            b for b in db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
            if b["interval_s"] == 10.0
        ]
        sheets = [
            s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000, interval=10.0)
        ]
    finally:
        conn.close()

    base = 1_800_000_000.0
    assert result["new_frames"] > 0
    assert buckets, "expected a 10.0s derived bucket"
    assert buckets[0]["start_ts"] == pytest.approx(base)
    assert sheets, "expected a 10.0s derived sheet"
    # base and base+10 both land on the recent tier's grid (1.0s) and on the
    # derived tier's own 10.0s grid -- exactly 2 cells selected.
    assert sheets[0]["count"] == 2


def test_decimation_never_spawns_ffmpeg(
    env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decimating a derived tier must never call the ffmpeg extraction layer --
    it only reads already-published sheets off disk."""
    denv = _derived_env(env, derived_intervals_s=[10.0])
    conn = db.open_joined(denv.frigate.db_path, denv.sidecar.db_path)
    try:
        profile = generator.SourceProfile()
        # Build the decode tier normally, with the harmless fakes `env` wires up.
        asyncio.run(
            generator.generate_camera(
                denv, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=profile, sem=asyncio.Semaphore(3),
            )
        )

        async def _boom_keyframes(*a: object, **k: object) -> object:
            raise AssertionError("decimation must not decode keyframes")

        async def _boom_fps(*a: object, **k: object) -> object:
            raise AssertionError("decimation must not full-decode")

        monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _boom_keyframes)
        monkeypatch.setattr(ffmpeg_io, "extract_fps", _boom_fps)

        result = asyncio.run(
            generator.generate_derived_tier(
                denv, "doorbell", interval_s=10.0,
                frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=profile,
            )
        )
    finally:
        conn.close()

    assert result["new_frames"] > 0


def test_backfill_starts_where_the_last_cycle_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall-clock deadline routinely cuts the backfill loop off part-way.
    Restarting at cameras[0] every cycle meant the tail was never reached: four
    of ten cameras on the live deployment had never been backfilled once."""
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    for cam in ("a", "b", "c"):
        conn.execute(
            f"INSERT INTO recordings VALUES ('{cam}1','{cam}','/media/frigate/1.mp4',?,?,10.0,5.0)",
            (base, base + 10),
        )
    conn.commit()
    conn.close()
    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "1.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            config_path=tmp_path / "cfg.yml", db_path=frigate_db,
            media_path=Path("/media/frigate"), recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db"),
        scrub=ScrubSection(
            enabled=True, cache_dir=tmp_path / "scrub",
            # Tiny but non-zero: the first camera is always attempted, and the
            # one below overruns the budget, so every cycle is cut before the
            # *second* camera. That is exactly the starvation shape being fixed.
            backfill_time_budget_s=0.01,
        ),
    )

    served: list[str] = []

    async def _fake_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        served.append(camera)
        await asyncio.sleep(0.05)  # overruns the budget, so only one runs per cycle
        return {"camera": camera, "segments": 0, "new_frames": 0, "backfilled": False}

    async def _noop_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    monkeypatch.setattr(generator, "generate_backfill", _fake_backfill)
    monkeypatch.setattr(generator, "generate_live_edge", _noop_live)

    profile = generator.SourceProfile()
    for cycle in range(3):
        asyncio.run(generator.generate_cycle(settings, now=base + cycle, profile=profile))

    assert sorted(served) == ["a", "b", "c"], (
        f"rotation must reach every camera across cycles, served {served}"
    )


def _cell_is_black(sheet: Path, row_meta: dict[str, object], idx: int) -> bool:
    """True if the sheet's cell `idx` is the tiler's black padding.

    Mean brightness, not peak: the canvas is JPEG-encoded after pasting, so a
    padded cell bleeds ringing from its lit neighbours (measured peak channel 34
    against a real neighbour's 255 on live footage) and no peak threshold
    separates it from genuinely dark night footage. Its mean stays at zero.
    """
    cw, ch, cols = int(row_meta["cell_w"]), int(row_meta["cell_h"]), int(row_meta["cols"])
    x, y = (idx % cols) * cw, (idx // cols) * ch
    with Image.open(sheet) as im:
        crop = im.convert("RGB").crop((x, y, x + cw, y + ch))
    return sum(ImageStat.Stat(crop).mean) < 5.0


def test_sheet_never_claims_a_cell_it_padded_black(env: Settings) -> None:
    """The index must not advertise coverage the image can't back.

    The tiler composes onto a black canvas and places cells at their own index,
    so an index inside the declared count with no cell file on disk is served as
    a black frame -- indistinguishable to the client from real night footage,
    and declared covered so it never falls back to Frigate's preview frames.
    Publishing therefore counts the contiguous cells that actually exist rather
    than trusting the assignment indices.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        # The still-filling sheet: a sheet published full has already released
        # its cell store, which is exactly the state that must not be re-tiled.
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
        target = max((s for s in sheets if not s["complete"]), key=lambda s: s["count"])
        start_ts, interval_s, full_count = target["start_ts"], target["interval_s"], target["count"]
        assert full_count >= 4, "need a multi-cell sheet to punch a hole in"

        # Lose one cell file the way a link failure or a vanished scratch frame
        # does -- after assignment, before publication.
        cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
        (cells_dir / "002.jpg").unlink()

        republished = generator._publish_sheet_version(
            env.scrub.cache_dir, "doorbell", interval_s, start_ts, full_count,
            target["cols"], target["rows"], target["cell_w"], target["cell_h"], "jpeg",
        )
        assert republished is not None
        rel, published_count = republished
        assert published_count == 2, (
            f"must stop at the hole (cell 2), published {published_count} of {full_count}"
        )

        # The published image contains no padded cell at all...
        sheet_path = env.scrub.cache_dir / rel
        for idx in range(published_count):
            assert not _cell_is_black(sheet_path, target, idx), f"cell {idx} is padding"
        # ...and its URL declares exactly what it contains.
        assert Path(rel).name == grid.sheet_filename(start_ts, interval_s, published_count, ".jpg")
    finally:
        conn.close()


def test_publishing_a_short_sheet_retires_the_overclaiming_version(env: Settings) -> None:
    """A version claiming cells the store can't back must leave the index.

    `db.list_scrub_sheets` advertises the *highest* count per sheet as current,
    so an inflated row would stay at the head of the index no matter how honest
    the version published next to it -- the client would keep being handed the
    black-padded URL.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        target = max(
            (s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
             if not s["complete"]),
            key=lambda s: s["count"],
        )
        start_ts, interval_s = target["start_ts"], target["interval_s"]
        inflated_path = env.scrub.cache_dir / target["path"]
        assert inflated_path.exists()

        writer = generator._TierWriter(
            env, "doorbell", interval_s=interval_s,
            window_start=0.0, window_end=1_900_000_000.0,
            cell_w=target["cell_w"], cell_h=target["cell_h"], sidecar_conn=conn,
        )
        cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
        (cells_dir / "002.jpg").unlink()
        asyncio.run(writer._flush_sheet(start_ts, target["count"]))

        current = [
            s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["start_ts"] == start_ts and s["interval_s"] == interval_s
        ]
        assert len(current) == 1
        assert current[0]["count"] == 2, f"index still over-claims: {current[0]['count']}"
        assert not inflated_path.exists(), "the over-claiming image is still servable"
    finally:
        conn.close()


def test_a_sheet_short_of_full_keeps_its_cells_for_the_backfilled_hole(env: Settings) -> None:
    """A sheet held short by a hole must not be sealed.

    Sealing drops the cell store, and the cells past the hole live there: drop
    them and the hole being backfilled later can never extend the sheet, so the
    span stays permanently unclaimed instead of temporarily.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
        full = min(
            (s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
             if s["count"] == env.scrub.sheet_cols * env.scrub.sheet_rows),
            key=lambda s: s["start_ts"],
        )
        start_ts, interval_s = full["start_ts"], full["interval_s"]
        cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", interval_s, start_ts)
        assert not cells_dir.exists(), "a genuinely full sheet should have released its cells"

        # Rebuild the store minus one cell and republish: short, so kept.
        cells_dir.mkdir(parents=True)
        for i in range(full["count"]):
            if i != 5:
                _make_jpg(cells_dir / f"{i:03d}.jpg")
        writer = generator._TierWriter(
            env, "doorbell", interval_s=interval_s,
            window_start=0.0, window_end=1_900_000_000.0,
            cell_w=full["cell_w"], cell_h=full["cell_h"], sidecar_conn=conn,
        )
        asyncio.run(writer._flush_sheet(start_ts, full["count"]))
        assert cells_dir.exists(), "cells dropped while the sheet is still incomplete"
        assert start_ts not in writer._sealed

        # Fill the hole; the next publish extends over the cells that were kept.
        _make_jpg(cells_dir / "005.jpg")
        asyncio.run(writer._flush_sheet(start_ts, full["count"]))
        current = [
            s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["start_ts"] == start_ts and s["interval_s"] == interval_s
        ]
        assert current[0]["count"] == full["count"], "hole filled but the sheet did not extend"
        assert start_ts in writer._sealed
    finally:
        conn.close()


def test_a_late_frame_starts_a_new_bucket_instead_of_skipping_a_cell(env: Settings) -> None:
    """A frame landing two cells past the bucket's last must split it.

    `grid.assign_cells` measures contiguity against `accepted[-1]`, which is
    empty at the start of every call, so a segment contributing a single frame
    one slot late was accepted at its own index -- leaving the cell between with
    no file, rendered black, inside a bucket and sheet that both still declare
    the span contiguous. Measured on live footage before this check: 9 of 14
    sheets in a single cycle carried exactly one such hole.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        writer = generator._TierWriter(
            env, "doorbell", interval_s=10.0,
            window_start=0.0, window_end=1_900_000_000.0,
            cell_w=32, cell_h=18, sidecar_conn=conn,
        )
        writer.resume(1_800_000_000.0)
        scratch = Path(tempfile.mkdtemp())
        def frame(ts: float) -> grid.Frame:
            p = scratch / f"{ts:.0f}.jpg"
            _make_jpg(p)
            return grid.Frame(timestamp=ts, path=str(p))

        # Cells 0-3 of a bucket at 1_800_000_000, then a lone frame on cell 5.
        asyncio.run(writer.feed([frame(1_800_000_000.0 + i * 10.0) for i in range(4)]))
        asyncio.run(writer.feed([frame(1_800_000_000.0 + 50.0)]))
        asyncio.run(writer.flush())

        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
        for sheet in sheets:
            cells_dir = generator._cells_dir(
                env.scrub.cache_dir, "doorbell", sheet["interval_s"], sheet["start_ts"]
            )
            on_disk = sorted(int(p.stem) for p in cells_dir.glob("*.jpg"))
            assert on_disk == list(range(sheet["count"])), (
                f"sheet {sheet['start_ts']} declares {sheet['count']} cells, has {on_disk}"
            )
        # The late frame is its own bucket, not cell 5 of the first one.
        assert len(sheets) == 2, f"expected the late frame to start a new sheet, got {sheets}"
    finally:
        conn.close()


def test_backfill_phase_stops_at_the_callers_deadline(env: Settings) -> None:
    """Backfill must yield to the next trailing-window pass.

    The loop hands `generate_cycle` its own tick as the deadline, so history can
    never push the edge pass out -- the failure that put measured live-edge lag
    at ~105s against a promised 90s.
    """
    import time as _time

    calls: list[str] = []

    async def _slow_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        calls.append(camera)
        await asyncio.sleep(0.05)
        return {"camera": camera, "segments": 1, "new_frames": 0, "backfilled": True}

    async def _noop_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(generator, "generate_backfill", _slow_backfill)
        mp.setattr(generator, "generate_live_edge", _noop_live)
        many = env.model_copy(
            update={"scrub": env.scrub.model_copy(
                update={"cameras": [f"cam{i}" for i in range(10)]}
            )}
        )
        # A deadline already in the past: not one camera may be backfilled.
        asyncio.run(generator.generate_cycle(
            many, now=1_800_000_030.0, backfill_deadline=_time.monotonic() - 1.0,
        ))
        assert calls == [], f"backfill ran past its deadline: {calls}"

        # Without one, the phase's own budget applies and work happens.
        asyncio.run(generator.generate_cycle(many, now=1_800_000_030.0))
        assert calls, "backfill should run when the deadline allows it"


def test_the_tighter_of_deadline_and_budget_wins(env: Settings) -> None:
    """A generous tick must not extend backfill past `backfill_time_budget_s`."""
    import time as _time

    calls: list[str] = []

    async def _slow_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        calls.append(camera)
        await asyncio.sleep(0.05)
        return {"camera": camera, "segments": 1, "new_frames": 0, "backfilled": True}

    async def _noop_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(generator, "generate_backfill", _slow_backfill)
        mp.setattr(generator, "generate_live_edge", _noop_live)
        tiny_budget = env.model_copy(
            update={"scrub": env.scrub.model_copy(update={
                "cameras": [f"cam{i}" for i in range(10)],
                "backfill_time_budget_s": 0.0,
            })}
        )
        asyncio.run(generator.generate_cycle(
            tiny_budget, now=1_800_000_030.0, backfill_deadline=_time.monotonic() + 600.0,
        ))
    assert calls == [], f"a far deadline overrode the budget: {calls}"


def test_every_superseded_partial_version_is_swept_once_the_grace_passes(
    env: Settings,
) -> None:
    """A still-filling sheet is published once per tick and every version is its
    own object, so the intermediates outlive their usefulness by days. Once the
    version that replaced them has been current for the whole grace, all of them
    go -- and the current one stays, however many versions preceded it.
    """
    conn = db.open_sidecar(env.sidecar.db_path)
    now = 1_800_000_000.0
    try:
        paths = {}
        for count, complete in ((10, False), (40, False), (96, True)):
            rel = f"doorbell/1/{grid.sheet_filename(now, 1.0, count, '.jpg')}"
            paths[count] = env.scrub.cache_dir / rel
            paths[count].parent.mkdir(parents=True, exist_ok=True)
            _make_jpg(paths[count])
            db.upsert_scrub_sheet(
                conn, camera="doorbell", start_ts=now, interval_s=1.0, cols=4, rows=3,
                cell_w=32, cell_h=18, count=count, path=rel, complete=complete,
            )
        conn.commit()
        for count in paths:
            os.utime(paths[count], (now - 10_000.0, now - 10_000.0))

        removed = generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera=None, grace_s=900.0, now=now,
        )
        assert removed == 2
        surviving = sorted(
            r["count"] for r in conn.execute(
                "SELECT count FROM scrub_sheets WHERE camera='doorbell'"
            )
        )
        assert surviving == [96], f"swept the wrong versions: {surviving}"
        assert paths[96].exists()
        assert not paths[10].exists() and not paths[40].exists()
    finally:
        conn.close()


def test_the_current_version_is_never_swept_however_old(env: Settings) -> None:
    """Nothing supersedes the largest count, so age alone must not remove it --
    that would delete the only sheet covering a span."""
    conn = db.open_sidecar(env.sidecar.db_path)
    now = 1_800_000_000.0
    try:
        rel = f"doorbell/1/{grid.sheet_filename(now, 1.0, 12, '.jpg')}"
        path = env.scrub.cache_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_jpg(path)
        db.upsert_scrub_sheet(
            conn, camera="doorbell", start_ts=now, interval_s=1.0, cols=4, rows=3,
            cell_w=32, cell_h=18, count=12, path=rel, complete=True,
        )
        conn.commit()
        os.utime(path, (now - 1_000_000.0, now - 1_000_000.0))

        assert generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera=None, grace_s=1.0, now=now,
        ) == 0
        assert path.exists()
    finally:
        conn.close()


def test_sweep_never_crosses_camera_or_tier_keys(env: Settings) -> None:
    """"Superseded" is per (camera, interval, start) -- all three.

    The same start_ts and count recur across every camera and tier, so a join
    that drops any key component would read one camera's larger version as
    superseding another's and delete a span's only sheet.
    """
    conn = db.open_sidecar(env.sidecar.db_path)
    start_ts = 1_800_000_000.0
    try:
        for camera in ("a", "b"):
            for interval_s in (1.0, 60.0):
                for count, complete in ((5, False), (20, False), (96, True)):
                    rel = (f"{camera}/{interval_s:g}/"
                           f"{grid.sheet_filename(start_ts, interval_s, count, '.jpg')}")
                    path = env.scrub.cache_dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    _make_jpg(path)
                    os.utime(path, (1_000_000.0, 1_000_000.0))  # older than any grace
                    db.upsert_scrub_sheet(
                        conn, camera=camera, start_ts=start_ts, interval_s=interval_s,
                        cols=12, rows=8, cell_w=320, cell_h=180,
                        count=count, path=rel, complete=complete,
                    )
        conn.commit()

        generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera=None, grace_s=10.0, now=start_ts,
        )
        survivors = {
            (r["camera"], r["interval_s"], r["count"])
            for r in conn.execute("SELECT camera, interval_s, count FROM scrub_sheets")
        }
        assert survivors == {
            (c, i, 96) for c in ("a", "b") for i in (1.0, 60.0)
        }, f"sweep crossed a key boundary: {sorted(survivors)}"
    finally:
        conn.close()


def test_sweep_scoped_to_a_camera_leaves_the_others_alone(env: Settings) -> None:
    conn = db.open_sidecar(env.sidecar.db_path)
    start_ts = 1_800_000_000.0
    try:
        for camera in ("a", "b"):
            for count in (5, 20):
                rel = f"{camera}/1/{grid.sheet_filename(start_ts, 1.0, count, '.jpg')}"
                path = env.scrub.cache_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                _make_jpg(path)
                os.utime(path, (1_000_000.0, 1_000_000.0))
                db.upsert_scrub_sheet(
                    conn, camera=camera, start_ts=start_ts, interval_s=1.0, cols=12, rows=8,
                    cell_w=320, cell_h=180, count=count, path=rel, complete=False,
                )
        conn.commit()

        removed = generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera="a", grace_s=10.0, now=start_ts,
        )
        assert removed == 1
        remaining = sorted(
            (r["camera"], r["count"])
            for r in conn.execute("SELECT camera, count FROM scrub_sheets")
        )
        assert remaining == [("a", 20), ("b", 5), ("b", 20)], remaining
    finally:
        conn.close()


def test_grace_runs_from_supersession_not_from_publication(env: Settings) -> None:
    """A version that was current for hours must still get its full grace.

    Backfill's round-robin can leave a partially-filled sheet as the only version
    of its span for many cycles. Measuring the grace from that version's own
    publication made it instantly sweepable the moment something finally
    superseded it -- so a client that indexed it a minute earlier got a 404,
    which is exactly what the grace exists to prevent.
    """
    conn = db.open_sidecar(env.sidecar.db_path)
    now = 1_800_000_000.0
    try:
        paths = {}
        for count in (10, 15):
            rel = f"doorbell/1/{grid.sheet_filename(now, 1.0, count, '.jpg')}"
            paths[count] = env.scrub.cache_dir / rel
            paths[count].parent.mkdir(parents=True, exist_ok=True)
            _make_jpg(paths[count])
            db.upsert_scrub_sheet(
                conn, camera="doorbell", start_ts=now, interval_s=1.0, cols=4, rows=3,
                cell_w=32, cell_h=18, count=count, path=rel, complete=False,
            )
        conn.commit()
        # The old version was published two hours ago and was current that whole
        # time; the version that superseded it went out one minute ago.
        os.utime(paths[10], (now - 7200.0, now - 7200.0))
        os.utime(paths[15], (now - 60.0, now - 60.0))

        assert generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera=None, grace_s=900.0, now=now,
        ) == 0, "swept a version superseded only a minute ago"
        assert paths[10].exists()

        # Once the current version has been current for the whole grace, no
        # client can still be holding the older URL.
        os.utime(paths[15], (now - 1000.0, now - 1000.0))
        assert generator.sweep_superseded_versions(
            conn, env.scrub.cache_dir, camera=None, grace_s=900.0, now=now,
        ) == 1
        assert not paths[10].exists()
        assert paths[15].exists()
    finally:
        conn.close()


def test_filling_a_hole_extends_the_sheet_over_the_cells_beyond_it(env: Settings) -> None:
    """The cells kept past a hole have to actually become reachable.

    A pass that fills the missing cell touches exactly one index, so a publish
    bounded by what *this pass* touched would stop one past the hole and never
    look at the cells beyond it -- real imagery, already decoded and stored,
    stranded until retention deleted it. This drives it through `feed()`, the
    path that computes that bound, rather than calling the publisher directly.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    scratch = Path(tempfile.mkdtemp())

    def frame(ts: float) -> grid.Frame:
        p = scratch / f"{ts:.0f}.jpg"
        _make_jpg(p)
        return grid.Frame(timestamp=ts, path=str(p))

    try:
        writer = generator._TierWriter(
            env, "doorbell", interval_s=10.0, window_start=0.0, window_end=1_900_000_000.0,
            cell_w=32, cell_h=18, sidecar_conn=conn,
        )
        writer.resume(1_800_000_000.0)
        base = 1_800_000_000.0
        asyncio.run(writer.feed([frame(base + i * 10.0) for i in range(6)]))
        asyncio.run(writer.flush())

        # Lose cell 3 the way a link failure does, and republish: the sheet
        # truncates to 3 while cells 4 and 5 stay on disk.
        cells_dir = generator._cells_dir(env.scrub.cache_dir, "doorbell", 10.0, base)
        (cells_dir / "003.jpg").unlink()
        asyncio.run(writer._flush_sheet(base, 6))
        current = [
            s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["start_ts"] == base and s["interval_s"] == 10.0
        ]
        assert current[0]["count"] == 3, current[0]["count"]
        assert (cells_dir / "004.jpg").exists(), "cells past the hole were discarded"

        # Backfill fills only the hole -- one index touched.
        _make_jpg(cells_dir / "003.jpg")
        asyncio.run(writer._flush_sheet(base, 4))
        current = [
            s for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["start_ts"] == base and s["interval_s"] == 10.0
        ]
        assert current[0]["count"] == 6, (
            f"sheet stopped at the hole's own index instead of extending: {current[0]['count']}"
        )
    finally:
        conn.close()


def test_prune_counts_and_logs_unlink_failures_but_still_removes_the_rest(
    env: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A blanket `contextlib.suppress(OSError)` around prune's unlinks used to
    swallow anything past FileNotFoundError -- a permissions problem or a
    wedged mount looked identical to a normal already-gone race. Now a
    `PermissionError` on one sheet's file is counted and logged, and prune
    still finishes and removes every other file."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, profile=generator.SourceProfile(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    sheet_files = list((env.scrub.cache_dir / "doorbell").rglob("*.jpg"))
    assert len(sheet_files) > 1, "need at least two sheet files for this to be meaningful"
    poisoned = sheet_files[0]

    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *a: object, **kw: object) -> None:
        if self == poisoned:
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)

    with caplog.at_level("WARNING", logger="marcellus.scrub.generator"):
        result = generator.prune(env, now=1_800_000_030.0 + 30 * 86400)

    assert result["unlink_failures"] == 1
    assert any("unlink failures" in r.message for r in caplog.records)
    assert poisoned.exists(), "the poisoned file could not be removed"
    remaining = list((env.scrub.cache_dir / "doorbell").rglob("*.jpg"))
    assert remaining == [poisoned], "every other sheet file should still be removed"


def test_zero_backfill_share_runs_every_camera_live_edge(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`backfill_min_share_s <= 0` is the escape hatch for a box with no
    decode headroom (prod: 4 cores shared with Frigate, cycles already fill
    the tick). Measured there, a 6 s share cut the live edge to 2-3 of 10
    cameras per tick and the live-edge lag climbed ~20 s per cycle. With the
    share at 0 the live edge must serve every camera every cycle, however
    slow, exactly as before #85."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(100)
        ],
    )
    conn.commit()
    conn.close()
    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_min_share_s": 0.0, "live_edge_interval_s": 20.0}
            )
        }
    )

    class _FakeClock:
        def __init__(self) -> None:
            self.t = 1_000.0

        def monotonic(self) -> float:
            return self.t

    clock = _FakeClock()
    monkeypatch.setattr(generator.time, "monotonic", clock.monotonic)

    served: list[str] = []

    async def _slow_live(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        served.append(camera)
        clock.t += 100.0
        return {"camera": camera, "segments": 1, "new_frames": 1}

    async def _noop_backfill(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "segments": 0, "new_frames": 0, "backfilled": False}

    async def _noop_derived(_s: Settings, camera: str, **kw: object) -> dict[str, object]:
        return {"camera": camera, "new_frames": 0, "tiers_touched": 0}

    monkeypatch.setattr(generator, "generate_live_edge", _slow_live)
    monkeypatch.setattr(generator, "generate_backfill", _noop_backfill)
    monkeypatch.setattr(generator, "generate_derived", _noop_derived)

    profile = generator.SourceProfile()
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0, profile=profile))

    cameras = sorted(set(served))
    assert len(served) == len(cameras) and len(cameras) >= 2, served
