---
title: First run
section: getting-started
order: 2
config: ["frigate", "sidecar", "log_level"]
---

The sidecar reads one YAML file, `config/sidecar.yml` (copy it from
`config/sidecar.example.yml`). Every value can also be set with environment
variables using the `MARCELLUS_` prefix and `__` for nesting
(`MARCELLUS_FRIGATE__BASE_URL=…`). The older `FRIGATE_SIDECAR_` prefix from
before the project was renamed still works as a fallback, with a deprecation
warning logged at startup (see `docs/deployment.md`).

## Setup checklist

```walkthrough
- Copy config/sidecar.example.yml to config/sidecar.yml
- Set frigate.base_url and frigate.proxy_base_url to your Frigate ports
- Point frigate.db_path at Frigate's frigate.db
- Start the service and open the Status page
- Check /healthz shows every component "ok" or "disabled"
```

## Configuration

**The `frigate:` section — how the sidecar reaches Frigate**

- `base_url` — Frigate's **unauthenticated** port (usually `:5000`). Used for
  server-to-server calls only; browsers are never pointed here.
- `proxy_base_url` — Frigate's **authenticated** origin (usually `:8971`).
  The sidecar forwards your browser traffic here and validates your session
  against it. These two URLs must be different, or the login check becomes a
  no-op.
- `db_path` — Frigate's SQLite database, opened **read-only**. The sidecar
  never writes to Frigate's database.
- `media_path` / `recordings_path` — where Frigate's recordings live from the
  sidecar's point of view. If scrubbing shows no images, this mapping is the
  most common culprit.
- `config_path` — Frigate's `config.yml`, read for camera/zone data (default
  `/opt/frigate/config.yml`).
- `config_refresh_enabled` — lets `POST /v1/push/frigate-config/refresh`
  overwrite `config_path` in place. Off by default — only turn this on when
  `config_path` is a sidecar-owned snapshot, never Frigate's live config.

**The `sidecar:` section — the sidecar itself**

- `db_path` — the sidecar's own SQLite file (labels, scrub cache index,
  push devices, faces). Created automatically.
- `bind_host` / `bind_port` — where the web app listens (default
  `0.0.0.0:5001`).
- `require_frigate_auth` — when on, every sidecar page requires signing in
  with your Frigate account.
- `auth_cache_ttl_s` — how long a validated Frigate session cookie is
  trusted before being re-checked upstream (default 60s).
- `auth_cache_max_entries` — hard cap on remembered sessions in the auth
  cache (default 1024).
- `remember_ttl_s` — lifetime of the sidecar's own "stay signed in" cookie
  minted by `POST /login/remember` (default 30 days).
- `remember_cache_ttl_s` — the (longer) auth-cache window granted to a
  request carrying a valid remember-me cookie (default 900s).
- `login_rate_limit_attempts` / `login_rate_limit_window_s` — caps failed
  login attempts per client IP (default 10 per 60s); only a failed POST to
  `/api/login` counts (a stale session cookie elsewhere never does — a
  client with an expired cookie can fire several requests in parallel, and
  guessing a valid one is infeasible anyway). Once the limit is hit the
  sidecar answers 429 with a `Retry-After` header until the window clears.
  Set `login_rate_limit_attempts` to `0` to disable.
- `tuning_path` — JSON file of runtime overrides written by the `/settings`
  Tuning panel (default `config/tuning.json`, next to `push.push_settings_path`).
  Read on every process start (web app, CLI, watchdog, face-capture timer) so
  a tuned value applies everywhere, not just to the running server.

`log_level` (top level) defaults to `INFO`.

## If it goes wrong

`/healthz` is the first stop — it reports each subsystem (database, MQTT,
scrub cache, face enrichment) with a one-word status. The
[Troubleshooting](/guide/troubleshooting) chapter maps common symptoms to
their usual causes.
