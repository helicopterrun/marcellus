"""Shared runtime helper: seconds to add to Frigate detect-stream event times
to land them on the record clock.

Frigate stamps events (and review segments, which reuse event clocks) from
the detect stream, while recordings come off the record stream -- two
separate RTSP sessions that sit a few seconds apart per camera.
`cameras.<cam>.detect.annotation_offset` (ms) is Frigate's own knob for that
skew; a sidecar-side override (`event_clock_offsets` table, applied from the
Settings page) is used when the config value is unset, so alignment does not
require editing Frigate's config.

Originally lived only in `routes/scrub.py` (the reel/coverage read path);
factored out here so `encounters/service.py`'s reconciler can apply the same
shift to `reviewsegment.start_time` without importing a route module. Do not
change this module's behaviour without checking both callers -- scrub's reel
alignment and encounters' linking both depend on it staying identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from marcellus.faces.crosscam import annotation_offset_ms

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
