# Changelog

All notable changes to Marcellus (formerly frigate-sidecar). Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) loosely (pre-1.0: minor releases
may change behaviour). Numbers are pull requests.

## [Unreleased]

### Upgrade notes

- **Event times now include a per-segment correction, on by default.**
  `scrub.segment_clock_correction` (default `true`) adds `0.5 s − f` to the
  per-camera event clock offset, where `f` is the recording segment's own
  sub-second start error (Frigate names segments after whole seconds). Your
  existing offsets — Frigate's `detect.annotation_offset` and the values
  applied from Settings › Event alignment — are left exactly as they are and
  keep their meaning: the shift that is right on average. Over the events any
  offset was fitted on, `f` averages about 0.5 on a drifting camera, so the
  average alignment does not move; only the spread around it shrinks. Where
  the sidecar cannot pin `f` (no recordings rows, a gap, a camera whose clock
  never drifts) the term is zero and the old offset applies alone. For the
  best result, re-run **Measure cameras** (or the calibrator) once after
  upgrading: both now measure with the correction in place, which removes the
  small bias (up to a few hundred milliseconds) an older value can carry. Set
  `scrub.segment_clock_correction: false` to get the previous behaviour
  byte-for-byte.

### Changed

- **Event, review and path times on the reel, highlights, `continues` links
  and encounter linking are corrected for each recording segment's
  sub-second start error** (`analysis/segment_clock.py`). A constant
  per-camera offset left event bars up to about half a second ahead of or
  behind the picture, in a sawtooth that drifts over hours; measured on the
  reference deployment the per-event spread fell from 0.24–0.35 s to
  0.03–0.09 s (standard deviation). Estimated read-only from Frigate's
  `recordings` table, cached per camera in bounded memory, no schema change.
  Times for recent windows can differ from what a client cached before the
  upgrade by up to ±0.6 s; nothing on the wire changes shape.
