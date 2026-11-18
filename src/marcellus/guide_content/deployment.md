---
title: Deployment & upgrades
section: getting-started
order: 3
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
curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0/install.sh | sudo bash
```

It uses Docker (image `ghcr.io/helicopterrun/marcellus`) when available,
otherwise a venv plus a systemd unit. Everything it installs comes from the one
release named in the address (`v0.5.0`): the compose file, the image's minor
train (`:0.5`), and, without Docker, that release's wheel (no git needed).
Re-run the same command to upgrade; with a newer release's address it moves to
that release.

On Docker the script settles Frigate's folders **before** it starts anything,
because a wrong host path would be mounted as an empty folder owned by root and
nothing would complain. It looks for them (a running Frigate container's
mounts, then common locations), shows what it found, and asks you to confirm or
type each one when run in a terminal (`sudo bash install.sh`). It then writes
them to `/opt/marcellus/.env`, checks each (exists, right kind, readable; an
empty recordings folder only warns, since a new Frigate has none yet), starts
the container, waits for it to report healthy and prints the address. Piped from
`curl` it cannot ask: it proceeds only if it detected all three and they check
out, otherwise it stops with nothing started and tells you what to pass:

```sh
curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0/install.sh \
  | sudo FRIGATE_CONFIG=/opt/frigate/config.yml FRIGATE_DB_DIR=/opt/frigate/database \
    FRIGATE_RECORDINGS=/opt/frigate/media/recordings bash
```

`FRIGATE_CONFIG` is Frigate's `config.yml` file, `FRIGATE_DB_DIR` the folder
that contains `frigate.db`, `FRIGATE_RECORDINGS` Frigate's `recordings` folder.
Values already in `.env` are kept on a re-run (and just re-checked).
`MARCELLUS_VERSION=main` installs the development branch on purpose.

**Manual, Docker Compose:**

```sh
sudo mkdir -p /opt/marcellus && cd /opt/marcellus
curl -fsSLO https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0/.env.example -o .env
$EDITOR .env                                            # point the paths at YOUR Frigate
mkdir -p data config && sudo chown -R 10001:10001 data  # container's non-root uid
docker compose run --rm marcellus init -o /config/sidecar.yml
docker compose up -d
```

`init` runs inside the container, so the paths it shows are **as the container
sees them**, not your host paths: accept the config file and database it finds
(`/opt/frigate/config.yml`, `/opt/frigate/database/frigate.db`), and for the
recordings folder enter the same host path you set as `FRIGATE_RECORDINGS` (it
is mounted at the same path inside the container). `init` reads Frigate's own
settings and asks you to confirm them (see [First run](/guide/first-run)); its
optional "Set up push alerts now?" question is covered in
[Set up push](/guide/push-setup).

Then open `http://<host>:5001`. To build the image locally instead of
pulling, clone the repo and run `docker compose build`.

**Manual, systemd (host process)**, for environments where Docker isn't an
option (for example unprivileged LXCs):

```sh
python3 -m venv /opt/marcellus/venv
/opt/marcellus/venv/bin/pip install \
  "https://github.com/helicopterrun/marcellus/releases/download/v0.5.0/marcellus-0.5.0-py3-none-any.whl"
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
| `wildlife` | the wildlife detector |
| `unifi_protect` | UniFi Protect doorbell rings |

The default Docker image and `install.sh` install only `http2`. For face
recognition in Docker, use the `-full` image instead (below). For the other
extras, add them yourself (`pip install "marcellus[wildlife] @ git+https://github.com/helicopterrun/marcellus"` in the venv) or
build your own image with `--build-arg EXTRAS=http2,wildlife`.

**Docker, with face recognition.** Each release also publishes
`ghcr.io/helicopterrun/marcellus:X.Y-full` (also `vX.Y.Z-full` and
`latest-full`), which adds the `annotation` and `enrich` extras. It is built for
amd64 only (face recognition's insightface library publishes no prebuilt
packages and compiles from source, too slow under CI emulation; on arm64 build
it yourself with `--build-arg EXTRAS=http2,annotation,enrich`), and it is several hundred MB larger than the slim image. The face
model (about 300 MB) is downloaded the first time it is needed, into
`face_enrich.model_dir`; set that to `/data/models` so it lives on the data
volume and survives container re-creation. To switch, set
`SIDECAR_IMAGE=ghcr.io/helicopterrun/marcellus:0.4-full` in `.env` and run
`docker compose up -d`.

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

v0.3.0 was published as `ghcr.io/helicopterrun/frigate-sidecar` before the
rename. That image is frozen at 0.3; the current image is
`ghcr.io/helicopterrun/marcellus`.

Releases are tagged `vX.Y.Z`; the Docker image is published multi-arch
(amd64 and arm64) with `latest`, `X.Y` and `vX.Y.Z` tags (the `-full` variant,
amd64 only, adds a `-full` suffix to each), and
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
