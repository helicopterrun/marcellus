---
title: "Frigate compatibility"
---

Marcellus depends on three things it does not own: Frigate's SQLite database,
which it opens read-only and queries directly; Frigate's HTTP API; and
Frigate's `config.yml` and MQTT topics. This page lists every such dependency
and which Frigate releases they are checked against.

## Tested versions

| Frigate | Fixture tests (CI) | Live server |
|---|---|---|
| 0.17.2 | yes | yes, the reference deployment |
| 0.18.2 | yes | no |

**Fixture tests** run in the normal `pytest` job (`tests/test_frigate_compat.py`),
once per version, against fixtures generated from that release's upstream
source by `tools/frigate_schema_fixture.py`:

- `tests/fixtures/frigate_schema/<tag>.sql` holds the schema that Frigate's own
  peewee migrations build. The generator runs the tag's `migrations/` against an
  empty SQLite file and dumps `sqlite_master`. The tests seed rows the way that
  release writes them, then run Marcellus's real read paths: the reel, reviews,
  coverage, highlights, triage detail, encounters reconcile, analysis pulls and
  the Frigate+ outbox checks.
- `tests/fixtures/frigate_api/<tag>.json` holds the release's route table and
  request/query models, parsed from `frigate/api/` with `ast` (never run). The
  tests check that every endpoint the sidecar calls exists with the method and
  auth dependency it assumes, and that the requests it sends use only fields
  that release accepts.

**Live server** means a running Frigate of that version has served a
production Marcellus. Only 0.17 has done so. A 0.18 deployment is covered by the
fixtures and the source review below, but has not been run live.

To add a release, run the generator for the new tag (its docstring has the
command), add the tag to `FRIGATE_VERSIONS` in the test module, and update
this page.

## 0.17.2 → 0.18.2: what changed for Marcellus

Sources: upstream `frigate/models.py`, `migrations/`, `frigate/api/**`,
`frigate/comms/mqtt.py`, `frigate/config/camera/*.py` and the 0.18.0 release
notes, at tags `v0.17.2` and `v0.18.2`.

| Dependency | 0.18 change | Marcellus |
|---|---|---|
| `event` table | None. Migrations 001–032 are identical in both releases, and 0.18 adds only 033–035. | Compatible |
| `event.data` JSON | Adds `snapshot_clean`, `snapshot_frame_time`, `snapshot_area` and `snapshot_estimated_speed` (`frigate/events/maintainer.py`). | Compatible (keys are additive and unread) |
| `reviewsegment` table and `data` | None (`frigate/review/maintainer.py` writes the same keys). | Compatible |
| `recordings` table | Migration 035 adds `motion_heatmap TEXT NULL`. | Compatible (column unread) |
| `export` / `exportcase` | Migration 034 adds `export.export_case_id`, and migration 033 adds the new `exportcase` table. | Not read |
| `PUT /api/config/set` | Body gains an optional `skip_save`. Writes now hold a file lock and return **503** while it is held. Validation errors return 400 with a message. | Compatible. Any non-200 already raises `FrigateAPIError` |
| `GET /api/review/activity/motion` | Zero-motion gap buckets are now **dropped**. In 0.17 they were returned as `motion: 0, camera: ""`. | **Fixed**: a fetch bucket with no point now counts as zero motion (see "Resolved decisions") |
| `GET /api/stats` | Non-admin callers get per-camera filtering and `cmdline` stripped. | Compatible: the sidecar calls the internal port, which is treated as admin |
| `GET /api/events/{id}/snapshot.jpg` | Rendered from the clean WebP. Omitted query params now default to the camera's `snapshots` config, and the response adds an `X-Frame-Time` header. | Compatible (sends only `bbox` and `height`) |
| `POST /api/events/{id}/plus`, `PUT …/false_positive` | Plus calls moved to a thread pool. The 400 text is now "Unable to find clean snapshot". | Compatible |
| `POST /api/faces/train/{name}/classify` | Names are sanitised with `safe_join` / `sanitize_path_component`. | Compatible for plain names |
| Other routes the sidecar calls | Same method, path and auth in both releases. | Compatible |
| Role enforcement | The internal-port check now reads `networking.listen.internal_port` (default 5000) and is no longer a literal `5000`. Media requests on the authenticated port are now checked per role and camera (`deny_response_for_media_uri`). | Compatible while `frigate.base_url` is the internal port |
| `frigate/available` (MQTT) | On a clean shutdown 0.18 publishes a retained **`stopped`**. | **Was broken, now fixed**: it was read as online |
| `config.yml` zones | Zones gain `enabled` (default true). `coordinates` may be a string or a list in both releases. | **Fixed**: list-form coordinates were dropped, and disabled zones were still used (now dropped everywhere) |
| `config.yml` masks, genai, `snapshots.clean_copy`, `record.sync_recordings` / `export.timelapse_args` | The 0.18 config migrator rewrites or removes these keys. | Not read |
| Exports (`/api/export…`) | `DELETE /api/export/{id}` is **removed**; use `POST /api/exports/delete {"ids": [...]}`. `POST /api/export/{camera}/start/{s}/end/{e}` drops `playback` (ignored, so timelapse needs the new `/export/custom/…` endpoint), returns **202** with `status: "queued"`, and can return 503 when the queue is full. `GET /api/exports` rows gain `export_case_id`. | The sidecar never calls these; it proxies them unchanged for clients. Client callers must adapt |

