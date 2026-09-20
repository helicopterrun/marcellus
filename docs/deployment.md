# Deployment notes

The README's [Install](../README.md#install) section covers the common paths.
This file collects the details that don't fit there.

## Layout

| Path | Purpose |
|---|---|
| `/opt/marcellus` | install dir (`.env`, compose file or venv, `data/`) |
| `/opt/marcellus/data` | sidecar SQLite DB + scrub sprite cache (read-write) |
| `config/sidecar.yml` (Docker) or `/etc/marcellus/sidecar.yml` (systemd) | configuration; generate with `fsc init`, full reference in `config/sidecar.example.yml` |

Frigate's config, database **directory** (WAL — never mount `frigate.db`
alone), and recordings tree are consumed read-only.

## systemd

`contrib/marcellus.service` is the reference unit; `install.sh` writes a
copy with the venv path substituted. Notes:

- `KillMode=mixed` is deliberate: the default control-group kill SIGTERMs
  in-flight ffmpeg children out from under the scrub generator, which then
  reads as a camera fault. Keep it.
- The unit runs as a dedicated `marcellus` system user under
  `ProtectSystem=strict` (install.sh creates the user and chowns the install
  dir and `/etc/marcellus`). Writable paths are only the install dir,
  the config dir, and the scrub cache (`ReadWritePaths` — install.sh derives
  the cache dir from the live config; add it by hand if you move it later).
- `EnvironmentFile=-/etc/marcellus-push.env` is read after the unit's own
  `Environment=` line, if present. It's an optional, 0600 root-owned file for
  secrets you'd rather not put in the unit file itself, e.g.
  `MARCELLUS_PUSH__MQTT_PASSWORD=...`. The leading `-` means the unit starts
  fine when the file doesn't exist.

### Granting the service user access to Frigate's files

The sidecar reads Frigate's `config.yml`, database directory, and recordings
tree. After the first install (or upgrade from a root unit), verify:

```sh
sudo -u marcellus test -r /opt/frigate/config.yml && echo config ok
sudo -u marcellus test -r /opt/frigate/database/frigate.db && echo db ok
sudo -u marcellus ls /mnt/frigate-storage/recordings >/dev/null && echo recordings ok
```

If any fail, either add the user to the group that owns those paths
(`usermod -aG <group> marcellus`, then restart the unit) or grant
ACLs directly:

```sh
setfacl -R -m u:marcellus:rX /mnt/frigate-storage/recordings
setfacl -m d:u:marcellus:rX /mnt/frigate-storage/recordings   # future files
setfacl -R -m u:marcellus:rX /opt/frigate/database
setfacl -m u:marcellus:r /opt/frigate/config.yml
```

Frigate runs SQLite in WAL mode, so the whole `database/` directory matters
(`frigate.db-wal`/`-shm` included). If the sidecar logs "unable to open
database file" despite read access, the `-shm` file needs group/ACL write
for read-only WAL clients on your SQLite build — extend the ACL to `rwX` on
`frigate.db-shm` only.

## Optional units

- `contrib/frigate-watchdog.service` — external Frigate health watchdog
  (`watchdog.enabled: true`); restarts the Frigate container when its backend
  hangs while the container still reads "Up". Needs access to the Docker CLI.

It is opt-in and independent of the main server.

### Self-heal healthcheck timer (recommended in prod)

`contrib/marcellus-healthcheck.{sh,service,timer}` poll `/healthz`
every 60s and `systemctl restart marcellus` after **three consecutive**
failures -- a second line of defense alongside the main unit's own
`Restart=on-failure`, for the case where the process is alive but wedged
(event loop stuck, upstream pool exhausted) rather than crashed. A single
503 never restarts: `/healthz` also reports MQTT reconnects and late scrub
cycles, which clear on their own. The script reads `bind_host`/`bind_port`
from the config (prod binds a LAN address, not loopback). Install and enable
it on prod:

```sh
sudo install -m 0755 contrib/marcellus-healthcheck.sh /usr/local/bin/marcellus-healthcheck
sudo install -m 0644 contrib/marcellus-healthcheck.service \
                     contrib/marcellus-healthcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marcellus-healthcheck.timer
```

Watch it with `journalctl -u marcellus-healthcheck`. Override the
config path with `Environment=MARCELLUS_CONFIG=...` in the unit if it
lives elsewhere.

## Networking

`network_mode: host` is the default because the sidecar needs to reach
Frigate's two origins (unauthenticated `:5000` and authenticated `:8971`) and,
for push, the MQTT broker — usually all LAN addresses. Bridged networking
works too: publish `5001` and make sure `frigate.base_url` /
`frigate.proxy_base_url` / `push.mqtt_host` resolve from inside the container.

## Backup / restore

`fsc backup <dest>` writes the sidecar's own state: the SQLite DB, the
`.session_secret` signing key (losing it just signs every remember-me device
out — regenerated on next start if missing), and the resolved `sidecar.yml`.
The scrub cache and face-model directories are NOT included — both are
regenerable from Frigate's own recordings/DB and are usually far larger.
`<dest>` is a plain directory, or a single file if named `*.tar.gz`.

Systemd stop/restore/start:

```sh
sudo systemctl stop marcellus
sudo -u marcellus fsc backup /opt/marcellus/backups/$(date +%F).tar.gz
# ... or, to restore:
sudo -u marcellus fsc restore /opt/marcellus/backups/2026-08-30.tar.gz --force
sudo systemctl start marcellus
curl -s localhost:5001/healthz
```

