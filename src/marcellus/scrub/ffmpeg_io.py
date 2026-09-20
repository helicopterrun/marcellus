"""ffmpeg/ffprobe subprocess helpers for the scrub generator (§5.2, §5.3).

Kept separate from the orchestration in generator.py so the pure
cell-assignment logic (grid.py) can be tested without a working ffmpeg on the
test runner. All functions here shell out and are exercised by
integration-style tests guarded with `pytest.mark.skipif(shutil.which(...))`.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path

_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"
_PROBE_TIMEOUT_S = 15.0
_EXTRACT_TIMEOUT_S = 30.0


class FfmpegError(RuntimeError):
    pass


class FfmpegInterrupted(FfmpegError):
    """The child was killed by a signal rather than failing on its input.

    systemd's default KillMode=control-group SIGTERMs every process in the
    unit's cgroup, so a service restart takes any in-flight extraction with it.
    ffmpeg exits 255 with nothing on stderr in that case. Reported as an
    ordinary failure it looks like a camera problem -- and lands
    disproportionately on whichever cameras are slowest to extract, which makes
    the false pattern look meaningful.
    """


async def _spawn(*argv: str, **kwargs: object) -> asyncio.subprocess.Process:
    """`create_subprocess_exec` with a missing binary reported as FfmpegError.

    Without this, a host with no ffmpeg/ffprobe on PATH raises FileNotFoundError
    out of every helper here -- past the `except FfmpegError` guards the callers
    use for every other failure -- and takes down whole generation cycles
    instead of degrading to "this camera couldn't be measured". Startup already
    warns about the missing binary (`_check_scrub_inputs`); it shouldn't also
    fail in an unrecognisable shape.
    """
    try:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        raise FfmpegError(f"could not run {argv[0]}: {exc}") from exc


def _interrupted(returncode: int | None, stderr_tail: str) -> bool:
    # Negative: killed by a signal directly. 255 with nothing to say: ffmpeg's
    # own exit code when it stops on a received signal.
    return returncode is not None and (
        returncode < 0 or (returncode == 255 and stderr_tail == "no stderr")
    )


async def probe_display_aspect(
    segment_path: Path, *, timeout_s: float = _PROBE_TIMEOUT_S
) -> float | None:
    """Display aspect ratio of the video stream, or None if it can't be read.

    Storage dimensions alone are not the display shape: a stream can carry a
    non-square sample aspect ratio, so the ratio has to be `width*SAR_n` over
    `height*SAR_d`. Frigate's own cameras here are all square-sampled, but
    getting this wrong for an anamorphic source would reintroduce exactly the
    squeeze this is meant to remove.
    """
    proc = await _spawn(
        _FFPROBE, "-v", "error", "-select_streams", "v",
        "-show_entries", "stream=width,height,sample_aspect_ratio",
        "-of", "csv=p=0", str(segment_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FfmpegError(f"ffprobe geometry timed out on {segment_path}") from exc
    if proc.returncode != 0:
        return None

    line = next((ln for ln in out.decode().splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2:
        return None
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None

    sar_n = sar_d = 1
    if len(parts) > 2 and ":" in parts[2]:
        try:
            n, d = (int(x) for x in parts[2].split(":"))
            # ffprobe reports "0:1" when the SAR is simply unknown.
            if n > 0 and d > 0:
                sar_n, sar_d = n, d
        except ValueError:
            pass
    return (width * sar_n) / (height * sar_d)


async def probe_keyframe_deltas(
    segment_path: Path, *, timeout_s: float = _PROBE_TIMEOUT_S
) -> list[float]:
    """Spacings between consecutive keyframes in one segment.

    Returned rather than reduced so callers can pool across segments: a 10s
    segment holding a 5s GOP yields exactly two keyframes, so its "median"
    spacing is a single sample, and whether it reads 4.5 or 5.0 depends only on
    where the segment boundary happened to fall.
    """
    pts = await probe_keyframe_pts(segment_path, timeout_s=timeout_s)
    return [b - a for a, b in zip(pts, pts[1:], strict=False)]


async def probe_gop_seconds(segment_path: Path, *, timeout_s: float = _PROBE_TIMEOUT_S) -> float:
    """Best-effort GOP length in seconds for a single segment (§5.2 M1).

    Falls back to the segment's own duration (i.e. "one keyframe per segment",
    the coarse case) if fewer than two keyframes are found. Prefer pooling
    `probe_keyframe_deltas` across several segments where the answer matters.
    """
    deltas = sorted(await probe_keyframe_deltas(segment_path, timeout_s=timeout_s))
    if not deltas:
        return await _probe_duration(segment_path, timeout_s=timeout_s)
    return deltas[len(deltas) // 2]


async def _probe_duration(segment_path: Path, *, timeout_s: float) -> float:
    proc = await _spawn(
        _FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(segment_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FfmpegError(f"ffprobe duration timed out on {segment_path}") from exc
    try:
        return float(out.decode().strip())
    except ValueError:
        return 10.0  # Frigate segments are ~10s; safe fallback.


# ffmpeg renamed the per-frame timestamp field: `pkt_pts_time` through 4.x,
# `pts_time` from 5.x (the old name was removed in 6). Ask for the modern one
# first and fall back, otherwise the whole keyframe path silently returns no
# timestamps on one side of that split -- and "no timestamps" reads exactly
# like "no keyframes", which sends the generator down the expensive
# full-decode fallback at best and yields zero frames at worst.
_PTS_ENTRIES = ("pts_time", "pkt_pts_time")


async def _probe_frame_entry(
    segment_path: Path, entry: str, *, timeout_s: float
) -> list[float]:
    proc = await _spawn(
        _FFPROBE, "-v", "error", "-select_streams", "v",
        "-skip_frame", "nokey",
        "-show_entries", f"frame={entry}",
        "-of", "csv=p=0",
        str(segment_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FfmpegError(f"ffprobe keyframe scan timed out on {segment_path}") from exc
    pts: list[float] = []
    for line in out.decode().splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        with contextlib.suppress(ValueError):
            pts.append(float(line))
    return pts


async def probe_keyframe_pts(
    segment_path: Path, *, timeout_s: float = _PROBE_TIMEOUT_S
) -> list[float]:
    """Presentation timestamps (seconds, relative to segment start) of every
    keyframe in the segment, via one `ffprobe` call (§5.2)."""
    for entry in _PTS_ENTRIES:
        pts = await _probe_frame_entry(segment_path, entry, timeout_s=timeout_s)
        if pts:
            return pts
    return []


#: `showinfo` writes one line per frame it passes, carrying the frame's
#: presentation time in seconds.
_SHOWINFO_PTS = re.compile(rb"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")


async def extract_keyframes_with_pts(
    segment_path: Path, out_dir: Path, *, timeout_s: float = _EXTRACT_TIMEOUT_S,
    cell_w: int = 320, cell_h: int = 180,
) -> list[tuple[float, Path]]:
    """Keyframe-only decode returning `(pts_seconds, path)` for each frame.

    One process instead of two. Probing timestamps separately meant demuxing
    every segment twice -- measured at 0.47s for the probe against 0.26s for the
    extraction itself, so nearly two thirds of the cost bought information the
    extracting process already had. That difference decides whether a
    ten-camera deployment can hold its live edge at all.

    Pairing is positional and produced by a single pass, so the Nth `showinfo`
    line and the Nth file on disk describe the same frame by construction --
    the two-process version assumed that across separate decodes.
    """
    proc = await _spawn(
        _FFMPEG, "-nostdin", "-loglevel", "info",
        "-skip_frame", "nokey", "-vsync", "0", "-i", str(segment_path),
        "-vf", f"scale={cell_w}:{cell_h},showinfo", "-q:v", "8", "-f", "image2",
        str(out_dir / "%06d.jpg"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FfmpegError(f"ffmpeg keyframe extract timed out on {segment_path}") from exc
    if proc.returncode != 0:
        tail = _stderr_tail(err)
        error_cls = FfmpegInterrupted if _interrupted(proc.returncode, tail) else FfmpegError
        raise error_cls(
            f"ffmpeg keyframe extract failed on {segment_path} "
            f"(rc={proc.returncode}): {tail}"
        )

    pts = [float(m.group(1)) for m in _SHOWINFO_PTS.finditer(err)]
    files = sorted(out_dir.glob("*.jpg"))
    return list(zip(pts, files, strict=False))


def _stderr_tail(err: bytes, limit: int = 300) -> str:
    """Last line(s) of ffmpeg's complaint, for the error message.

    Discarding stderr meant a failure logged only "failed on <path>", which says
    nothing about whether the file was truncated, the disk was full, or the
    decoder gave up -- and these failures are intermittent enough that they
    can't be reproduced on demand afterwards.
    """
    text = err.decode("utf-8", "replace").strip()
    return text[-limit:].replace("\n", " | ") if text else "no stderr"


async def extract_fps(
    segment_path: Path, out_dir: Path, interval_s: float, *,
    timeout_s: float = _EXTRACT_TIMEOUT_S, cell_w: int = 320, cell_h: int = 180,
) -> list[Path]:
    """Full-decode `fps=1/N` fallback for a coarser GOP (§5.2)."""
    proc = await _spawn(
        _FFMPEG, "-nostdin", "-loglevel", "error", "-i", str(segment_path),
        "-vf", f"fps=1/{interval_s},scale={cell_w}:{cell_h}", "-q:v", "8", "-f", "image2",
        str(out_dir / "%06d.jpg"),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise FfmpegError(f"ffmpeg fps extract timed out on {segment_path}") from exc
    if proc.returncode != 0:
        tail = _stderr_tail(err)
        error_cls = FfmpegInterrupted if _interrupted(proc.returncode, tail) else FfmpegError
        raise error_cls(
            f"ffmpeg fps extract failed on {segment_path} (rc={proc.returncode}): {tail}"
        )
    return sorted(out_dir.glob("*.jpg"))
