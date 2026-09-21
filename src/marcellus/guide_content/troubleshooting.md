---
title: Troubleshooting
section: operations
order: 2
routes: ["/debug", "/toybox", "/login"]
---

## First stops

- [`/healthz`](/healthz) -- one line per component, and the status table
  below. Workers report **staleness**, so a wedged loop shows up even while
  the process lives. Any check going bad returns HTTP 503 instead of 200,
  so `curl -f` and the Compose healthcheck both notice.
- [Status](/) -- the same picture, visually, with sizes and probes.
- [Debug](/debug) -- version, the live capability probe (the same payload
  the iOS client reads), and a link to the interactive OpenAPI docs. Check
  here first when a client integration reports a missing/unexpected
  capability, or to confirm which build is actually running.

## /healthz statuses

A check only appears when its feature is enabled. Red states below are the
ones that flip the whole response to `degraded` + HTTP 503.

| Check | Status | Meaning | What to do |
|---|---|---|---|
| `frigate` | `ok` | Any HTTP answer through the proxy's own path and pool, 401 included -- the proxy origin is authenticated. | Nothing. |
| `frigate` | `error` | Frigate answered 5xx. Informational only: `watchdog.py` restarts the *Frigate* container, so a Frigate outage must not restart the sidecar. | Check Frigate itself and the watchdog. |
| `frigate` | `unreachable` | Connect or read failure to Frigate. Informational, same reason. | Check Frigate and the network path. |
| `frigate` | `proxy_stalled` | **Degraded.** The 2s probe along the media proxy's own path/pool timed out or got no connection: the sidecar's own proxy is wedged and no proxied request can get through. The body carries `reason: proxy_stalled`; the probe result is cached 10s. | Restart the sidecar. |
| `upstream_pool` / `api_pool` | stats | Connections/active/idle for the media and API pools. | Nothing. |
| `upstream_pool` / `api_pool` | saturated | **Degraded.** At max connections with nothing idle; the next proxied stream would fail on a slow pool timeout rather than a fast 503. | If the media pool stays saturated for 30s straight the sidecar recycles its own stream client and logs a warning -- no restart needed. Otherwise look for a viewer storm. |
| `mqtt` | `connected` | The push subscriber is on the broker. | Nothing. |
| `mqtt` | `disconnected` | **Degraded.** Push is not being delivered, whether this is startup, backoff, or a dead broker. | Check the broker and `push.mqtt` settings; the reconnect loop clears it fast on its own. |
| `db` | `ok` | The sidecar DB opened and answered `SELECT 1`. | Nothing. |
| `db` | `error` | **Degraded.** The sidecar DB could not be opened or queried. | Check `sidecar.db_path` and its filesystem. |
| `scrub` | `starting` | No cycle has completed yet, still inside the startup grace. | Wait. |
| `scrub` | `ok` | Last cycle is recent (`scrub_last_cycle_age_s` is also reported). | Nothing. |
| `scrub` | `stale` | **Degraded.** No completed cycle for ten ticks -- the loop is stuck, not merely slow. | Check the logs for ffmpeg errors and free space; restart the service. |
| `scrub` | `locked` | **Degraded.** Another process holds the cache lock -- a restarting predecessor, or a concurrent `fsc scrub` run -- so the generation loop never started. | Find the stray process rather than assuming a wedge. |
| `face_enrich` | `starting` / `ok` / `stale` | Same staleness shape as `scrub` (`face_enrich_last_cycle_age_s` is reported); a wedged model load and a dead task both read as `stale`. | For `stale`, check the enrich worker's logs and that the `enrich` extra is installed. |
| `encounters` | `disabled` | `encounters.enabled` is off. Not an error. | Turn it on if you want [Encounters](/guide/encounters). |
| `encounters` | `starting` | Enabled, but the service object isn't up yet. | Wait. |
| `encounters` | `ok` | Reconciling; `encounters_last_reconcile` carries the last cycle's counts and age. | Nothing. |
| `encounters` | `error` | **Degraded.** The last reconcile raised; the message is in the body. | Read the error, check the Frigate DB is readable, then restart. |

The body also carries `scrub_low_disk` when scrub is enabled -- free space
on the cache filesystem is under `scrub.min_free_bytes`, so the generator
is skipping cycles until pruning frees space back up.

## Logs and service control

Bare metal (systemd; the unit `install.sh` writes and `contrib/` ships is
`marcellus.service`):

```
sudo systemctl status marcellus
sudo systemctl restart marcellus
journalctl -u marcellus -f
```

Docker Compose (the service is `marcellus`):

```
docker compose ps
docker compose restart marcellus
docker compose logs -f marcellus
```

There is no log file: the sidecar logs to stdout/stderr at `log_level`
(default `INFO`, live-tunable), so journald or the container runtime owns
the log. Turn `log_level` to `DEBUG` from [Settings](/settings) to get more
without a restart.

Two extra units ship in `contrib/`: `marcellus-healthcheck.timer`, which
polls `/healthz` and restarts the unit after three consecutive failures,
and `marcellus-face-capture.timer`, which runs face capture in its own
process. Both are followed the same way, e.g.
`journalctl -u marcellus-healthcheck`.

## Retention and disk

Four independent retention knobs, each pruning its own store:

| Knob | Default | Prunes | Run by hand |
|---|---|---|---|
| `scrub.retention_days` | `4` | Scrub sprite sheets and buckets, oldest first. Capped by how long continuous recording actually lasts. | `fsc scrub prune` |
| `face_capture.retention_days` | `30` | Stored full-res face captures, matched to Frigate's own alert retention. | `fsc face-capture prune` |
| `encounters.retention_days` | `30` | Sealed encounters plus their members and decisions; unsealed encounters never expire. | `fsc encounters prune` |
| `face_enrich.cluster_ttl_days` | `60` | Unnamed clusters and their embeddings. Named clusters never expire. | (worker only) |

`scrub.min_free_bytes` (default 2 GiB) is the floor below which the scrub
generator skips a cycle rather than grinding out the same ENOSPC failure
forever; pruning keeps running on its own cadence and is what frees the
space.

## Common symptoms

| Symptom | Usual cause |
|---|---|
| Scrub strips empty | `media_path`/`recordings_path` mapping wrong, or camera not enrolled in `scrub.cameras` |
| No push notifications | MQTT unreachable (check `mqtt` in `/healthz`), or transport still `mock` |
| Login loop / 401s | `frigate.proxy_base_url` pointing at the wrong origin |
| Triage pages empty on a dev box | No Frigate database -- pages say so rather than erroring |
| Identities never appear | `face_enrich.enabled` off, camera not enrolled, or the `enrich` extra not installed |

## Signing in

When `require_frigate_auth` is on, every page needs a Frigate session. The
[login page](/login) posts your credentials straight through to Frigate --
the sidecar never sees or stores your password -- and can mint a
stay-signed-in cookie.

Repeated failed logins from the same IP get a **429** with a `Retry-After`
header once they exceed the configured attempt window -- only failed
attempts against the login endpoint itself count (a client's parallel
401 retries against other pages never trip this). Wait out the window
before trying again.

## Backup & restore

See [Deployment & upgrades](/guide/deployment) "Backup / restore" -- `fsc
backup <dest>` and `fsc restore <src> --force`, with the service stopped.

## The toybox

[/toybox](/toybox) is a 50-states map quiz with a high-score board. It has
no operational purpose whatsoever. High scores are, however, persistent.