`fsc restore` refuses to run at all without `--force` (a restore under a
running service can corrupt the DB's WAL — `--force` is your confirmation
that the unit above is stopped). Restoring a backup taken before a schema
migration is fine: the first open after the restore re-creates any newer
tables and columns (empty) — a warning is logged so you know it happened. A
backup taken before the frigate-sidecar → marcellus rename still has its DB
stored as `frigate-sidecar.db`; restore recognizes that name too.

A cron example for a nightly backup while the service stays up (safe: the DB
copy uses SQLite's own online backup API, not a raw file copy):

```cron
15 3 * * * marcellus fsc backup /opt/marcellus/backups/$(date +\%F).tar.gz
```

Prune old backups yourself (e.g. `find backups/ -mtime +30 -delete` in the
same crontab) — `fsc backup`/`restore` don't manage retention.

## Releases

Tagging `vX.Y.Z` (matching `__version__` in `src/marcellus/__init__.py`)
runs `.github/workflows/release.yml`: multi-arch image to
`ghcr.io/helicopterrun/marcellus` (`latest`, `X.Y`, `vX.Y.Z`) plus an
sdist/wheel attached to the GitHub Release.

### High-res cross-camera face capture (optional)

`marcellus-face-capture.timer` grabs the *capture* camera's full
main-stream frame out of Frigate's recordings whenever a `person` event fires on
a *trigger* camera, for human review at `/faces/captures`.

```sh
sudo install -m 0644 contrib/marcellus-face-capture.service \
                     contrib/marcellus-face-capture.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now marcellus-face-capture.timer
```

Configure under `face_capture:` in `sidecar.yml` — at minimum `enabled`,
`trigger_cameras`, `capture_camera`, and an `output_dir` **inside
/opt/marcellus** (the main unit runs `ProtectSystem=strict` with
`ReadWritePaths=/opt/marcellus`, so anything outside fails EROFS at write
time rather than at config load).

It is a **oneshot behind a timer, not an in-process loop, and deliberately not an
MQTT hook**: `/api/{camera}/recordings/{ts}/snapshot.jpg` 404s until the segment
covering that timestamp has been committed, and segments commit at their *end*
(measured publish lag 5.4-9.4s per camera). `capture_delay_s` (default 45s) is
what makes a 404 genuinely terminal rather than "asked too early".

Checks: `python3 -m marcellus face-capture stats` (counts + last-run
heartbeat), `face-capture scan` (one manual pass), `face-capture prune`.

### Face enrichment (optional)

`face_enrich:` runs **inside the main service** as a lifespan worker (no extra
unit): for each ended `person` event on an enrolled camera it samples full-res
recording frames, embeds the best faces (InsightFace buffalo_l, CPU-only), and
clusters identities at `/enrich/clusters`. Naming a cluster makes later matches
write the event's `sub_label` back to Frigate — disable Frigate's own face
recognition on those cameras first so the sidecar is the only writer.

Setup:

1. `pip install "marcellus[enrich]"` (adds insightface + onnxruntime).
2. Set `face_enrich.enabled: true` and `cameras:` in `sidecar.yml`. The model
   pack (~300 MB) downloads into `model_dir` on the first cycle — keep it
   under `/opt/marcellus` (ProtectSystem=strict) and expect the first
   cycle to be slow; pre-warm with
   `python3 -c "from marcellus.faces.enrich import _engine; _engine('/opt/marcellus/data/models')"`
   as the service user if you want the download done before restart.
3. Restart and watch `/healthz` — a `face_enrich` check appears (ok/starting/
   stale) alongside the scrub one.

## Upgrading from frigate-sidecar

The project (package, CLI, systemd units, and default paths) was renamed from
`frigate-sidecar` to `marcellus`. Existing deployments keep working across the
upgrade without a forced cutover:

- **Package/CLI:** the distribution and import name is now `marcellus`; `fsc`
  still works unchanged, and `marcellus` is now also a valid command name.
- **Env vars:** the prefix changed from `FRIGATE_SIDECAR_` to `MARCELLUS_`.
  Old `FRIGATE_SIDECAR_*` vars are still read (with a one-time deprecation
  warning logged at startup) whenever the corresponding `MARCELLUS_*` var
  isn't set — a `MARCELLUS_*` value always wins if both are set. There's no
  hard deadline to migrate, but do it when convenient.
- **Config file search path:** `/etc/marcellus/sidecar.yml` is now checked
  first; `/etc/frigate-sidecar/sidecar.yml` is still checked as a fallback if
  the new path doesn't exist.
- **systemd:** the units, system user, and install/config directories are now
  `marcellus`/`/opt/marcellus`/`/etc/marcellus` instead of
  `frigate-sidecar`/`/opt/frigate-sidecar`/`/etc/frigate-sidecar`. Re-running
  `install.sh` on an existing bare-metal box creates the new user/paths
  alongside the old ones rather than migrating them automatically — move your
  data (`systemctl stop frigate-sidecar`, copy `/opt/frigate-sidecar` to
  `/opt/marcellus` and `/etc/frigate-sidecar` to `/etc/marcellus`, `chown -R
  marcellus:`, `systemctl enable --now marcellus`, then remove the old unit
  and user) or keep running the old unit indefinitely — nothing forces the
  move.
- **DB filename:** the sidecar DB is now `marcellus.db` (was
  `frigate-sidecar.db`). `fsc restore` accepts a backup made under either
  name.
- **Docker image:** now published as `ghcr.io/helicopterrun/marcellus`
  instead of `ghcr.io/helicopterrun/frigate-sidecar`; the compose service and
  container name are now `marcellus`.