## Resolved decisions

- **Missing motion buckets are zero motion.** 0.18 no longer returns the
  zero-motion gap rows from `/api/review/activity/motion`. `/v1/motion` and
  `/v1/reel` now fill the fetch-scale grid (at most 300 s buckets, anchored at
  the window start) with zeros before averaging into a wider bucket
  (`aggregate_motion(..., fetch_scale=...)` in `scrub/motion.py`), so a coarse
  trace reads the same on both releases
  (`test_coarse_motion_trace_is_version_independent`). One consequence: 0.17
  output can change slightly at the edges of a window, where Frigate never sent
  gap rows and those slots now count as zero. The `motion_unavailable` flag is
  unchanged: an unreachable Frigate is "unavailable", not zero.
- **Disabled zones drop out everywhere.** `zones.load_camera_zones` is the one
  place zones are read from `config.yml`, and it skips zones with
  `enabled: false` (0.17 has no flag, so every zone is enabled). That removes
  them from zone hits, the push settings `available_zones` / `available_openings`,
  the floorplan zone map, the settings page, and encounter adjacency. Push
  routing also ignores them: review events drop a disabled zone name, and the
  attention ladder ignores a stored zone override for one. Stored per-zone
  settings (class, name, override) stay in the sidecar's settings, so
  re-enabling the zone restores them. A zone counts as disabled only when no
  camera defines it as enabled.
- **Fractional motion scales are rounded.** Frigate's `scale` is an `int`, so
  the sidecar rounds the scale it sends to the nearest whole number (minimum 1)
  before calling it. The response grid still uses the scale the client asked
  for.

## Inventory: Frigate database

Only three tables are read. `timeline`, `regions`, `previews`, `export`,
`user`, `trigger` and `userreviewstatus` are not.

| Table | Columns read | JSON read | Main readers |
|---|---|---|---|
| `event` | `id`, `camera`, `label`, `sub_label`, `start_time`, `end_time`, `zones` (JSON list, also `json_each`), `has_clip`, `has_snapshot`, `false_positive`, `plus_id`, `retain_indefinitely`, `score` / `top_score` (column fallback only, since Frigate leaves them NULL), `area` / `ratio` (column-gated), `data` | `data.score`, `top_score`, `box` (`[x, y, w, h]`, also via `json_extract`), `region`, `type`, `attributes`, `max_severity`, `path_data` (`[[x, y], t]` or `[x, y, t]`) | `routes/scrub.py` (reel, highlights, cross-camera), `routes/triage.py`, `routes/search.py`, `routes/analysis.py`, `routes/inbox.py`, `triage/*`, `analysis/*`, `encounters/observations.py`, `faces/enrich.py`, `faces/crosscam.py`, `wildlife/finder.py` |
| `reviewsegment` | `id`, `camera`, `start_time`, `end_time`, `severity`, `data` | `data.objects`, `zones`, `detections`, `sub_labels` | `routes/scrub.py` `_reviews_json`, `encounters/service.py` reconcile, `encounters/repair.py` |
| `recordings` | `camera`, `start_time`, `end_time`, `path`, `motion`, `objects` | none | `db.py` coverage, `scrub/generator.py`, `routes/scrub.py` camera discovery, `routes/analysis.py`, `wildlife/finder.py` |

