---
title: Deployment & upgrades
section: getting-started
order: 2
---

The sidecar runs as a single Python service beside Frigate — bare metal
(systemd), Docker Compose, or the `install.sh` quick path. Full details are
in the repo's `docs/deployment.md`; the essentials:

## Install

**Requirements:** a running Frigate install whose `config.yml`, database
directory and recordings tree are readable from the machine the sidecar runs
on (usually the same box), plus network reach to Frigate's API. The scrub
cache additionally needs ffmpeg (bundled in the Docker image) and MQTT access
for push notifications.

**Quick install (recommended):**

```sh
curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/main/install.sh | sudo bash
```

It uses Docker (image `ghcr.io/helicopterrun/marcellus`) when available,
otherwise a venv plus a systemd unit. It prompts for the deployment-specific
values (Frigate URLs and paths) and writes `sidecar.yml` for you. Re-run the
same command to upgrade.

**Manual, Docker Compose:**

```sh
sudo mkdir -p /opt/marcellus && cd /opt/marcellus
curl -fsSLO https://raw.githubusercontent.com/helicopterrun/marcellus/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/helicopterrun/marcellus/main/.env.example -o .env
$EDITOR .env                                            # point the paths at YOUR Frigate
mkdir -p data config && sudo chown -R 10001:10001 data  # container's non-root uid
docker compose run --rm marcellus init -o /config/sidecar.yml
docker compose up -d
```

Then open `http://<host>:5001`. To build the image locally instead of
pulling, clone the repo and run `docker compose build`.

**Manual, systemd (host process)**, for environments where Docker isn't an
option (for example unprivileged LXCs):

```sh
python3 -m venv /opt/marcellus/venv
/opt/marcellus/venv/bin/pip install "marcellus @ git+https://github.com/helicopterrun/marcellus"
sudo mkdir -p /opt/marcellus/data /etc/marcellus
sudo /opt/marcellus/venv/bin/fsc init -o /etc/marcellus/sidecar.yml \
  --sidecar-db /opt/marcellus/data/marcellus.db
sudo cp contrib/marcellus.service /etc/systemd/system/   # adjust ExecStart to the venv python
sudo systemctl daemon-reload
sudo systemctl enable --now marcellus.service
```

## Install extras

Optional features are Python "extras" — install only what you use:

| Extra | Enables |
|---|---|
| `http2` | faster proxying |
| `annotation` | annotation-offset analysis |
| `enrich` | identity clustering ([Identities](/enrich/clusters)) |

## Upgrading

Docker: `docker compose pull && docker compose up -d`, or re-run the install
script. Systemd: upgrade in the venv, then `systemctl restart marcellus`.
From source:

1. Pull the new code.
2. Re-run `pip install ".[…extras…]"` — **don't skip this**: a code update
   without the install step runs new code against old dependencies and
   misses new ones entirely.
3. Restart the service.
4. Open [`/healthz`](/healthz) and confirm every component reports healthy.

Releases are tagged `vX.Y.Z`; the Docker image is published multi-arch
(amd64 and arm64) with `latest`, `X.Y` and `vX.Y.Z` tags, and
`/v1/capabilities` reports the running version. Upgrading an existing
`frigate-sidecar` deployment is covered in the repo's `docs/deployment.md`
(env vars, config path, systemd units and the DB filename all have
backward-compatible fallbacks).

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
