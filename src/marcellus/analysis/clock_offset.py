"""Shared runtime helper: seconds to add to Frigate detect-stream event times
to land them on the record clock.

Frigate stamps events (and review segments, which reuse event clocks) from
the detect stream, while recordings come off the record stream -- two
separate RTSP sessions that sit a few seconds apart per camera.
`cameras.<cam>.detect.annotation_offset` (ms) is Frigate's own knob for that
skew; a sidecar-side override (`event_clock_offsets` table, applied from the
Settings page) is used when the config value is unset, so alignment does not
require editing Frigate's config.

The constant is not the whole shift. Frigate's recording clock carries a 0-1 s
sawtooth of its own: a segment's `start_time` is the whole wall-clock second
its file was opened, and its footage really starts a fraction `f` later
(`analysis.segment_clock`). With `scrub.segment_clock_correction` on (the
default) the shift for an event at detect time `t` is

    constant + (0.5 - f(t))

where `f(t)` is the estimate for the recording segment holding `t + constant`.
The constant keeps its old meaning -- the offset that is right *on average*,
which is what Frigate's own `annotation_offset` is in Frigate's UI and what
every value measured before this correction existed is -- because on a
drifting camera `f` averages about 0.5 over the events any constant was fitted
on (where it does not, a re-measure removes the bias). Wherever no segment
vouches for `f` (no recordings rows, a gap, an unpinned run, a non-whole-second
`start_time`) the term is 0 and the shift is exactly the constant, as before.
`ClockShift.at` is the one place that sum happens.

Originally lived only in `routes/scrub.py` (the reel/coverage read path);
factored out here so `encounters/service.py`'s reconciler can apply the same
shift to `reviewsegment.start_time` without importing a route module. Do not
change this module's behaviour without checking both callers -- scrub's reel
alignment and encounters' linking both depend on it staying identical.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marcellus.analysis import segment_clock
from marcellus.faces.crosscam import annotation_offset_ms

#: Bound on the segment-clock term, seconds. `f` estimates sit in [0, 1)
#: give or take the file-open jitter; anything past this is clamped. Callers
#: that turn a record-clock window into a detect-clock SQL range widen it by
#: `ClockShift.max_dev_s` and filter after shifting.
MAX_CORRECTION_S = 0.6

#: (config mtime, epoch, offset seconds) per camera. The config is a file
#: read + YAML parse and the sidecar override is a DB row, and callers here
#: run on hot paths (once per camera per reel/reconcile cycle is enough).
_cache: dict[str, tuple[float, int, float]] = {}
_epoch = 0


def invalidate() -> None:
    """Called after the Settings apply endpoint writes new overrides."""
    global _epoch
    _epoch += 1


def event_clock_offset_s(settings: Any, camera: str) -> float:
    """Seconds to add to detect-stream event times to land on the record clock.

    The config value wins when set; otherwise a sidecar-side override applied
    from the Settings page (`event_clock_offsets`) is used.
    """
    from marcellus import db

    config_path = str(settings.frigate.config_path)
    try:
        mtime = Path(config_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _cache.get(camera)
    if cached is not None and cached[0] == mtime and cached[1] == _epoch:
        return cached[2]
    offset_ms = annotation_offset_ms(config_path, camera)
    if offset_ms == 0:
        try:
            conn = db.open_sidecar(settings.sidecar.db_path)
            try:
                offset_ms = db.event_clock_offsets(conn).get(camera, 0)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 -- alignment is best-effort, never a 500
            offset_ms = 0
    offset_s = offset_ms / 1000.0
    _cache[camera] = (mtime, _epoch, offset_s)
    return offset_s


def _term(clock: segment_clock.SegmentClock, camera: str, record_t: float) -> float:
    f = clock.fraction(camera, record_t)
    if f is None:
        return 0.0
    return round(max(-MAX_CORRECTION_S, min(MAX_CORRECTION_S, 0.5 - f)), 3)


@dataclass(frozen=True)
class ClockShift:
    """Seconds to add to one camera's detect-clock times, as a function of time.

    `constant_s` is `event_clock_offset_s`; `at(t)` adds the segment-clock
    term when the correction is on. `max_dev_s` bounds `|at(t) - constant_s|`
    (0 when the correction is off, so callers keep their exact old queries).
    """

    camera: str
    constant_s: float
    clock: segment_clock.SegmentClock | None = None

    @property
    def max_dev_s(self) -> float:
        return MAX_CORRECTION_S if self.clock is not None else 0.0

    def correction_s(self, t: float) -> float:
        """The segment-clock term alone for detect-clock time `t` (0 when
        unknown or off)."""
        if self.clock is None:
            return 0.0
        return _term(self.clock, self.camera, t + self.constant_s)

    def at(self, t: float) -> float:
        """Seconds to add to detect-clock time `t` to land on the picture."""
        if self.clock is None:
            return self.constant_s
        return self.constant_s + self.correction_s(t)

    def shift(self, t: float) -> float:
        """`t` moved onto the record clock."""
        return t + self.at(t)


def segment_correction_enabled(settings: Any) -> bool:
    scrub = getattr(settings, "scrub", None)
    return bool(getattr(scrub, "segment_clock_correction", True))


def event_clock_shift(settings: Any, camera: str) -> ClockShift:
    """The shift every event/review/path time for `camera` goes through --
    scrub's reel, highlights and continuations and encounters' linking alike."""
    constant = event_clock_offset_s(settings, camera)
    clock = None
    if segment_correction_enabled(settings):
        clock = segment_clock.clock_for(str(settings.frigate.db_path))
    return ClockShift(camera=camera, constant_s=constant, clock=clock)


def segment_term_s(settings: Any, camera: str, record_t: float) -> float:
    """`0.5 - f` for the segment holding record-clock time `record_t`, or 0
    (correction off, or nothing known). The measuring tools add this to
    `detect time + candidate constant` so the constant they find is the one
    `ClockShift` should be given -- not one that has `f` folded into it."""
    if not segment_correction_enabled(settings):
        return 0.0
    return _term(segment_clock.clock_for(str(settings.frigate.db_path)), camera, record_t)


def segment_term_fn(settings: Any) -> Callable[[str, float], float] | None:
    """`segment_term_s` bound to `settings` for the measuring tools, or None
    when the correction is off (they then measure a plain constant)."""
    if not segment_correction_enabled(settings):
        return None
    return lambda camera, record_t: segment_term_s(settings, camera, record_t)
