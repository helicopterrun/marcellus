"""Recording path mapping: Frigate's container path -> the sidecar's host path.

docs/scrub-cache-and-proxy-spec.md §8.2 -- a two-part rewrite, not a simple
1:1 substitution: strip `frigate.media_path` (the DB's container-side root)
from `recordings.path`, then reattach `frigate.recordings_path` (the
sidecar's own host-side path to the same tree, which may itself have
deployment-specific quirks like a nested `recordings/` segment).
"""

from __future__ import annotations

from pathlib import Path


def map_recording_path(raw_path: str, media_path: Path, recordings_path: Path) -> Path:
    raw = str(raw_path)
    media_prefix = str(media_path).rstrip("/")
    if raw == media_prefix:
        rel = ""
    elif raw.startswith(media_prefix + "/"):
        rel = raw[len(media_prefix) + 1 :]
    else:
        # Prefix didn't match -- best-effort: treat as already-relative rather
        # than silently producing a bogus absolute path.
        rel = raw.lstrip("/")
    return Path(recordings_path) / rel if rel else Path(recordings_path)
