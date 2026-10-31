---
title: Face captures
section: people
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

Only frames with a detected face are kept (the **face gate**): a trigger
often fires when nobody is in front of the capture camera yet, and without
the gate those frames are just the street. When the capture camera tracks the
person itself, frames are also sampled across its own sighting, not only at
fixed offsets from the trigger, so a visitor who reaches the gate a while
after (or before) the trigger fires is still caught. Each kept frame's
thumbnail is cropped to the face.

The captures page shows one card per visit: the most confident face large,
the visit's other faces in a strip below it (tap one to swap it in), and
**To review / Kept / Discarded / All** tabs with counts over the last 7, 30
or 90 days. Tap the face to open the full frame with the detected face
outlined; arrow keys step through the visit's frames, and a single bad frame
can be discarded there. **Keep** and **Discard** act on the whole visit and
offer **Undo**. Kept frames are skipped by `face-capture regate`, but every
capture, kept or not, is deleted after `retention_days`. Discarding only
hides a visit from review.

Each card also names the [identities](/guide/identities) clustered from the
capture camera's own sightings within `event_window_s` of the trigger, and
links straight to them. Identity clustering runs on those sightings, not on
these captures, so this is where a sharp capture meets the cluster to name.
The status pill shows when the capture job last ran (hover it for the full
counts), and **Check now** runs it immediately.

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
- `face_gate` (default on) — run the face detector on each frame and keep
  only frames with a face, thumbnailed to that face. Needs the `[enrich]`
  extra; without it, capture runs ungated and logs an error.
- `min_face_score` (default 0.6) — detector score a face must reach to pass
  the gate.
- `face_pad` (default 0.4) — padding around the face crop, as a fraction of
  the face size per side.
- `event_window_s` (default 30) — with the gate on, if the capture camera has
  its own person event within this many seconds of the trigger, frames are
  also sampled across that event, not only at `offsets_s`.
- `event_max_samples` (default 8) — most frames sampled across that event.
- `event_min_gap_s` (default 1.0) — minimum spacing between those samples,
  in seconds.
- `keep_per_visit` (default 3) — how many face frames a visit keeps, biggest
  and most confident first; the rest are recorded as skipped.
- `crop_to_bbox` — when no face crop is available (gate off), crop the
  preview thumbnail to the capture camera's own concurrent event box
  (default on).
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