- **The annotation-offset measurement** (`fsc analysis annotation-offset`,
  Settings › Measure cameras, the calibrator's frames) **measures with the
  correction applied**, so its suggested value is the constant the corrected
  shift expects; results carry `segment_clock_corrected`.

### Added

- **`GET /analysis/segment-clock?camera=&ts=`** — read-only diagnostics: the
  recording segments around `ts` with their estimated start error and how
  tightly it is pinned, and the shift an event at `ts` gets.

## [0.5.0] - 2026-11-18

### Upgrade notes

Written for operators upgrading from 0.4.0. Everything in the 0.4.0 notes
below still applies unchanged, including the push transport and relay
credential behaviour (the new default `push.transport: auto` only changes what
happens when no transport was set; an explicit `relay` or `mock` is untouched).

- **Database durability trade.** Every sidecar database connection now sets
  `synchronous = NORMAL` (WAL mode stays). Commits no longer fsync while holding
  the write lock, which removes the `database is locked` stalls seen on a disk
  shared with Frigate's recordings. The database stays consistent across a crash
  or power cut, but the last few transactions before one may be rolled back.
  Nothing to configure; no schema change.
- **`frigate.verify_tls`** is a new setting, default `true`: certificate checks
  stay strict and existing installs behave exactly as before. A stock Frigate
  serves its sign-in port (8971) with a self-signed certificate; if the sidecar
  cannot reach it, `/healthz` now says `tls_untrusted` and you can set
  `verify_tls: false` (affects Frigate's two origins only).
- **Frigate with sign-in turned off is now admitted automatically.** When
  Frigate reports `auth.enabled: false` and serves a request with no
  credential, the sidecar's own routes also accept requests without a session
  (no more `sidecar.require_frigate_auth: false` for that case). That means
  anything that can reach the sidecar's `bind_host`/`bind_port` can use it,
  exactly as it could use Frigate; keep it on a trusted network or behind a
  proxy. Frigate with sign-in on, `proxy.auth_secret` set, or a login-requiring
  reverse proxy keeps the session requirement.
- **Installer:** re-running `install.sh` on a bare-metal host with a pre-rename
  `frigate-sidecar` install now stops with migration steps instead of creating a
  second user and unit beside it (see "Upgrading from frigate-sidecar" in
  `docs/deployment.md`). `ALLOW_LEGACY_COEXIST=1` keeps the old behaviour.
- **Docker:** `install.sh` and `docker-compose.yml` now pin `:0.5`. The new
  `-full` image (`:0.5-full`, amd64 only, face recognition) is available;
  the default image is unchanged.

### Added

- **`frigate.verify_tls`** (default `true`). Set it to `false` to accept
  Frigate's self-signed certificate: certificate checks are then off for
  connections to Frigate's two origins only (HTTP and WebSocket), never for the
  push relay, APNs, UniFi Protect or external sources. `fsc init` probes the
  sign-in port when it has a Frigate address, picks the scheme that answers,
  and asks whether to trust a self-signed certificate (default yes on a
  private address); `--non-interactive` keeps the strict default and prints a
  warning naming the setting. When `--frigate-url` is given, the sign-in URL now
  defaults to `https://` on the same host, port 8971. The example config uses
  `https://` for that port.
- **Frigate with sign-in turned off.** The sidecar's own routes (`/v1`, pages,
  `/faces`, ...) now admit requests that carry no Frigate cookie when Frigate
  itself serves such callers: its running config reports `auth.enabled: false`
  and its authenticated origin answers a request with no credential. The
  Elsinore app's "no sign-in" servers get the scrub reel, push, Encounters and
  People instead of falling back to Frigate alone. Checked against Frigate and
  cached for `sidecar.auth_cache_ttl_s`, never assumed; a Frigate with sign-in
  on, `proxy.auth_secret` set, or a reverse proxy asking for its own login keeps
  the session requirement. `sidecar.require_frigate_auth: false` is no longer
  needed for such a Frigate. Note that the sidecar is then open to anything
  that can reach its `bind_host`/`bind_port` (guide: "Frigate with sign-in
  turned off").

### Changed

- Guide: new getting-started page "Docker or systemd?" and a "Set up push" page covering the MQTT broker settings and Frigate sign-in.
- Installer: re-running `install.sh` on a bare-metal host that still has a pre-rename `frigate-sidecar` install now stops with migration steps instead of creating a second user and unit beside it; nothing is moved or deleted (`ALLOW_LEGACY_COEXIST=1` overrides).
- **Frigate 0.18 behaviour decisions.** A motion bucket Frigate leaves out is now
  counted as zero motion, so coarse `/v1/motion` and `/v1/reel` traces read the
  same on 0.17 and 0.18 (0.17 output can shift slightly at window edges).
  Zones with `enabled: false` are ignored everywhere: zone hits, push routing,
  the zone map, encounter adjacency and the zone lists the app and settings page
  show; stored per-zone settings are kept for when a zone is re-enabled. A
  fractional `/v1/motion` `scale` is rounded to a whole number (minimum 1)
  before calling Frigate instead of surfacing as motion unavailable.
- Docs: Elsinore works with Frigate alone and Marcellus is optional; Marcellus is
  not published to PyPI (`install.sh` now installs from the repository only);
  the old `frigate-sidecar` image is frozen at 0.3; LAN addresses in tracked
  files replaced with documentation placeholders.
- Push: new default `push.transport: auto` uses the relay once a `relay_key` is set or a device supplies a relay credential (checked per send, no restart), and sends nothing until then; explicit `relay` / `mock` are unchanged. The example config no longer ships a placeholder relay URL.
- Docker: releases also publish `X.Y-full` / `vX.Y.Z-full` / `latest-full` images (amd64) with the `annotation` and `enrich` extras for face recognition.
- Guide: relay credentials, optional `relay_key`, and the `auto` transport.
- Push: the situations wire is retired on the server (#172). Registration no longer reads or stores a `situations` list (older app builds may still send one; it is ignored), and `/v1/push/test/{id}` answers 404. The `push_devices.situations` column stays, unused.

### Fixed

- **A default Frigate could not be reached at all.** Frigate serves its
  signed-in port (8971) over HTTPS with a self-signed certificate by default.
  The sidecar checks certificates and had no way to trust that one, so with a
  stock Frigate every login check failed (everything answered 401), proxied
  calls answered 502 with `CERTIFICATE_VERIFY_FAILED`, and nothing said why;
  only turning TLS off in Frigate worked. The new `frigate.verify_tls` setting
  (below) fixes it, and the cause is now named: one WARNING log line (repeated
  at most hourly while it lasts), `checks.frigate_diagnosis` on `/healthz`
  (`tls_untrusted`), and the login page. The mirror-image mistake, `http://`
  pointed at that HTTPS port (Frigate answers 400), is diagnosed as
  `https_required`. The watchdog no longer counts a certificate failure as a
  hung Frigate.
- **WebSocket proxy no longer logs a traceback when a client closes a live stream.** A client leaving while upstream frames were still arriving left an unretrieved task exception (`Unexpected ASGI message 'websocket.send'` or `ConnectionClosedOK`). Either side closing now ends the relay quietly, cancels the other pump and closes the upstream socket; a genuine error logs one warning line.
- **`database is locked` in scrub generation and the push sweep.** One lock
  failure in a generation cycle no longer fails every later camera: Python left
  the failed write's transaction open, so each later write on the cycle's
  connection failed at once on a stale snapshot. The connection is now rolled
  back after a failure. Sidecar connections use `synchronous = NORMAL` (WAL
  stays), so a commit no longer holds the write lock through an fsync; the
  generation cycle waits up to 10 s for a lock instead of 3 s; the hourly
  superseded-sheet sweep deletes its rows in one short transaction and removes
  files afterwards. A busy lock in scrub generation, the push activity sweep,
  the MQTT consumer or external-source state now logs one warning line instead
  of a traceback.
- **One camera with thousands of aged-tier holes no longer stops scrub
  decimation for every camera after it.** Pass 3 built that camera's span list
  by comparing every hole with every finer-tier bucket (millions of pairs, longer
  than the whole pass), logged `tried=0`, did not count the turn and so started
  the next cycle with the same camera; no later camera got its 5 s tier
  decimated or its stale 1 s buckets retired. The span list is now built in one
  sweep, each camera's turn gets a share of the pass instead of all of it, every
  turn counts and always decimates at least one source sheet, and time the
  other cameras leave goes to the ones that ran out. Backfill's subtraction of
  finer-tier coverage got the same sweep. The cycle log line now ends with
  `pass3 cursor a->b turns=n/N second_round=k out_of_time=<camera>(turns tried
  spans)`, and each aged-tier line reports `setup_s` and `turn_s`. What gets
  generated or retired is unchanged.

## [0.4.0] - 2026-11-15

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
- **Frigate:** developed against Frigate 0.17 and tested against 0.17.2 and 0.18.2 (see
  `docs/frigate-compatibility.md`). Optional features
  need extra config sections (see Added): `unifi_protect`, `external_sources`,
  `wildlife`, `recording_watch`, `encounters`. All are off or inert unless
  configured; unknown old YAML keys are ignored.
- **Relay credentials:** devices now authenticate to the push relay with a
  per-install credential (`push_devices.relay_credential`, additive column;
  `capabilities.push.relay_credentials`). `relay_key` is now optional. A device
  with neither a credential nor a key is skipped with a diagnostic instead of
  being sent unauthenticated, so check the decisions feed after upgrading.
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
- Frigate compatibility tests against 0.17.2 and 0.18.2, with a "Tested Frigate
  versions" table (#168).
- "What leaves your network" privacy page in the guide and docs (#166).
- Per-install relay credentials for push (#169).
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

- Frigate 0.18: `frigate/available` "stopped" state, and list-form and
  disabled zones, are now handled (#168).
- Dead push-to-start tokens are cleared (#169).

### Removed

- The old faces B1 curation stage, v1 push residue, retired situations
  pipeline, family booleans and `alert_all_changes`, legacy redirects.

## [0.3.0] - 2026-08-07

Situations model and Live Activities (phases 1 and 2), snooze RPC deprecation.

## [0.2.0] - 2026-08-05

Distribution tooling: GHCR release workflow, `fsc init`, install script.