Several readers probe columns first with `PRAGMA table_info(event)` or catch
`sqlite3.Error` and degrade. A schema break would then show up as missing
data, not as an error, which is why the fixture tests assert on values.

## Inventory: Frigate HTTP API

Except where noted, the sidecar calls `frigate.base_url`, Frigate's internal
unauthenticated port, which Frigate treats as the admin role. Browser and app traffic goes through
the catch-all proxy to `frigate.proxy_base_url`, the authenticated port, with
the client's own cookie.

| Method and path | Frigate auth | Caller | Sends | Reads |
|---|---|---|---|---|
| `GET /api/version` | public (behind nginx auth on the authenticated port) | `auth.py` session check (proxy URL, client cookie), health/status | (none) | status code |
| `POST /api/login` | public | proxied; rate-limited in `auth.py` | (client body) | status code |
| `GET /api/stats` | any authenticated | `routes/status.py`, `analysis/fps_budget.py` | (none) | `detectors.*.inference_speed`, `detection_fps`, `cameras.*.{detection_fps,skipped_fps}`, `cpu_usages`, `gpu_usages`, `service.{storage,uptime}` |
| `GET /api/config` | any authenticated | `recording_watch.py`, `fps_budget`, `plus_enabled` | (none) | `cameras.*.{enabled,record.enabled,detect.fps}`, `plus.enabled` |
| `GET /api/config/raw` | admin | `routes/push_settings.py` zone import (proxy URL, client cookie, so the user must be an admin) | (none) | YAML text |
| `PUT /api/config/set` | admin | `FrigateClient.set_annotation_offset` | `{requires_restart: 1, config_data: {cameras: {<cam>: {detect: {annotation_offset}}}}}` | status code |
| `POST /api/restart` | admin | `FrigateClient.restart` | (none) | status code |
| `GET /api/events` | any authenticated | `push/mqtt.py` backfill | `after` | `id`, `camera`, `label`, `start_time` |
| `GET /api/events/{id}/thumbnail.jpg` | none declared (camera checked in handler) | `FrigateClient.event_thumbnail`, triage sampler | (none) | image |
| `GET /api/events/{id}/snapshot.jpg` | none declared (camera checked in handler) | `event_snapshot_jpeg` (`bbox`, `height`), push thumbnails | query params | image |
| `POST /api/events/{id}/sub_label` | admin | `FrigateClient.set_sub_label` (faces) | `{subLabel, subLabelScore?}` | status code |
| `POST /api/events/{id}/plus` | admin | `FrigateClient.submit_plus` (triage outbox) | `{include_annotation: 1}` | `success`, `plus_id`, `message` |
| `PUT /api/events/{id}/false_positive` | admin | `FrigateClient.submit_false_positive` | (none) | `success`, `plus_id`, `message` |
| `POST /api/{camera}/plus/{frame_time}` | camera access | `FrigateClient.submit_plus_frame` | (none) | `success`, `message` |
| `GET /api/review/activity/motion` | any authenticated | `/v1/motion`, `/v1/reel` | `cameras`, `after`, `before`, `scale` | `[{start_time, motion}]` |
| `GET /api/{camera}/recordings/summary` | camera access | `analysis/motion_active.py` | (none) | summary rows |
| `GET /api/{camera}/recordings/{ts}/snapshot.jpg` | camera access | `recording_snapshot`, faces crosscam, annotation offset | (none) | image |
| `GET /api/{camera}/latest.jpg` | camera access | push thumbnails (fallback) | (none) | image |
| `POST /api/faces/train/{name}/classify` | (none declared) | `FrigateClient.train_face` | `{training_file}` | `success`, `message` |

## Inventory: config file and MQTT

- `config.yml`, read directly from `frigate.config_path` (`zones.py`,
  `frigate_config.py`, `faces/crosscam.py`): `cameras` (names), `cameras.*.zones.*.{coordinates,objects,inertia,loitering_time,friendly_name,enabled}`,
  `record.{continuous,motion,retain}.days` (global and per camera), and
  `cameras.*.detect.{annotation_offset,width,height}`.
- MQTT topics: `frigate/reviews`, `frigate/events`, and `frigate/available`
  (`online` / `offline`, plus `stopped` from 0.18).
