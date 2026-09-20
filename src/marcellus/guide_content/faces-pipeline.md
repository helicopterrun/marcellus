---
title: Face captures
section: faces
order: 1
routes: ["/faces/captures"]
config: ["face_capture"]
---

The face pipeline has two stages; this topic covers the first — collecting
good face images. The second stage, recognition, is
[Identities](/guide/identities).

## Using it

**High-res captures — [Face captures](/faces/captures).** Detection streams
are low resolution; faces from them are often too soft to be useful. The
capture stage watches for a person on a **trigger camera** and grabs
full-resolution frames from a paired **capture camera** (e.g. a
doorbell-height camera aimed at the gate) during the visit. In the last 24
hours **{{stat:faces_captured_24h}}** frames were captured.

The captures page groups by visit for fast review: keep the sharp frontal
shots, discard the rest. Kept captures are the raw material the
[identity clustering](/guide/identities) stage feeds on.

## Configuration

- `face_capture:` — trigger/capture camera pairs are explicit here, so
  captures only happen where you've set them up. The output directory must
  be writable by the sidecar service.
- `enabled` — master on/off switch for the whole feature (default off).
- `trigger_cameras` — cameras whose `person` events trigger a capture; empty
  means the feature does nothing even when enabled.
- `capture_camera` — the single identification camera whose main-stream
  frame is grabbed on a trigger.
- `trigger_labels` — object labels that count as a trigger (default
  `["person"]`).
- `offsets_s` — sample offsets, in seconds from the trigger event's start
  time (default `[-4.0, 0.0, 4.0]`).
- `capture_delay_s` — how long after a sample timestamp to wait before
  fetching it, so the covering recording segment has committed (default
  45s).
- `lookback_s` — how far back each run reconsiders trigger events, for
  self-healing catch-up after a restart (default 3600s).
- `dedup_window_s` — consecutive trigger events within this gap collapse
  into one visit; only the first is captured (default 60s).
- `max_visit_s` — hard ceiling on a gap-chained visit, so a long loiter
  still yields a fresh capture periodically (default 300s).
- `max_captures_per_run` — bound on captures per run, so a long lookback
  can't hold the manual scan open for minutes (default 60).
- `max_attempts` — cap on retries for a transport failure before it's given
  up on (default 3).
- `apply_annotation_offset` — fold the trigger camera's
  `detect.annotation_offset` into the sample timestamps (default on).
- `crop_to_bbox` — crop the preview thumbnail to the capture camera's own
  concurrent event box (default on).
- `head_fraction` — fraction of the person box's height, from its top,
  taken as the head crop (default 0.4).
- `crop_pad` — expand the head crop box by this fraction of its own
  width/height (default 0.25).
- `thumb_max_edge` / `thumb_quality` — size and JPEG quality of the
  generated preview thumbnail (default 480px / 80).
- `output_dir` — directory captures are written to; must be writable by the
  sidecar service.
- `retention_days` — how long captured frames are kept before pruning
  (default 30).
- `http_timeout_s` — timeout for the Frigate HTTP fetch of each sampled
  frame (default 15s).

## If it goes wrong

No captures appearing: confirm the trigger camera is actually producing
person events, and check the capture rows' status/detail on the page — HTTP
errors from Frigate's snapshot endpoint show up there.
