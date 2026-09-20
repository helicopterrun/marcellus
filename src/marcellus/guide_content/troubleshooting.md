---
title: Troubleshooting
section: operations
order: 2
routes: ["/debug", "/toybox", "/login"]
---

## First stops

- [`/healthz`](/healthz) — one-word status per component. Workers report
  **staleness**, so a wedged loop shows up even while the process lives.
  `mqtt` is connected/disconnected; `scrub` is ok/starting/stale/locked
  (`locked` means another process — a restarting predecessor or a concurrent
  `fsc scrub` invocation — holds the cache lock, distinct from a wedged
  loop); `face_enrich` is ok/starting/stale the same way. `frigate` is
  ok (any HTTP answer, 401 included -- the proxy origin is authenticated)
  /error (5xx)/unreachable/proxy_stalled and is informational except for
  `proxy_stalled`: `watchdog.py` restarts the *Frigate* container directly
  when it hangs, so an ordinary Frigate outage (`error`/`unreachable`) is
  surfaced but doesn't flip the sidecar's own status. `proxy_stalled` means
  the probe -- sent along the media proxy's own path and connection pool,
  with a 2 s budget -- timed out or couldn't get a connection, i.e. the
  sidecar's *own* proxy is wedged and no proxied request can get through.
  That DOES flip /healthz to degraded (body carries `reason:
  proxy_stalled`), since only a sidecar restart fixes it; the probe result
  is cached 10 s. `upstream_pool` and `api_pool` report the two connection
  pools
  (connections/active/idle); saturated (at its max with nothing idle) is
  also degraded, since the next proxied stream would otherwise fail with a
  slow pool timeout instead of a fast 503. If the media pool stays saturated
  across probes for 30 s straight, the sidecar recycles its own stream
  client automatically and logs a warning — a restart is no longer required
  to clear a wedged pool. Any check going bad returns HTTP 503 instead of
  200.
- [Status](/) — the same picture, visually, with sizes and probes.
- [Debug](/debug) — version, the live capability probe (the same payload
  the iOS client reads), and a link to the interactive OpenAPI docs. Check
  here first when a client integration reports a missing/unexpected
  capability, or to confirm which build is actually running.

## Common symptoms

| Symptom | Usual cause |
|---|---|
| Scrub strips empty | `media_path`/`recordings_path` mapping wrong, or camera not enrolled in `scrub.cameras` |
| No push notifications | MQTT unreachable (check `mqtt` in `/healthz`), or transport still `mock` |
| Login loop / 401s | `frigate.proxy_base_url` pointing at the wrong origin |
| Triage pages empty on a dev box | No Frigate database — pages say so rather than erroring |
| Identities never appear | `face_enrich.enabled` off, camera not enrolled, or the `enrich` extra not installed |

## Signing in

When `require_frigate_auth` is on, every page needs a Frigate session. The
[login page](/login) posts your credentials straight through to Frigate —
the sidecar never sees or stores your password — and can mint a
stay-signed-in cookie.

Repeated failed logins from the same IP get a **429** with a `Retry-After`
header once they exceed the configured attempt window — only failed
attempts against the login endpoint itself count (a client's parallel
401 retries against other pages never trip this). Wait out the window
before trying again.

## Backup & restore

`fsc backup <dest>` writes the sidecar DB, session secret, and resolved
config to a directory or `.tar.gz` (scrub cache and face-model directories
are excluded — both regenerate from Frigate's own data). `fsc restore <src>
--force` restores one; stop marcellus first.

## The toybox

[/toybox](/toybox) is a 50-states map quiz with a high-score board. It has
no operational purpose whatsoever. High scores are, however, persistent.
