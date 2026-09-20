---
title: Identities
section: faces
order: 2
routes: ["/enrich/clusters"]
config: ["face_enrich"]
---

[Identities](/enrich/clusters) is where face recognition becomes useful. A
background worker watches person events on enrolled cameras, samples
recording frames, scores face quality, computes embeddings, and groups
recurring people into **clusters** — no training, no photo upload.

Right now there are **{{stat:clusters_total}}** clusters,
**{{stat:clusters_named}}** of them named.

## How it becomes recognition

A cluster starts **unknown**. The moment you give it a name, it becomes a
**known person**: future sightings write that name into the Frigate event's
`sub_label` (visible in Frigate, the Elsinore app, and notifications), and
the name is retro-written onto the cluster's past events still in retention.
Unnamed clusters that stop appearing expire on their own after
`cluster_ttl_days`.

## Walkthrough: name your first identity

```walkthrough
- Wait until a cluster shows several sightings of the same person
- Check the sighting strip is coherent — every thumbnail is the same person
- Exclude any wrong sighting with its ✕ button
- Type the person's name and tap "name"
- Watch the toast confirm how many past events were relabeled
- If a second cluster of the same person exists, use "merge" to fold it in
```

## Repair tools

- **✕ on a sighting** — soft-**excludes** it: the sighting stays in the
  cluster's history but no longer feeds its face signature (centroid),
  sub_label matching, or the pipeline stats. A toast offers **Undo**
  right away, and the **Show excluded** link at the top of the page lists
  everything you've excluded (dimmed, with an "include" action) if you
  change your mind later. This is different from the hard "remove" the
  API still exposes, which fully detaches a sighting and can empty (and
  delete) a cluster — that stays available for repairing a bad join, but
  the page no longer uses it directly.
- **merge** — combine two clusters of the same person; the named one
  survives. "Looks like…" hints appear when two clusters are suspiciously
  similar.
- **delete** — dissolve a cluster entirely (events keep their history).

## Configuration

The `face_enrich:` section (needs the `enrich` install extra): `enabled`,
`cameras` to enroll, quality/matching thresholds, and `cluster_ttl_days`.
Keep Frigate's own face recognition **disabled** on enrolled cameras — the
sidecar is the sole author of `sub_label`, and two writers would fight.

- `interval_s` — worker cadence for the enrichment cycle (default 15s).
- `process_delay_s` — how long after an event ends before it's processed,
  so its last segment has committed (default 45s).
- `lookback_s` — how far back each cycle reconsiders ended events, for
  self-healing catch-up (default 3600s).
- `max_frames` / `min_sample_gap_s` — at most this many frames are sampled
  per event, at least this far apart (default 40 / 1.0s).
- `best_n` — number of best-quality frames kept for embedding aggregation
  (default 5).
- `min_face_area_px` — minimum face box area, in pixels on the main-stream
  frame, to be considered (default 4000).
- `min_quality` — minimum quality score (sharpness × size × frontality) for
  a sampled face (default 0.15).
- `match_threshold` — cosine-distance threshold for matching a face to a
  NAMED cluster (default 0.45).
- `cluster_threshold` — cosine-distance threshold for folding a face into
  an unnamed cluster (default 0.55).
- `model_dir` — where the insightface model pack is cached, ~300MB on
  first download.
- `max_events_per_cycle` — bound on events processed per cycle (default
  10).
- `max_attempts` — cap on retries for a transport/inference failure
  (default 3).
- `http_timeout_s` — timeout for the Frigate HTTP fetch of each sampled
  frame (default 15s).

## If it goes wrong

Clusters never appearing: check `face_enrich` in `/healthz`, confirm the
camera is enrolled, and confirm the `enrich` extra is installed. The stats
bar on the page shows the last-7-days pipeline outcomes (no-face, no-frame,
errors), which usually points at the stage that's starving.
