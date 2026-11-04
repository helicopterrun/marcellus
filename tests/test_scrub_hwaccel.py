"""VAAPI hardware decode for scrub extraction: argv placement, config, and the
software fallback / auto-disable logic. No real GPU or ffmpeg is needed."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from marcellus.config import ScrubSection
from marcellus.scrub import ffmpeg_io, generator
from marcellus.scrub.generator import SourceProfile

HW = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]


# --- config ------------------------------------------------------------------

def test_config_defaults() -> None:
    s = ScrubSection()
    assert s.hwaccel == "none"
    assert s.hwaccel_device == "/dev/dri/renderD128"


def test_config_vaapi_parses() -> None:
    s = ScrubSection(hwaccel="vaapi", hwaccel_device="/dev/dri/renderD129")
    assert s.hwaccel == "vaapi"
    assert s.hwaccel_device == "/dev/dri/renderD129"


def test_config_rejects_unknown_hwaccel() -> None:
    with pytest.raises(ValidationError):
        ScrubSection(hwaccel="cuda")  # type: ignore[arg-type]


def test_hwaccel_input_args_helper() -> None:
    profile = SourceProfile()
    assert generator.hwaccel_input_args(ScrubSection(), profile) == []
    on = ScrubSection(hwaccel="vaapi")
    assert generator.hwaccel_input_args(on, profile) == HW
    profile.hwaccel_disabled = True
    assert generator.hwaccel_input_args(on, profile) == []


# --- ffmpeg_io argv ----------------------------------------------------------

class _FakeProc:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def _fake_spawn(*argv: str, **kw: object) -> _FakeProc:
        calls.append(argv)
        return _FakeProc()

    monkeypatch.setattr(ffmpeg_io, "_spawn", _fake_spawn)
    return calls


def test_keyframes_argv_with_hwaccel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _capture_argv(monkeypatch)
    asyncio.run(ffmpeg_io.extract_keyframes_with_pts(Path("s.mp4"), tmp_path))
    asyncio.run(ffmpeg_io.extract_keyframes_with_pts(Path("s.mp4"), tmp_path, hwaccel_args=HW))
    plain, hw = (list(c) for c in calls)
    assert "-hwaccel" not in plain
    i = hw.index("-hwaccel")
    assert hw.index("-nostdin") < i < hw.index("-skip_frame") < hw.index("-i")
    assert hw[i:i + 4] == HW
    # Otherwise identical to the software argv.
    assert hw[:i] + hw[i + 4:] == plain


def test_fps_argv_with_hwaccel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _capture_argv(monkeypatch)
    asyncio.run(ffmpeg_io.extract_fps(Path("s.mp4"), tmp_path, 2.0))
    asyncio.run(ffmpeg_io.extract_fps(Path("s.mp4"), tmp_path, 2.0, hwaccel_args=HW))
    plain, hw = (list(c) for c in calls)
    assert "-hwaccel" not in plain
    i = hw.index("-hwaccel")
    assert hw.index("-nostdin") < i < hw.index("-i")
    assert hw[:i] + hw[i + 4:] == plain


# --- generator fallback ------------------------------------------------------

def _run(
    work: Path, scrub: ScrubSection, profile: SourceProfile, monkeypatch: pytest.MonkeyPatch,
    behaviour: list[str],
) -> tuple[list[generator.grid.Frame] | BaseException, list[list[str]]]:
    """Run `_sample_segment` once. `behaviour` is a queue of outcomes, one per
    ffmpeg call: "ok", "fail" (FfmpegError) or "interrupt"."""
    seen: list[list[str]] = []

    async def _fake(seg: Path, out_dir: Path, **kw: object) -> list[tuple[float, Path]]:
        seen.append(list(kw.get("hwaccel_args", ())))  # type: ignore[call-overload]
        what = behaviour.pop(0)
        (out_dir / "000000.jpg").write_bytes(b"x")
        if what == "fail":
            raise ffmpeg_io.FfmpegError("boom")
        if what == "interrupt":
            raise ffmpeg_io.FfmpegInterrupted("stopped")
        # Partial output from a failed attempt must have been cleared.
        assert sorted(p.name for p in out_dir.iterdir()) == ["000000.jpg"]
        return [(0.0, out_dir / "000000.jpg")]

    monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _fake)
    try:
        out: list[generator.grid.Frame] | BaseException = asyncio.run(
            generator._sample_segment(
                Path("s.mp4"), 100.0, 1.0, 1.0, asyncio.Semaphore(1), work,
                cell_w=320, cell_h=180, stats=profile.stats, scrub=scrub, profile=profile,
            )
        )
    except Exception as exc:
        out = exc
    return out, seen


def test_hw_failure_retries_in_software(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    profile = SourceProfile()
    with caplog.at_level(logging.WARNING):
        out, seen = _run(tmp_path, ScrubSection(hwaccel="vaapi"), profile, monkeypatch,
                         ["fail", "ok"])
    assert isinstance(out, list) and len(out) == 1
    assert seen == [HW, []]
    assert profile.stats.ffmpeg_hw_fallbacks == 1
    assert any(r.levelno == logging.WARNING and "software" in r.message for r in caplog.records)
    assert "ffmpeg_hw_fallbacks=1" in profile.stats.summary()


def test_three_consecutive_failures_disable_hwaccel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    profile = SourceProfile()
    scrub = ScrubSection(hwaccel="vaapi")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            out, _ = _run(tmp_path, scrub, profile, monkeypatch, ["fail", "ok"])
            assert isinstance(out, list)
        assert profile.hwaccel_disabled
        out, seen = _run(tmp_path, scrub, profile, monkeypatch, ["ok"])
    assert seen == [[]]
    assert profile.stats.ffmpeg_hw_fallbacks == 3
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_hw_success_resets_consecutive_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = SourceProfile()
    scrub = ScrubSection(hwaccel="vaapi")
    for plan in (["fail", "ok"], ["fail", "ok"], ["ok"], ["fail", "ok"], ["fail", "ok"]):
        _run(tmp_path, scrub, profile, monkeypatch, plan)
    assert not profile.hwaccel_disabled
    assert profile.hw_failures == 2


def test_software_retry_failure_propagates_without_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = SourceProfile()
    out, seen = _run(tmp_path, ScrubSection(hwaccel="vaapi"), profile, monkeypatch,
                     ["fail", "fail"])
    assert isinstance(out, ffmpeg_io.FfmpegError)
    assert seen == [HW, []]
    assert profile.hw_failures == 0
    assert profile.stats.ffmpeg_hw_fallbacks == 0


def test_interrupt_does_not_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SourceProfile()
    out, seen = _run(tmp_path, ScrubSection(hwaccel="vaapi"), profile, monkeypatch,
                     ["interrupt"])
    assert isinstance(out, ffmpeg_io.FfmpegInterrupted)
    assert seen == [HW]
    assert profile.hw_failures == 0
