# Changelog

All notable changes to Marcellus (formerly frigate-sidecar). Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) loosely (pre-1.0: minor releases
may change behaviour). Numbers are pull requests.

## [0.4.0] - Unreleased

About 330 commits since 0.3.0. Written for operators upgrading from 0.3.0.

### Upgrade notes

- **Renamed.** The project, package, CLI and defaults are now
  `marcellus`. The Docker image is `ghcr.io/helicopterrun/marcellus` (0.3.x was
  published as `ghcr.io/helicopterrun/frigate-sidecar`), and the compose service
  and container are `marcellus`. `fsc` still works. See "Upgrading from
  frigate-sidecar" in `docs/deployment.md`.
- **Env vars** are now prefixed `MARCELLUS_`; old `FRIGATE_SIDECAR_*` still
  load with a one-time deprecation warning. The config file is looked up at
  `/etc/marcellus/sidecar.yml` first, then the old path.
- **Bare-metal installs:** re-running `install.sh` creates the new
  `marcellus` user, paths and unit beside the old ones; it does not migrate
  them. Move data by hand or keep the old unit running.
- **Database file** is now `marcellus.db`; `fsc restore` accepts either
  name. Schema changes are additive and applied automatically at start-up
  (new tables for encounters, people, decisions, external sources, review inbox);
  take an `fsc backup` first anyway.
- **systemd unit** now runs as a non-root hardened service. Check that the
  service user can read Frigate's config and DB and write the data directory.
- **Docker:** `install.sh` and `docker-compose.yml` now pin `:0.4`. Dependencies
  have major-version caps. Python 3.10+ still required.
- **Elsinore app:** the app is paired with this release. Attention ladder v3
  (four levels, four places) replaces the older outcome tables; older
  sidecars are still served by legacy payloads, but 0.4 is expected by current
  app builds. Run the app's contract-fixture sync after wire changes.
- **Frigate:** developed and run against Frigate 0.17. Optional features
  need extra config sections (see Added): `unifi_protect`, `external_sources`,
  `wildlife`, `recording_watch`, `encounters`. All are off or inert unless
  configured; unknown old YAML keys are ignored.
- **Push privacy change:** person names are no longer sent in anything pushed
  to devices (#140). Re-check any custom copy.
- Optional hardware decode: `scrub` and `wildlife` have VAAPI options
  (off by default).

### Added

- Encounters: cross-camera grouping of review segments, feed API with paging
  and severity filter, human split/move/merge/undo, learned camera transition
  times and `/v1/topology`, observations with direction, suggested
  continuations, a repair command, a wildlife gallery with hover playback
  (#59-#101).
- Encounter-aware push: one notification per encounter (#80).
- `GET /v1/timeline` composing per-camera reels into a synchronized window (#76).
- Attention ladder v3: Ignore, Quiet, Glance, Urgent across four places (#105).
- Alerts: durable decisions feed, push status, delivery receipts, device
  detail, one-tap silence, per-device Connection Doctor health (#54, #55, #58).
- Settings: full policy editor, runtime tuning overrides, effective config (#66).
- People ("Who's There"): permanent face IDs, generated handles, `/v1/people`,
  follows, merge suggestions and dismissals, encounters by person, people on
  encounters, `GET /v1/people/by-events`, encounter titles naming people
  (#137-#160). Blind pair accuracy labelling page at `/enrich/accuracy`.
- Faces: high-res cross-camera capture, best-face crops and stacks, detection
  score floor, backfill button, numpy similarity hints.
- Wildlife finder: MegaDetector and SpeciesNet over segments Frigate tracked
  nothing in; optional VAAPI keyframe decode (#116, #156).
- External event sources (Bernardo first) with vendored contract fixtures
  (#98, #99).
- UniFi Protect: doorbell-ring push, device health poll, `/healthz` check,
  `/v1/protect/status`, LCD reply presets and ring snapshot proxy (#88-#92).
- Review inbox: daily Frigate+-ranked queue with an auto-send outbox (#119).
- Recording watch: warn when a camera stops recording (#147).
- Scrub cache: single-cell sheet endpoint, aged tier by decimation,
  newest-first live edge, optional VAAPI decode, per-pass timing log (#87,
  #113-#123).
- Camera map and calibration tooling (/map), manual event-offset calibrator,
  GET `/v1/events/search`, `/v1/events/{id}/related`, `/v1/push/map/track`.
- Backup/restore CLI, login rate limiter, settings sync CLI.
- `live_activities.summary_row` to opt out of the post-LA summary row (#97).
- Login landing page, new Marcellus look and icon, docs site (Starlight) with
  app screenshots (#161-#165).

### Changed

- Push pipeline rebuilt as a single card pipeline; situations retired.
  Live Activities are device-scoped with pacing, tombstones and escalations.
- Quiet hours use the device time zone, not the host clock (#148).
- Camera list comes from Frigate's config, not its zones (#157).
- Encounter reconcile and wildlife/face writes hold the DB lock briefly
  (#135, #136, #155).
- Frigate proxy hardened against pool exhaustion and leaks; `/vod` has its
  own pool (#51, #53, #106, #108).
- `/healthz` reflects background-task liveness and probes Frigate.
- Wire timestamps are ISO8601 Z (#56); unified error envelope and typed wire
  models with golden fixtures (#48).
- Web UI consolidated into one theme with mobile tab bar and shared assets.

### Fixed

- Live Activity identity, payload casing and duplication bugs; silent card
  pushes; resolve rows left for devices that never got the card (#145).
- Concurrent-INSERT race in encounter upsert (#84).
- Proxy no longer logs client disconnects mid-handshake as errors (#142).
- Scrub backfill starvation, redundant re-decodes, corrupt-sheet repair,
  ghost cameras for renamed ones.
- External-source one-shot notices no longer read as standing alerts (#149).
- Retry of the WAL switch on first open; deploy health URL (#100).

### Removed

- The old faces B1 curation stage, v1 push residue, retired situations
  pipeline, family booleans and `alert_all_changes`, legacy redirects.

## [0.3.0] - 2026-08-07

Situations model and Live Activities (phases 1 and 2), snooze RPC deprecation.

## [0.2.0] - 2026-08-05

Distribution tooling: GHCR release workflow, `fsc init`, install script.
