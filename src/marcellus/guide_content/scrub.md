---
title: Scrub cache
section: daily-review
order: 1
routes: []
config: ["scrub"]
---

The **scrub cache** is what lets the Elsinore app's reel show frames
instantly: the sidecar pre-renders recordings into **sprite sheets** —
grids of small frames at a fixed cadence — and serves them over the `/v1`
scrub, reel and coverage endpoints. There is no browser page for it; the
app is the viewer.

In the last 24 hours the cache generated **{{stat:scrub_sheets_24h}}**
sprite sheets.

## What it holds

- **Six cadences** — 1s, 5s, 1m, 5m, 15m and 1h per cell. These are exactly
  the sprite cadences the cache generates, and the app picks a rung per
  gesture.
- **Coverage** — which spans were actually recorded. Hatching in the app
  means nothing was recorded there, distinct from a quiet stretch.
- **Events and motion** — the reel endpoint also returns Frigate's events
  (alerts and detections, with object lane, zones and sub-label) and a motion
  trace, so the app can draw them beside the frames.
- **The live edge** — the cache continuously follows "now", so the newest
  minute is only a few seconds behind.

## Configuration

The `scrub:` config section:

- `enabled` + `cameras` — which cameras get cached. Each camera costs disk
  and steady CPU, so enroll the ones you actually scrub.
- `cache_dir` — where sheets are written. Must be a real local filesystem
  path with room to grow, and on a different filesystem from
  `frigate.recordings_path`; the sidecar refuses to enable the cache at
  startup otherwise.
- Interval/thinning/retention settings trade freshness and history depth for
  disk. The defaults follow the design spec and rarely need touching;
  retention pruning runs automatically.
- `preserve_source_aspect` / `format` control how cells are rendered.
  `preserve_source_aspect` (default on) derives each camera's cell height from
  its own aspect ratio, because a 4:3 camera squeezed into a 16:9 cell is
  anamorphic and nothing downstream can undo it. `format` defaults to `jpeg`,
  which measured smaller than `webp` on real camera content.
- `min_free_bytes` — free-space floor on the cache filesystem; a tick is
  skipped rather than grinding out ENOSPC below it (default 2GiB).
- `recent_interval_s` / `aged_interval_s` — cadence of the two decode
  tiers, finest to coarsest (default 1.0s / 5.0s). The aged tier is now
  produced by decimating already-cached recent-tier frames (cheap PIL crops,
  no extra decode) whenever the recent tier already covers a span end to
  end; only a span the recent tier never reached falls back to decoding it
  directly from recordings.
- `match_keyframe_cadence` — generate a camera at its own keyframe cadence
  when coarser than `recent_interval_s`, instead of full-decoding (default
  on).
- `derived_intervals_s` — extra cadences cheaply re-tiled from an existing
  decode tier (default `[60, 300, 900, 3600]`).
- `aged_after_h` — footage age (hours) at which the aged tier's cadence
  takes over from the recent tier's (default 24).
- `retention_days` — how long generated sprite sheets are kept before
  pruning (default 4, matching the measured continuous-recording window, not
  Frigate's `record.retain.days`). Coverage responses report
  `recording_retention_days` and `scrub_retention_days` separately; don't
  read one as the other.
- `cell_w` — sprite-sheet cell width in pixels (default 320); `cell_h` is
  the fallback height used only when the source aspect can't be measured or
  `preserve_source_aspect` is off (default 180).
- `hwaccel` / `hwaccel_device` — hardware decode of recorded segments for
  scrub sheets: `none` (default) or `vaapi`, with the render node in
  `hwaccel_device` (default `/dev/dri/renderD128`). Only decoding moves to
  the GPU; scaling stays in software. A segment whose hardware decode fails
  is retried once in software, and after 3 consecutive such failures
  hardware decode is turned off until the service restarts (one ERROR line
  in the log; `ffmpeg_hw_fallbacks` in the cycle summary counts the
  retries).
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
  3). It is also how many segments of one camera decode in parallel; their
  frames are still fed in segment order. Use 2 on a box shared with Frigate,
  1 for strictly serial decoding. The cycle log's `ffmpeg_s / ffmpeg_wall_s`
  is the parallelism achieved.
