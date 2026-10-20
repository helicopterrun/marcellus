---
title: Wildlife finder
section: tuning
order: 6
routes: ["/wildlife"]
config: ["wildlife"]
---

Frigate's model decides what becomes an event. When it scores an animal too
low, nothing is recorded: no event, no review item, and no example to train
the next model on. The wildlife finder looks again at exactly the footage
where that can happen. These are recording segments that had **motion but no
tracked object**. It runs two models that are built for animals over them.

## How it works

1. **Pick segments.** Every 10 minutes a background job takes the configured
   cameras' segments with motion and zero tracked objects that it hasn't
   scanned yet. Only segments inside each camera's `hours` window are taken.
2. **Decode a few frames.** It decodes up to three keyframes per segment,
   spread across the 10 seconds. This is a keyframe-only decode, which is cheap.
3. **Detect.** [MegaDetector](https://github.com/agentmorris/MegaDetector) v6
   looks for anything animal-shaped. Its threshold is set low on purpose, so it
   misses little and its false positives are expected.
4. **Skip known scenery.** Some spots keep fooling the detector, such as a
   pumpkin, a tree trunk or a garden ornament. Once the classifier has called a
   spot "blank" a couple of times, the job skips boxes in that same place. A cat
   that sleeps on the same mat every night is never skipped, because the
   classifier never calls it blank.
5. **Classify.** [SpeciesNet](https://github.com/google/cameratrapai) labels
   each remaining box. A box it is confident is *blank*, *human* or *vehicle* is
   dropped. Anything else is kept, so recall comes first. The species
   probabilities are rolled up into the labels Frigate uses (squirrel, rodent,
   raccoon, possum, cat, ...) to give a suggestion. Treat the suggestion as a
   hint on infrared footage: SpeciesNet's exact species is often wrong at
   night, even when the family is right.
6. **Group into visits.** Hits on one camera close together in time become one
   visit.

## Reviewing — [Wildlife](/wildlife)

- **Scoreboard.** For each camera, the table at the top compares *Frigate
  caught* (wild-animal events Frigate tracked itself) with what the finder
  found. *Missed share* is kept visits ÷ (kept + Frigate caught). Every finder
  visit comes from a segment where Frigate tracked nothing, so each kept visit
  is an animal Frigate missed. As Frigate+ models improve, this share should
  fall.
- **Keep / discard** applies to a whole visit. Click a crop to open the full
  frame with the box drawn.
- **Frigate+** uploads that one frame through Frigate, at full resolution and
  unlabeled. Label it at plus.frigate.video, then request a new model build.
  Sending a frame also marks its visit kept. Frigate pulls the frame from
  recordings, so this only works while the recording still exists (7 days of
  continuous recording here).

## Setup

The models are exported to ONNX once, offline, with
`tools/wildlife_export_models.py`. Its docstring has the exact commands. Install
the `[wildlife]` extra and the systemd timer
(`contrib/marcellus-wildlife.service` / `.timer`), then enable the section.
`marcellus wildlife scan --start 2026-10-17T00:00-07:00 --end 2026-10-18T00:00-07:00 -c gate-walkway`
backfills a past window, which is useful for checking a known sighting.
`marcellus wildlife stats` prints the scoreboard.

Cost scales with each camera's count of motion-only segments, which varies a
lot between cameras. Start with the cameras where animals actually go. Set
`hours` on cameras with no useful night view.

## Configuration

`wildlife:`

| Setting | Default | What it does |
|---|---|---|
| `enabled` | false | Turn the finder on. The timer job does nothing while this is false. |
| `cameras` | {} | Map of camera name to options. Only these cameras are scanned. Each entry may set `hours: [start, end]` in local time for a daytime-only camera, e.g. `package: {hours: [7, 19]}`. |
| `timezone` | "" | IANA zone the `hours` windows are read in, e.g. America/Los_Angeles. Empty means the host clock, which is often UTC. |
| `detector_path` | /opt/marcellus/data/models/wildlife/MDV6-yolov10-c.onnx | MegaDetector v6 ONNX file written by the export script. |
| `classifier_path` | /opt/marcellus/data/models/wildlife/speciesnet-v4.0.3a-crop.onnx | SpeciesNet crop-classifier ONNX file written by the export script. |
| `classifier_labels_path` | …/speciesnet-v4.0.3a-crop.labels.txt | SpeciesNet's class list, one taxonomy string per line, in model output order. |
| `frame_width` | 1280 | Width frames are scaled to before detection. Small animals far from the camera need 1280; 640 lost an overhead squirrel. |
| `frames_per_segment` | 3 | Keyframes sampled per 10s segment. More catches quicker dashes and costs proportionally more CPU. |
| `min_detector_score` | 0.2 | MegaDetector score floor for a candidate box. Low on purpose, because the classifier does the rejecting. |
| `reject_score` | 0.8 | A box whose blank, human or vehicle probability reaches this is dropped. Lower it to drop more doubtful boxes. |
| `exclude_suggestions` | ["bird"] | Suggestions recorded but never queued for review. Frigate already tracks birds. |
| `scenery_iou` | 0.6 | How closely a new box must overlap a past blank box to count as the same spot. |
| `scenery_min_blanks` | 2 | Blank calls at a spot before it is skipped as scenery. 0 turns learned scenery off, which classifies every box. |
| `scenery_memory_h` | 48 | Hours a blank call is remembered. After that the spot is classified again, so a moved object is not skipped forever. |
| `visit_gap_s` | 60 | Animal hits on one camera within this many seconds join the same visit. |
| `max_visit_s` | 900 | Longest single visit. A longer stay is split into several reviewable visits. |
| `process_delay_s` | 120 | Wait this long after a segment ends before scanning it, so the recording is committed. |
| `lookback_s` | 86400 | How far back each run looks for unscanned segments. This lets a run that fell behind catch up. |
| `max_runtime_s` | 540 | Time budget for one run. The rest waits for the next run. Keep it under the 10-minute timer interval. |
| `threads` | 2 | onnxruntime threads. The unit also runs at the lowest CPU and I/O priority. |
| `output_dir` | /opt/marcellus/data/wildlife | Where crops and frame previews of animal hits are saved. Must be writable by the service. |
| `retention_days` | 30 | Finder rows and images older than this are deleted. |
| `max_attempts` | 3 | How many runs retry a segment that failed to decode before it is given up. |
| `http_timeout_s` | 60 | Timeout for a Frigate+ upload, during which Frigate extracts the frame and posts it upstream. |
