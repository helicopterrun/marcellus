"""Tests for §8.2 path mapping (strip media_path, reattach recordings_path)."""

from __future__ import annotations

from pathlib import Path

from marcellus.scrub.mapping import map_recording_path


def test_strips_media_path_and_reattaches_recordings_path() -> None:
    raw = "/media/frigate/recordings/2026-07-30/14/alley-wide/30.44.mp4"
    result = map_recording_path(
        raw, Path("/media/frigate"), Path("/mnt/frigate-storage/recordings/recordings")
    )
    assert result == Path(
        "/mnt/frigate-storage/recordings/recordings/recordings/2026-07-30/14/alley-wide/30.44.mp4"
    )


def test_nonmatching_prefix_falls_back_to_relative() -> None:
    raw = "/some/other/root/x.mp4"
    result = map_recording_path(raw, Path("/media/frigate"), Path("/host/recordings"))
    assert result == Path("/host/recordings/some/other/root/x.mp4")