- `backfill_segments_per_cycle` / `backfill_time_budget_s` — segment and
  wall-clock caps on the backfill phase per cycle (default 120 / 22s).
- `live_edge_segments` / `live_edge_lookback_s` — cap on the live-edge pass
  per camera per cycle, and how far back it resumes from before jumping
  forward to the edge (default 3 / 900s). The edge takes a camera's
  *newest* segments and leaves any older gap to backfill (newest-first), so
  a small cap keeps every camera fresh: a large one lets a single backlogged
  camera use the whole pass while the others fall behind. 3 is two 10s
  segments per 20s tick plus one of slack.
- `derive_time_reserve_s` — wall-clock carved out of
  `backfill_time_budget_s` for the derived-tier decimation pass (default
  5s).
- `backfill_min_share_s` — minimum wall-clock seconds per cycle reserved for backfill (default 6s); the live-edge pass stops early, between cameras, once the tick has less than this left, so backfill always gets a turn. Set it to 0 on a box with no decode headroom: the live edge then keeps the whole tick (less `decimation_min_share_s`) and backfill only gets what the edge leaves.
- `decimation_min_share_s` — minimum wall-clock seconds per cycle guaranteed to the decimation pass (aged and derived tiers, default 3s), even when the live edge and backfill have already run past the tick. It uses no ffmpeg, only a little Python CPU. 0 disables the guarantee; with `backfill_min_share_s` also 0 the live edge is then unbounded.
- `canvas_cache_sheets` — how many still-growing sheet images the generator keeps in memory so each update decodes only the new frames instead of the whole sheet (default 24, about 37 MB each at 480×270 cells, so roughly 0.9 GB at most). Set it to at least 2 × the number of cameras (one growing recent and one growing aged sheet each); only those two tiers use it. 0 turns the cache off and re-reads every frame on every update, trading CPU for memory.
- `derived_from_live_edge` — feed the derived tiers (`derived_intervals_s`) from the frames the live edge has just decoded, instead of decoding a full recent-tier sheet for every new derived cell just to crop that one cell back out (default on). Only a derived tier that is already caught up to the live edge is fed this way; the decimation pass still builds everything else (first run, catch-up after a restart, older history). Turn it off to build derived tiers only by decimation, as before.
- `reel_motion_deadline_s` — how long a reel request waits for Frigate's motion data before answering without it (default 0.9 s). Frames, coverage and events are the sidecar's own and arrive regardless; when Frigate is slow the motion trace is left out (`motion_unavailable`) and filled in on the next poll once the background fetch lands. 0 waits as long as Frigate takes, as before.

## Highlights

`/v1/highlights/{camera}` serves "take me to the next interesting thing". It
returns **raw tracked-object events, newest first**, not destinations, and
events cluster hard: measured across three cameras, 40-50% of consecutive
highlights were under 45s apart, so one person walking past emits three or
four. Two opt-in parameters, both off by default:

- `cluster_s=45` groups events within that many seconds into one destination
  at the run's earliest start (`events` counts the members, `end` is the latest
  end, `reason`/`score` come from the most confident member). Gaps are measured
  end to start.
- `order=score` ranks by peak confidence instead of recency. Recency stays the
  default because a client scanning for adjacency depends on time order.

`limit` bounds the events considered, not the destinations returned, and its
reach in wall-clock time varies enormously with how busy a camera is
(`limit=100` covered 3.4 hours on one camera and 152 hours on another), so a
client filtering for a sparse label must page. `score` is the event's peak
confidence, which current Frigate keeps in the `data` JSON blob, leaving the
`score`/`top_score` columns NULL.

## If it goes wrong

Empty strips in the app: check `/healthz` (`scrub` component) and the
`media_path`/`recordings_path` mapping described in
[First run](/guide/first-run). A camera missing from the app's picker usually
isn't enrolled in `scrub.cameras`.

Event bars in the app a few seconds off from the pictures: that's detect-vs-record clock
skew, and it's fixable per camera — see
[Event alignment](/guide/settings) on the Settings page.
