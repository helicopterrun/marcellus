---
title: Deployment & upgrades
section: operations
order: 1
---

The sidecar runs as a single Python service beside Frigate — bare metal
(systemd), Docker Compose, or the `install.sh` quick path. Full details are
in the repo's `docs/deployment.md`; the essentials:

## Install extras

Optional features are Python "extras" — install only what you use:

| Extra | Enables |
|---|---|
| `http2` | faster proxying |
| `annotation` | annotation-offset analysis |
| `enrich` | identity clustering ([Identities](/enrich/clusters)) |

## Upgrading

1. Pull the new code.
2. Re-run `pip install ".[…extras…]"` — **don't skip this**: a code update
   without the install step runs new code against old dependencies and
   misses new ones entirely.
3. Restart the service.
4. Open [`/healthz`](/healthz) and confirm every component reports healthy.

Database migrations are automatic: the sidecar's schema is applied
idempotently at startup, and Frigate's database is only ever read.

## Where state lives

Everything the sidecar owns is in its SQLite file plus the scrub cache
directory. The scrub cache and face-model directory are regenerable from
Frigate's own recordings/DB, so only the DB, `.session_secret`, and config
need backing up.

## Backup / restore

`fsc backup <dest>` (a directory, or a single `.tar.gz` file) writes those
three: the sidecar DB, `.session_secret`, and the resolved config. `fsc
restore <src> --force` brings them back — stop the service first (an
un-stopped restore can corrupt the DB's WAL); see `docs/deployment.md` for
the full systemd stop/restore/start sequence and a cron example.
