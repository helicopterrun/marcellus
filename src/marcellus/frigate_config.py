"""Read the bits of Frigate's own `config.yml` the sidecar needs to answer for.

Kept separate from `zones.py` (which reads the same file for polygon overlays)
because this is about answering "what does Frigate's configuration imply",
which callers ask per request. The file is parsed at most once per mtime.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def load_frigate_config(config_path: str | Path) -> dict[str, Any]:
    """Parse Frigate's config.yml, or `{}` if it is missing or unparseable.

    A broken Frigate config is Frigate's problem; it must not turn a sidecar
    read endpoint into a 500.
    """
    p = Path(config_path)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    key = str(p)
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with p.open() as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("could not parse Frigate config at %s", p)
        return {}
    if not isinstance(data, dict):
        return {}
    _cache[key] = (mtime, data)
    return data


def _record_section(cfg: dict[str, Any], camera: str | None) -> dict[str, Any]:
    record = cfg.get("record")
    merged: dict[str, Any] = dict(record) if isinstance(record, dict) else {}
    if camera:
        cam = (cfg.get("cameras") or {}).get(camera)
        cam_record = cam.get("record") if isinstance(cam, dict) else None
        if isinstance(cam_record, dict):
            merged.update(cam_record)
    return merged


def recording_retention_days(
    config_path: str | Path, camera: str | None = None
) -> float | None:
    """How far back Frigate keeps *recording segments*, in days, or None.

    The outer bound of the continuous and motion bands -- the two settings that
    decide whether footage exists at a past moment irrespective of any event.
    Alert/detection retention is deliberately excluded: those keep recordings
    only around the events that triggered them, so reporting 90 here would
    promise coverage that mostly isn't there.

    Handles both the modern shape (`record.continuous.days`, `record.motion.days`)
    and the older flat `record.retain.days`.
    """
    record = _record_section(load_frigate_config(config_path), camera)
    candidates: list[float] = []
    for key in ("continuous", "motion"):
        band = record.get(key)
        if isinstance(band, dict) and isinstance(band.get("days"), (int, float)):
            candidates.append(float(band["days"]))
    retain = record.get("retain")
    if isinstance(retain, dict) and isinstance(retain.get("days"), (int, float)):
        candidates.append(float(retain["days"]))
    return max(candidates) if candidates else None
