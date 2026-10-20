"""Per-cycle cost counters for the scrub generator.

Held on `SourceProfile` and reset at the start of each `generate_cycle`, so the
cycle's summary line can say where the time and CPU went (ffmpeg spawns, Pillow
decodes/encodes, wall time per pass) without anyone having to attach a profiler
to the prod box. Pure bookkeeping: plain int/float `+=`, which is safe from the
`asyncio.to_thread` workers that do the Pillow work (the GIL covers it, and an
occasional lost update on a diagnostic counter is not worth a lock).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CycleStats:
    # Wall seconds per pass (live edge, backfill, decimation/derived/retire).
    pass1_s: float = 0.0
    pass2_s: float = 0.0
    pass3_s: float = 0.0
    # ffmpeg extraction subprocesses (one per segment decoded) and their summed
    # wall time. Failed and timed-out spawns count: they cost CPU all the same.
    ffmpeg_spawns: int = 0
    ffmpeg_s: float = 0.0
    segments_decoded: int = 0
    # ffprobe helper calls (GOP / aspect probes); each may spawn more than one
    # process, so this counts calls, not processes.
    ffprobe_calls: int = 0
    # Pillow image decodes (cell JPEGs composed into a sheet, source sheets
    # opened for decimation) and encodes (published sheets, decimated cell crops).
    pil_decodes: int = 0
    pil_sheet_encodes: int = 0
    pil_cell_encodes: int = 0

    @property
    def ms_per_segment(self) -> float:
        return self.ffmpeg_s * 1000.0 / self.segments_decoded if self.segments_decoded else 0.0

    def summary(self) -> str:
        return (
            f"pass1={self.pass1_s:.2f}s pass2={self.pass2_s:.2f}s pass3={self.pass3_s:.2f}s "
            f"ffmpeg_spawns={self.ffmpeg_spawns} ffmpeg_s={self.ffmpeg_s:.2f} "
            f"segments_decoded={self.segments_decoded} ms_per_segment={self.ms_per_segment:.0f} "
            f"ffprobe_calls={self.ffprobe_calls} pil_decodes={self.pil_decodes} "
            f"pil_sheet_encodes={self.pil_sheet_encodes} pil_cell_encodes={self.pil_cell_encodes}"
        )
