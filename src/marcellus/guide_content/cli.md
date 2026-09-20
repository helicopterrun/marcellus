---
title: fsc CLI reference
section: operations
order: 3
routes: []
config: []
---

`fsc` (alias `marcellus`) is the Marcellus command-line tool — same
settings/config as the server, run one-off or from cron/systemd timers.

## Top-level

- `fsc serve` — run the HTTP server.
- `fsc watchdog` — run the Frigate health watchdog (restarts the Frigate
  container on a hung backend).
- `fsc init` — generate `sidecar.yml` interactively or from flags
  (`--frigate-url`, `--proxy-url`, `--frigate-config`, `--frigate-db`,
  `--recordings-path`, `--sidecar-db`, `--bind-host`, `--bind-port`,
  `--non-interactive`, `--force`), then sanity-checks Frigate reachability,
  the DB path, and the recordings path.
- `fsc version` — print the installed version.
- `fsc backup <dest>` — back up the sidecar DB, session secret, and resolved
  config to a directory or `.tar.gz` (not scrub cache or face models).
- `fsc restore <src> --force` — restore a backup made by `fsc backup`; stop
  marcellus first.

## `fsc triage`

- `sample --days --n --camera --label --seed` — sample borderline events as
  JSONL on stdout.
- `record --event-id --label (fp|tp|skip) --note --session --force` — record
  a triage label for one event.
- `clear --event-id` — delete the triage label for one event.
- `stats` — print counts of triage labels.

## `fsc face-capture`

- `scan` — capture the ID camera's full-res frame for every new trigger
  event.
- `prune` — drop captures past `face_capture.retention_days`.
- `stats --days` — counts by status/review plus the last-run heartbeat.

## `fsc analysis`

- `score-histogram --days --camera --label --min-samples --json` — score
  distribution + min_score/threshold suggestions per (camera, label).
- `motion-rate --days --json` — per-camera event rate, spikiness, and
  suggestions.
- `fps-budget --json` — detector inference budget vs. configured demand.
- `motion-active --days --json` — per-camera raw motion activity and yield.
- `motion-compare --baseline --target --json` — A/B motion comparison
  across two date ranges.
- `zone-hits --days --camera --json` — per-camera zone hit-map + mask
  candidates.
- `pull-events --days --camera --label` — dump events as JSONL on stdout.
- `annotation-offset --days --camera --json` — measured
  `detect.annotation_offset` (ms) per camera via template matching; requires
  the `[annotation]` extra.

## `fsc scrub`

- `generate --camera --json` — run one generation cycle now (forward edge).
- `backfill --camera --days` — one-time history fill for a camera, looping
  the generator until it catches up to now (capped at `scrub.retention_days`).
- `verify --camera --interval --repair` — find sheets whose index entry
  claims more cells than they render; `--repair` republishes them at their
  true count.
- `prune --camera --drop-interval` — drop sheets/buckets past
  `scrub.retention_days`, oldest-first; `--drop-interval` unconditionally
  deletes every bucket/sheet at a given interval regardless of retention.
- `coverage --camera` — print what's generated for a camera (debug).
