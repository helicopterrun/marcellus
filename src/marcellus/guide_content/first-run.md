---
title: First run
section: getting-started
order: 4
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
- Run `fsc init`: it finds Frigate's settings and asks you to confirm them (the quick install already did this)
- Or do it by hand: copy config/sidecar.example.yml to config/sidecar.yml
- Set frigate.base_url and frigate.proxy_base_url to your Frigate ports
- Use https:// for port 8971 and decide on frigate.verify_tls (below)
- Point frigate.db_path at Frigate's frigate.db
- Start the service and open the Status page
- Run `fsc doctor` and fix every line marked as failed
- Check /healthz shows every component "ok" or "disabled"
```

Not sure it is right? Run `fsc doctor`: it checks the Frigate addresses, the
database and recordings paths, ffmpeg, the scrub cache, push and the running
service, and prints a one-line fix for anything wrong. See
[Check your install](/guide/check-your-install).

## Let `fsc init` write the file

`fsc init` (the quick install runs it for you) looks for Frigate instead of
asking you to type its settings. It never writes to Frigate's config or files.

1. It asks for Frigate's address, then probes the signed-in port (scheme and
   certificate, see "Frigate's HTTPS sign-in port" below).
2. It asks Frigate itself (`/api/config`) and looks for Frigate's `config.yml`
   in the usual places (`--frigate-config`, `$FRIGATE_CONFIG`, `/opt/frigate`,
   `/srv/frigate`, `/config` and similar). A file counts only if its cameras
   match the running Frigate's, so another project's file is never used.
3. It reads from them how many cameras there are, whether sign-in and TLS are
   on, where Frigate keeps its database, and the `mqtt:` section (broker,
   port, user, whether a password is set, topic prefix). It never reads a
   password: a literal one, a `{FRIGATE_MQTT_PASSWORD}` placeholder and a
   secret file all just mean "a login is needed".
4. It shows a summary, for example *Frigate 0.18.2 at `http://192.168.1.10:5000`,
   9 cameras, sign-in on, MQTT broker at `mqtt.local:1883` as user `frigate`*,
   then asks once whether to use the values it found. Answer no to go through
   them one by one.

What it cannot know it asks for. Frigate's own paths are paths **inside its
container**, so the one value you usually type is the host-side folder that
holds Frigate's `recordings/` (the media folder). It checks that the folder
exists and looks right; if you give the `recordings` folder itself it uses the
folder above it, because `recordings_path` replaces `media_path` (see below).

Then comes one optional question, "Set up push alerts now?" (see
[Set up push](/guide/push-setup)). Say no and nothing about push is written.

Running it again over an existing file needs `--force`. Your current values are
offered as defaults and every setting `init` does not manage is kept (comments
are not). With `--non-interactive` it never prompts: it uses flags, then what it
found, then your existing file, then the defaults, and prints each value it had
to assume.

**The `frigate:` section — how the sidecar reaches Frigate**

- `base_url` — Frigate's **unauthenticated** port (usually `:5000`). Used for
  server-to-server calls only; browsers are never pointed here.
- `proxy_base_url` — Frigate's **authenticated** origin (usually `:8971`).
  The sidecar forwards your browser traffic here and validates your session
  against it. These two URLs must be different, or the login check becomes a
  no-op. Frigate serves this port over **HTTPS** by default, so the URL
  usually starts with `https://` (for example `https://192.168.1.10:8971`);
  see "Frigate's HTTPS sign-in port" below.
- `verify_tls` — default `true`: the sidecar checks the TLS certificate on
  its connections to Frigate (both URLs above, HTTP and WebSocket). Set it to
  `false` when Frigate uses its own self-signed certificate. It never affects the push relay, APNs, UniFi Protect or
  external sources.
- `db_path` — Frigate's SQLite database, opened **read-only**. The sidecar
  never writes to Frigate's database.
- `media_path` / `recordings_path` — where Frigate's recordings live from the
  sidecar's point of view. If scrubbing shows no images, this mapping is the
  most common culprit. `recordings_path` **replaces** `media_path` in the
  `recordings.path` rows, which already read `<media_path>/recordings/<date>/...`,
  so don't repeat the `recordings/` segment: pointing it at
  `…/recordings/recordings` maps every segment one level too deep and
  generation produces nothing. Startup logs an error if the path doesn't
  resolve.
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

### Frigate's HTTPS sign-in port

Frigate serves the signed-in port, 8971, over HTTPS with a certificate it
makes itself (self-signed) unless you turn TLS off in Frigate's config
(`tls: enabled: false`). That leaves three working setups:

- `proxy_base_url: https://<frigate>:8971` with `verify_tls: false`. The
  sidecar then accepts Frigate's self-signed certificate. This is fine on a
  home network; what it gives up is proof that the other end really is your
  Frigate, so another device on the same network could pose as it.
- `proxy_base_url: https://<frigate>:8971` with `verify_tls: true` (the
  default), once Frigate has a certificate this machine trusts (for example
  one from your own certificate authority, or Let's Encrypt behind a real
  name).
