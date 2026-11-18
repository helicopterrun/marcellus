---
title: Docker or systemd?
section: getting-started
order: 2
---

Marcellus runs as one service next to Frigate. There are two ways to run it:
as a Docker container, or as a Python program managed by systemd. Both give
you the same app. Pick with the questions below.

## Pick one

1. **Is Docker installed on the machine that will run Marcellus, with the
   `docker compose` plugin?** Use Docker. It is the simplest path: the image
   already contains ffmpeg (the scrub cache needs it) and upgrading is one
   command.
2. **No Docker, or you cannot use it** (for example an unprivileged LXC
   container)? Use systemd. You need Python 3.10 or newer, systemd, and ffmpeg
   installed from your package manager.

If you run the [quick install](/guide/deployment) script, it makes this choice
for you the same way: it installs the Docker deployment when it finds `docker`
on the machine (and stops if the compose plugin is missing), and otherwise
installs the systemd one. Either way it ends by running the
[install checklist](/guide/check-your-install) and printing it, then one line:
everything works, or how many checks need attention and the exact command to
run again. A problem there never undoes the install.

## What either way needs

Marcellus reads Frigate's files directly, so the machine must be able to see:

- Frigate's `config.yml`.
- Frigate's database **directory** (not just `frigate.db`).
- Frigate's recordings folder.
- Frigate's two web ports over the network: the plain one (usually `5000`) and
  the signed-in one (usually `8971`). Frigate serves the signed-in one over
  HTTPS with a self-signed certificate by default; see "Frigate's HTTPS
  sign-in port" in [First run](/guide/first-run).

All of these are read-only. If Frigate runs on a different machine, mount its
folders (for example over NFS) before you start.

## Docker

- Files live in `/opt/marcellus`: `.env` (where Frigate's folders are on the
  host: `FRIGATE_CONFIG`, `FRIGATE_DB_DIR`, `FRIGATE_RECORDINGS`, which the
  quick install detects, confirms and checks before starting),
  `config/sidecar.yml`, and `data/` (Marcellus's own database).
- The container reaches Frigate and your MQTT broker through host networking,
  which is why the default needs no port settings. The page is at
  `http://<host>:5001`.
- Secrets such as the MQTT password go in `.env`; `init` can write the line for
  you, and see [Set up push](/guide/push-setup).
- Upgrade: `docker compose pull && docker compose up -d`, or re-run the quick
  install command.
- Face recognition needs the larger `-full` image (amd64 only); see
  [Deployment & upgrades](/guide/deployment).

## systemd

- Files live in `/opt/marcellus` (a Python venv and `data/`) and
  `/etc/marcellus/sidecar.yml`. The service runs as its own `marcellus` user,
  which needs read access to Frigate's files. If it cannot read them, add it
  to a group that can, for example `usermod -aG <frigate-group> marcellus`.
- Secrets such as the MQTT password go in `/etc/marcellus-push.env` (root-owned,
  mode `0600`); run `init` as root and it writes the line for you, and see
  [Set up push](/guide/push-setup).
- Restart after editing the config: `systemctl restart marcellus`. Logs:
  `journalctl -fu marcellus`.
- Optional features (face recognition, wildlife, UniFi Protect) are extras you
  install into the venv yourself; see
  [Deployment & upgrades](/guide/deployment).

## Then

Follow [Deployment & upgrades](/guide/deployment) for the exact commands, then
[First run](/guide/first-run). When Marcellus is up and healthy, turn on alerts
with [Set up push](/guide/push-setup).
