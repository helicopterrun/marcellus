"""Integration tests for the ffmpeg helpers, against a real encoded segment.

Skipped where ffmpeg isn't installed. These matter because the extraction path
is where timestamps come from: if `showinfo` output and the files on disk ever
disagree, every sprite in the sheet is mislabelled in time and nothing else in
the pipeline can detect it.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from marcellus.scrub import ffmpeg_io

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


@pytest.fixture(scope="module")
def segment(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 10s clip with a keyframe every second, like Frigate's own segments."""
    out = tmp_path_factory.mktemp("seg") / "segment.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=10:size=640x360:rate=10",
            "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(out),
        ],
        check=True,
    )
    return out


def test_extract_returns_a_timestamp_for_every_file(segment: Path, tmp_path: Path) -> None:
    got = asyncio.run(
        ffmpeg_io.extract_keyframes_with_pts(segment, tmp_path, cell_w=64, cell_h=36)
    )
    assert got, "expected keyframes out of a 10s clip with a 1s GOP"
    for pts, path in got:
        assert path.exists() and path.stat().st_size > 0
        assert 0.0 <= pts <= 10.0
    # Strictly increasing, one file each, no repeats.
    assert [p for p, _ in got] == sorted(p for p, _ in got)
    assert len({path for _, path in got}) == len(got)


def test_extracted_timestamps_match_ffprobe(segment: Path, tmp_path: Path) -> None:
    """The single-pass timestamps have to equal what the separate probe said --
    that equality is the whole basis for dropping the second process."""
    probed = asyncio.run(ffmpeg_io.probe_keyframe_pts(segment))
    got = asyncio.run(
        ffmpeg_io.extract_keyframes_with_pts(segment, tmp_path, cell_w=64, cell_h=36)
    )
    assert [round(p, 3) for p, _ in got] == [round(p, 3) for p in probed[: len(got)]]


def test_extract_honours_the_requested_cell_size(segment: Path, tmp_path: Path) -> None:
    from PIL import Image

    got = asyncio.run(
        ffmpeg_io.extract_keyframes_with_pts(segment, tmp_path, cell_w=96, cell_h=54)
    )
    with Image.open(got[0][1]) as im:
        assert im.size == (96, 54)


def test_probe_gop_seconds_measures_the_encoder_cadence(segment: Path) -> None:
    gop = asyncio.run(ffmpeg_io.probe_gop_seconds(segment))
    assert 0.5 <= gop <= 1.5, f"expected ~1s GOP, measured {gop}"


def test_missing_file_raises_rather_than_returning_empty(tmp_path: Path) -> None:
    with pytest.raises(ffmpeg_io.FfmpegError):
        asyncio.run(
            ffmpeg_io.extract_keyframes_with_pts(tmp_path / "nope.mp4", tmp_path)
        )
