---
title: Scrub
section: sidecar
order: 2
routes: ["/scrub"]
config: ["scrub"]
---

[Scrub](/scrub) is the timeline viewer — a **reel**: a fixed aperture over a
column of time that moves behind it. Drag it (down is earlier), scroll, or
use the arrow keys. Frames render instantly, because the sidecar pre-renders
recordings into **sprite sheets** — grids of small frames at a fixed cadence.
The Elsinore app's reel is the same instrument over the same API.

In the last 24 hours the cache generated **{{stat:scrub_sheets_24h}}**
sprite sheets.

## Reading it

Left to right: the time column, then the severity spine, then four object
lanes, then the motion tide coming in from the right edge.

- **Rows** are one rung of the zoom ladder — 1s, 5s, 1m, 5m, 15m or 1h each.
  Those six are exactly the six sprite cadences the cache generates, at one
  cell per row. A row landing on the rung's counting landmark (minute, hour,
  day) gets a heavier rule.
- **Severity spine** is Frigate's own call: a full-width amber bar is an
  alert, a thin grey rule a detection.
- **Lanes** are person, vehicle, animal and package. A track is drawn for as
  long as the object was present, with a cap where it arrived and a cap where
  it left — *no closing cap means it never left*, not that it left there. An
  unrecognised label gets no lane and is not drawn, rather than being filed
  under the wrong one.
- **Tide** is motion: area, low contrast, behind everything, because it
  steers rather than reads.
- **Hatching** means nothing was recorded there. That is distinct from a
  quiet stretch, which still draws its line.

## Using it

- Pick a camera with `?camera=` or the selector.
- Click or tap a track to select it — the card under the image names the
  object, its zones, its sub-label (a recognised face, plate or carrier), its
  score and whether there is a clip to open.
- Toggle lanes to quiet the reel down. Turning the last one off turns them
  all back on; an empty reel reads as broken rather than as a filter.
- Drag the image itself for frame-by-frame.
- Scrubbing near "now" works too — the cache continuously follows the live
  edge, so the newest minute is only a few seconds behind.

## Configuration

The `scrub:` config section:

- `enabled` + `cameras` — which cameras get cached. Each camera costs disk
  and steady CPU, so enroll the ones you actually scrub.
- `cache_dir` — where sheets are written. Must be a real local filesystem
  path with room to grow.
- Interval/thinning/retention settings trade freshness and history depth for
  disk. The defaults follow the design spec and rarely need touching;
  retention pruning runs automatically.
- `preserve_source_aspect` / `format` control how cells are rendered.
- `min_free_bytes` — free-space floor on the cache filesystem; a tick is
  skipped rather than grinding out ENOSPC below it (default 2GiB).
- `recent_interval_s` / `aged_interval_s` — cadence of the two decode
  tiers, finest to coarsest (default 1.0s / 5.0s).
- `match_keyframe_cadence` — generate a camera at its own keyframe cadence
  when coarser than `recent_interval_s`, instead of full-decoding (default
  on).
- `derived_intervals_s` — extra cadences cheaply re-tiled from an existing
  decode tier (default `[60, 300, 900, 3600]`).
- `aged_after_h` — footage age (hours) at which the aged tier's cadence
  takes over from the recent tier's (default 24).
- `retention_days` — how long generated sprite sheets are kept before
  pruning (default 4).
- `cell_w` — sprite-sheet cell width in pixels (default 320); `cell_h` is
  the fallback height used only when the source aspect can't be measured or
  `preserve_source_aspect` is off (default 180).
- `sheet_cols` / `sheet_rows` — sprite-sheet grid size (default 12 × 8).
- `generate_interval_s` — ceiling on the generation loop's tick length
  (default 60s).
- `live_edge_interval_s` — how often the trailing-window (live-edge) pass
  runs; the floor on how stale the newest cell can be (default 20s).
- `sheet_version_grace_s` — how long a superseded still-filling sheet
  version stays servable after a larger version publishes (default 900s).
- `prune_interval_s` — retention-sweep cadence for the in-process generator
  (default 3600s).
- `ffmpeg_concurrency` — cap on concurrent ffmpeg/ffprobe children (default
  3).
- `backfill_segments_per_cycle` / `backfill_time_budget_s` — segment and
  wall-clock caps on the backfill phase per cycle (default 120 / 22s).
- `live_edge_segments` / `live_edge_lookback_s` — cap on the live-edge pass
  per camera per cycle, and how far back it resumes from before jumping
  forward to the edge (default 90 / 900s).
- `derive_time_reserve_s` — wall-clock carved out of
  `backfill_time_budget_s` for the derived-tier decimation pass (default
  5s).

## If it goes wrong

Empty strips: check `/healthz` (`scrub` component) and the
`media_path`/`recordings_path` mapping described in
[First run](/guide/first-run). A camera missing from the picker usually
isn't enrolled in `scrub.cameras`.

Event bars a few seconds off from the pictures: that's detect-vs-record clock
skew, and it's fixable per camera — see
[Event alignment](/guide/settings) on the Settings page.