- TLS turned off in Frigate (`tls: enabled: false` in Frigate's config) and
  `proxy_base_url: http://<frigate>:8971`.

What does not work is `https://` with the default strict check against a
self-signed certificate (every request fails certificate verification), or
`http://` against a port that speaks HTTPS (Frigate answers every request with
400, "The plain HTTP request was sent to HTTPS port"). The sidecar recognises
both: it logs one WARNING naming the setting to change, `/healthz` adds
`frigate_diagnosis` (`tls_untrusted` or `https_required`) to its checks, and
the login page shows the same explanation instead of a bare "login failed".
`fsc init` probes the port when you give it an address, picks the scheme that
answers, and asks whether to trust a self-signed certificate.

## Authentication

Every endpoint the sidecar owns requires the client's Frigate session cookie:
the triage UI, `/faces/captures`, `/analysis`, `/toybox` and `/v1` alike. The
sidecar has no user database and never holds a password; it validates the
cookie against `frigate.proxy_base_url` and caches the pass for
`sidecar.auth_cache_ttl_s`. Because it serves event history, face crops and
writes that reach back into Frigate (labels, Frigate+ submissions, face-library
promotion), an open sidecar would be a way around Frigate's own auth.
`sidecar.require_frigate_auth: false` turns the check off entirely, whatever
Frigate says; a Frigate with sign-in turned off no longer needs it (next
section).

### Frigate with sign-in turned off

When Frigate has `auth.enabled: false` it serves anyone on its authenticated
port without a session, and the Elsinore app saves such a server as "no
sign-in" and sends no cookie. The sidecar then admits requests that carry no
cookie as well, but only when Frigate itself says so. Both of these must hold:

- Frigate's running config, read from `frigate.base_url`, has `auth.enabled`
  set to exactly `false`.
- A request with no credential to `frigate.proxy_base_url` (`GET
  /api/version`) is answered with a 2xx. With sign-in on, or with
  `proxy.auth_secret` set, or with a reverse proxy in front that asks for its
  own login, Frigate refuses it and so does the sidecar.

The answer is cached for `sidecar.auth_cache_ttl_s` (5 s if Frigate could not
be reached), so turning sign-in back on in Frigate makes the sidecar ask for a
session again within that window. The log says when the sidecar starts or
stops admitting requests without a session.

What an anonymous caller gets is the sidecar's whole surface, the same as any
signed-in Frigate user gets today: the sidecar has no roles of its own, and its
own writes to Frigate (labels, Frigate+ submissions, face-library promotion) go
through `frigate.base_url`. Anything that can reach the sidecar's
`bind_host`/`bind_port` (default `0.0.0.0:5001`, every interface) can use it.
If your Frigate is reachable from fewer places than that, for example only on
a Docker network or localhost, bind the sidecar just as narrowly or put it
behind a firewall rule, because otherwise the sidecar is the more exposed of
the two.

Requests the sidecar forwards to Frigate (its catch-all proxy) are then
Frigate's to answer: on the authenticated port with sign-in off Frigate treats them as
the `viewer` role (`proxy.default_role`), so admin-only Frigate routes answer
403 there.

The reverse-proxy catch-all is gated the same way: a request is forwarded to
Frigate only with the caller's own credential, the `frigate_token` cookie or
an `Authorization: Bearer` token (both are accepted everywhere the sidecar
asks for a session), and the sidecar never adds one of its own. If
`frigate.proxy_base_url` answers requests without a session while Frigate has
sign-in on (usually because it points at the unauthenticated port 5000), the
sidecar cannot check sessions against it, so it refuses every request with a
502 `proxy_origin_unprotected` and logs why; point it at the authenticated
port (8971).

Three deliberate exceptions:

- `/v1/capabilities`, `/healthz`, `/version` (plus `/static` and `/login`
  itself): reachability probes a client needs before it has a session.
- `/v1/push/thumbnail/{handle}`: fetched by the iOS Notification Service
  Extension, which holds no Frigate session; protected instead by the handle
  being opaque, unguessable and short-lived.
- `POST /api/login`: forwarded to Frigate without a credential, since it is
  how a caller gets one; failed attempts are rate-limited per address.

The remember-me cookie (the "stay signed in" box at `/login`) is not an
exception: it never admits a request on its own. It is a signed expiry token
minted only for a caller who had just proved a live Frigate session, and its
only effect is to lengthen how long a validated session is cached
(`remember_cache_ttl_s` instead of `auth_cache_ttl_s`). The Frigate session is
still validated on every cache miss, so disabling the Frigate account cuts a
holder off within that window, and an expired Frigate JWT sends them back to
`/login`. The cookie's own lifetime is `remember_ttl_s`, hard rather than
sliding. Deleting `.session_secret` (in the data dir) and restarting
invalidates every outstanding cookie at once. The cookie is `HttpOnly` and
`SameSite=Lax`, but not `Secure`, since plain-HTTP LAN deployments are the norm.

## The /v1 API and the proxy

Everything new the sidecar adds lives under `/v1`, and every `/v1` endpoint is
optional: a client should fall back to talking to bare Frigate when
`/v1/capabilities` reports nothing. Anything the sidecar doesn't handle itself
is streamed through to Frigate's authenticated origin unchanged (HTTP methods,
`Range`, cookies and WebSockets included), so a client can hold a single base
URL. Unknown paths under endpoints the sidecar owns return JSON 404s
(`{"error": "<code>", "message": "..."}`), never HTML. The full contract and
error vocabulary are in the Reference section's scrub-cache and proxy spec.

## If it goes wrong

Run `fsc doctor` first ([Check your install](/guide/check-your-install)): it
tells you in plain lines what is wrong and what to change. After that,
`/healthz` is the next stop — it reports each subsystem (database, MQTT,
scrub cache, face enrichment) with a one-word status. The
[Troubleshooting](/guide/troubleshooting) chapter maps common symptoms to
their usual causes.
