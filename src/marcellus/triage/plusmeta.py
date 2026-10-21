"""What the inbox needs from Frigate's resolved config: thresholds and Frigate+ coverage.

One `GET /api/config` gives both. Per camera/label `threshold` (Frigate fills
the defaults in, so the resolved value is the real one) defines the
borderline band. `model.plus` is the metadata of the Frigate+ model in
use, and its `verifiedCounts` says how many verified images the model was
trained on, per camera name at upload time. `images` is the number to watch:
the per-label numbers count images where that label was enabled, not
objects of it.

Cached for a few minutes in-process: the inbox reads it on every page load
and it only changes on a Frigate restart.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from marcellus.config import Settings
from marcellus.frigate_api import FrigateClient

logger = logging.getLogger(__name__)

_TTL_S = 300.0
_DEFAULT_THRESHOLD = 0.7

_lock = threading.Lock()
_cache: dict[str, tuple[float, PlusMeta]] = {}


@dataclass
class PlusMeta:
    ok: bool = False
    thresholds: dict[tuple[str, str], float] = field(default_factory=dict)
    default_thresholds: dict[str, float] = field(default_factory=dict)
    cameras: list[str] = field(default_factory=list)
    model_name: str = ""
    model_id: str = ""
    train_date: str = ""
    base_model: str = ""
    labels: set[str] = field(default_factory=set)
    # Frigate+ camera name -> verified image count.
    verified_images: dict[str, int] = field(default_factory=dict)

    def threshold(self, camera: str, label: str) -> float:
        return self.thresholds.get(
            (camera, label), self.default_thresholds.get(label, _DEFAULT_THRESHOLD)
        )

    def train_ts(self) -> float | None:
        if not self.train_date:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(self.train_date.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def parse(cfg: dict[str, Any]) -> PlusMeta:
    meta = PlusMeta(ok=True)
    for label, f in ((cfg.get("objects") or {}).get("filters") or {}).items():
        if isinstance(f, dict) and f.get("threshold") is not None:
            meta.default_thresholds[label] = float(f["threshold"])
    for cam, ccfg in (cfg.get("cameras") or {}).items():
        meta.cameras.append(cam)
        for label, f in (((ccfg or {}).get("objects") or {}).get("filters") or {}).items():
            if isinstance(f, dict) and f.get("threshold") is not None:
                meta.thresholds[(cam, label)] = float(f["threshold"])
    plus = (cfg.get("model") or {}).get("plus") or {}
    if isinstance(plus, dict):
        meta.model_name = str(plus.get("name") or "")
        meta.model_id = str(plus.get("id") or "")
        meta.train_date = str(plus.get("trainDate") or "")
        meta.base_model = str(plus.get("baseModel") or "")
        meta.labels = {str(v) for v in (plus.get("labelMap") or {}).values()}
        for cam, counts in (plus.get("verifiedCounts") or {}).items():
            if isinstance(counts, dict):
                meta.verified_images[str(cam)] = int(counts.get("images") or 0)
    return meta


def get(settings: Settings, *, refresh: bool = False) -> PlusMeta:
    """Resolved thresholds + Plus metadata; an empty `ok=False` on failure."""
    key = settings.frigate.base_url
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < _TTL_S:
            return hit[1]
    try:
        with FrigateClient(settings.frigate.base_url) as fc:
            meta = parse(fc.config())
    except Exception:  # noqa: BLE001 - degrade to defaults, never break the page
        logger.warning("inbox: could not read Frigate config", exc_info=True)
        meta = PlusMeta()
    with _lock:
        # A failure is cached briefly too, so a Frigate outage doesn't put a
        # 10 s HTTP timeout on every page load.
        _cache[key] = (now if meta.ok else now - _TTL_S + 30.0, meta)
    return meta


def camera_images(settings: Settings, meta: PlusMeta) -> dict[str, int]:
    """Verified Frigate+ images per CURRENT camera, folding in renamed ones."""
    aliases = settings.triage.plus_camera_aliases
    out: dict[str, int] = {}
    for cam in meta.cameras:
        names = [cam, *aliases.get(cam, [])]
        out[cam] = sum(meta.verified_images.get(n, 0) for n in names)
    return out


def retired_cameras(settings: Settings, meta: PlusMeta) -> dict[str, int]:
    """Frigate+ camera names that match no current camera or alias."""
    claimed = set(meta.cameras)
    for names in settings.triage.plus_camera_aliases.values():
        claimed.update(names)
    return {n: c for n, c in meta.verified_images.items() if n not in claimed}
