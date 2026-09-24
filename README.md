# Marcellus

A companion sidecar for [Frigate NVR](https://github.com/blakeblackshear/frigate): scrub
cache, push notifications, triage and analysis pages, face pipeline.

It adds:

- **Triage UI** for labeling tracked-object events as true-positive / false-positive
  / skip, with snapshots, video clips, and zone overlays. Labels go into a
  sidecar SQLite DB; Frigate's own DB is opened read-only.
- **Read-only analysis** over Frigate's database (score histograms, motion rate,
  FPS budget vs detector capacity, zone hit counts, annotation offset
  diagnostics, etc.) — things Frigate's own UI doesn't expose. Available both
  as a CLI and as JSON HTTP endpoints.
- **Scrub-cache** (`/v1/scrub`) — a uniform-cadence sprite-sheet cache generated
  from real recording segments, so clients get a predictable, gap-free
  timeline instead of Frigate's motion-driven, top-of-hour-emptied preview
  cache. See [`docs/scrub-cache-and-proxy-spec.md`](docs/scrub-cache-and-proxy-spec.md)
  for the full design.
- **Reverse proxy** to Frigate's authenticated origin, so a client can hold a
  single base URL — everything the sidecar doesn't handle itself streams
  through to Frigate unchanged, with `Range` and auth cookies passed through.
- **Push notifications** (`/v1/push`) — subscribes to Frigate's `frigate/reviews`
  MQTT topic and routes each event through the **attention ladder**: a per
  subject×place outcomes table (`off/log/glance/notify/alarm`, synced from the
  iOS app) plus zone overrides, snooze, quiet hours, and rate caps, decides
  whether an event becomes an APNs card, a Live Activity, or just a logged
  decision. Every decision is queryable at `GET /v1/push/decisions`. Delivery
  goes through a pluggable transport (log-only mock for development, or a
  minimal relay-client transport — see
  [`docs/push-notifications.md`](docs/push-notifications.md)). Off by default.
  A UniFi Protect doorbell also sends its own ring events straight to
  registered devices, bypassing the attention ladder — see the in-app
  "UniFi Protect doorbell" guide topic.

Runs as a single Docker container next to Frigate, bind-mounting Frigate's
`config.yml` and `frigate.db` read-only and writing its own SQLite DB.

## Install

**Requirements:** a running Frigate install whose `config.yml`, database
directory, and recordings tree are readable from the machine the sidecar runs
on (usually the same box), plus network reach to Frigate's API. The scrub
cache additionally needs ffmpeg (bundled in the Docker image) and MQTT access
for push notifications.

### Quick install (recommended)

```sh
curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/main/install.sh | sudo bash
```

Uses Docker (image from `ghcr.io/helicopterrun/marcellus`) when
available, otherwise falls back to a venv + systemd unit. Prompts for the
handful of deployment-specific values (Frigate URLs and paths) and writes
`sidecar.yml` for you. Re-run the same command to upgrade.

### Manual — Docker compose

```sh
sudo mkdir -p /opt/marcellus && cd /opt/marcellus
curl -fsSLO https://raw.githubusercontent.com/helicopterrun/marcellus/main/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/helicopterrun/marcellus/main/.env.example -o .env
$EDITOR .env                                            # point the paths at YOUR Frigate
mkdir -p data config && sudo chown -R 10001:10001 data  # container's non-root uid
docker compose run --rm marcellus init -o /config/sidecar.yml
docker compose up -d
```

Then open `http://<host>:5001`. To build the image locally instead of pulling,
clone the repo and `docker compose build`.

### Manual — systemd (host process)

For environments where Docker isn't an option (e.g. unprivileged LXCs) — see
[`docs/deployment.md`](docs/deployment.md) for details:

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

### Upgrading

- **Docker:** `docker compose pull && docker compose up -d` (or re-run the
  install script).
- **Systemd:** `pip install --upgrade ...` in the venv, then
  `systemctl restart marcellus`.

Upgrading an existing `frigate-sidecar` deployment? See ["Upgrading from
frigate-sidecar"](docs/deployment.md#upgrading-from-frigate-sidecar) in the
deployment docs — env vars, config path, systemd units, and the DB filename
all have backward-compatible fallbacks.

Releases are tagged `vX.Y.Z`; the image is published multi-arch (amd64 +
arm64) with `latest`, `X.Y`, and `vX.Y.Z` tags. `/v1/capabilities` reports the
running version.

### Push notifications and the Elsinore app

`push.transport: relay` with the default `relay_base_url` uses the shared
Elsinore push relay and works out of the box once `push.enabled: true` and the
MQTT settings point at the broker Frigate publishes to. Self-hosting the relay
(your own APNs key/bundle id) is optional — see
[`docs/push-notifications.md`](docs/push-notifications.md).

## Auth

**Every endpoint the sidecar owns requires the client's Frigate session
cookie** — the triage UI, `/faces/captures`, `/analysis`, `/toybox` and `/v1` alike. The
sidecar has no user database and never holds a password: it validates the
cookie against `frigate.proxy_base_url` and caches the pass for
`sidecar.auth_cache_ttl_s`. Since the sidecar serves event history, face crops
of identified people, and writes that reach back into Frigate (labels, Frigate+
submissions, face-library promotion), an open sidecar is a way around Frigate's
own auth.

Three deliberate exceptions:

- `/v1/capabilities`, `/healthz`, `/version` — reachability probes a client
  needs *before* it has a session (plus `/static` and `/login` itself).
- `/v1/push/thumbnail/{handle}` — fetched by the iOS Notification Service
  Extension, which holds no Frigate session; protected instead by the handle
  being opaque, unguessable, and short-lived.
- the reverse-proxy catch-all — Frigate authenticates that traffic itself, and
  its 401 + `WWW-Authenticate` challenge must reach the client intact.

The remember-me cookie (the "stay signed in" box at `/login`) is **not** an
exception — it never admits a request on its own. It is a signed expiry token
minted only for a caller who had just proved a live Frigate session, and its
only effect is to lengthen how long a validated session is cached:
`sidecar.remember_cache_ttl_s` (default 15 minutes) in place of
`sidecar.auth_cache_ttl_s` (default 60 seconds). The Frigate session is still
validated on every cache miss, so **disabling the Frigate account cuts a holder
off within that window**, and an expired Frigate JWT sends them back to
`/login`. The cookie's own lifetime is `sidecar.remember_ttl_s` (default 30
days), hard rather than sliding, with no renewal on use; deleting
`.session_secret` (data dir) and restarting invalidates every outstanding
cookie at once. `HttpOnly`, `SameSite=Lax`; not `Secure`, since plain-HTTP LAN
deployments are the norm.

Set `sidecar.require_frigate_auth: false` only if your Frigate has auth
disabled, in which case there is no session to check.

## API

All new endpoints are under `/v1`. Every `/v1` endpoint is optional: a client
should degrade to talking to bare Frigate directly when `/v1/capabilities`
reports nothing.

Full contract, error vocabulary, and rationale: [`docs/scrub-cache-and-proxy-spec.md`](docs/scrub-cache-and-proxy-spec.md).

| Method | Path | What it does |
|---|---|---|
| `GET` | `/v1/capabilities` | Unauthenticated. Reports whether the scrub cache and proxy are enabled, which cameras have generated data, and the sidecar version. |
| `GET` | `/v1/coverage/{camera}?start=&end=` | Recording coverage read live from `frigate.db` — what Frigate actually recorded in `[start, end)`, plus `latest_segment_end` (diagnostic) and `authoritative_through` (the boundary the client should actually trust). `recording_retention_days` comes from Frigate's own `record` config (the outer bound of the continuous and motion bands); `scrub_retention_days` is the cache's separate, usually shorter horizon — **do not read one as the other**. ETag'd. |
| `GET` | `/v1/scrub/{camera}/coverage?start=&end=` | Scrub-cache coverage — which buckets of sprite data exist, distinct from recording coverage. Compares against `retention_days` so the client can tell "will never be generated" from "still lagging". |
| `GET` | `/v1/scrub/{camera}/sheets?start=&end=` | Index of sprite sheets covering the window: immutable, content-addressed URLs (`{start}-{interval}-{count}`) plus grid geometry. |
| `GET` | `/v1/scrub/{camera}/sheet/{start}-{interval}-{count}.{jpg,webp}` | One sprite-sheet image. Every distinct fill-count is its own immutable object — no cache-freshness reasoning needed anywhere in the path. |
| `GET` | `/v1/motion/{camera}?start=&end=&scale=` | Totalized motion at any `scale`, always covering the full requested range, zero-filled where there's genuinely no data (works around two measured gaps in Frigate's own `/api/.../activity/motion`). |
| `GET` | `/v1/reel/{camera}?start=&end=&motion_scale=` | One call per reel window: coverage + scrub buckets (as `frames[]`) + motion + events, one cache lifetime. ETag'd. |
| `GET` | `/v1/highlights/{camera}?before=&limit=&order=&cluster_s=` | Recent tracked-object events (`reason` is a Frigate label: person/car/package/…) for jump-to-highlight UI. **Raw events, newest first, by default** — see below. |
| `*` | `/{path:path}` (catch-all) | Transparent reverse proxy to Frigate's authenticated origin (`frigate.proxy_base_url`) — `/api/*`, `/vod/*`, `/live/*`, `/preview/*`, everything else. Forwards `Range`/`Authorization`/`Cookie`/`Accept-Encoding`, streams the body **raw** so `content-encoding` and `content-length` stay consistent, relays `content-range`/`etag`/`location`/`www-authenticate` (incl. 401) unchanged, emits each `Set-Cookie` separately, and passes the HTTP method through (not GET-only). Registered last, so `/v1/*`, `/static`, and `/healthz` always win first. |
| `WS` | `/{path:path}` (catch-all) | WebSocket relay to the same origin — Frigate's `/ws` state feed and go2rtc's WebRTC signalling, so live view works through the single base URL. |
| `PUT` | `/v1/push/devices/{apns_token}` | Register (or idempotently re-register) a device for push. Auth'd the same as everything else. |
| `DELETE` | `/v1/push/devices/{apns_token}` | Unregister a device. Idempotent — always 200. |
| `POST` | `/v1/push/devices/{apns_token}/test` | Send a test notification to one device; `404` iff the token has no device row. |
| `GET` | `/v1/push/decisions` | The decision trace: recent events with the level each was decided at and the reasons — the app's "Recent decisions" tuning feed. |
| `GET` / `PUT` | `/v1/push/settings` | The attention-settings document (outcomes table, zone overrides, quiet hours, Live Activity prefs). `outcomes` is authoritative; `routing_table_v2` is kept as a legacy projection. |
| `POST` | `/v1/push/snooze` · `DELETE /v1/push/snooze/{scope}` | Set / clear a snooze scope (camera, subject, or global). |
| `GET` | `/v1/push/sounds` | The custom-sound catalog the app offers. |
| `POST` / `DELETE` | `/v1/push/activity/token` (`/{activity_id}`) | Register / drop a Live Activity push token. |
| `GET` | `/v1/push/thumbnail/{handle}` | Thumbnail for a push handle (Live Activity images). |
| `GET` | `/v1/push/handle/{handle}` | Resolve an opaque, short-lived push handle to `{camera, event_id, snapshot_url}` — called by the iOS Notification Service Extension, never by the relay. |
| `POST` | `/v1/push/feedback` | Per-decision feedback from the app's tuning UI. |

See [`docs/push-notifications.md`](docs/push-notifications.md) for the full
design (event source, payload shape, privacy model, and failure modes).

Unknown paths under endpoints the sidecar owns (not the proxy catch-all) return
JSON 404s, never HTML: `{"error": "<code>", "message": "..."}`.

### Highlights are events, not destinations

`/v1/highlights` returns **raw tracked-object events**, newest first. That is
worth stating plainly because the endpoint's purpose ("take me to the next
interesting thing") implies destinations, and events cluster hard: measured
across three cameras, 40–50% of consecutive highlights are less than 45s apart
(39/99, 46/99, 49/99, with median gaps of 306s / 57s / 48s). One person walking
past emits three or four, so an unclustered "next highlight" control presses the
same person four times.

Two opt-in parameters, both off by default so existing consumers see no change:

- `cluster_s=45` groups events within that many seconds into one destination at
  the run's earliest start, with `events` counting the members, `end` the latest
  end, and `reason`/`score` taken from the most confident member. Gaps are
  measured end-to-start, so a long event followed closely by another counts as
  continuing.
- `order=score` ranks by peak confidence rather than recency. Recency stays the
  default because a client scanning for adjacency depends on time order.

`limit` bounds the **events considered**, not the destinations returned, and its
reach in wall-clock time varies enormously with how busy a camera is: `limit=100`
covered 3.4 hours on one camera and 152 hours on another on the reference
deployment. A client filtering for a sparse label (`package` was 3 in 100) must
page; one call is never enough on its own.

`score` is the event's peak confidence. Current Frigate keeps it in the `data`
JSON blob and leaves the `score`/`top_score` columns NULL, so anything reading
the column alone reports null for every event.

## CLI

The same code is available as a CLI inside the container:

```sh
docker exec marcellus fsc --help
docker exec marcellus fsc triage sample --days 7 --n 30
docker exec marcellus fsc analysis score-histogram --days 7
```

Scrub-cache generation is also driven from the CLI (`fsc scrub ...`), run
continuously in-process by the server (never hourly — see §5.4 of the spec),
which also sweeps retention every `scrub.prune_interval_s`. The CLI stays
useful for backfill/maintenance:

```sh
docker exec marcellus fsc scrub generate            # one generation cycle, all configured cameras
docker exec marcellus fsc scrub generate --camera doorbell
docker exec marcellus fsc scrub backfill --camera doorbell --days 4
docker exec marcellus fsc scrub prune                # drop sheets/buckets past scrub.retention_days
docker exec marcellus fsc scrub coverage --camera doorbell
```

## Configuration

All values are settable in `config/sidecar.yml` or via environment variables.
Env vars use the prefix `MARCELLUS_` and nest with `__`:

```sh
MARCELLUS_FRIGATE__BASE_URL=http://frigate.lan:5000
MARCELLUS_SIDECAR__BIND_PORT=5001
```

(The older `FRIGATE_SIDECAR_` prefix from before the project was renamed
still works as a deprecated fallback.)

Two settings sections back the new features:

- `scrub.*` — off by default (`scrub.enabled: false`). When enabled, generates
  sprite sheets for `scrub.cameras` (empty = all cameras) at
  `scrub.recent_interval_s` (default 1 fps) thinning to `scrub.aged_interval_s`
  after `scrub.aged_after_h`, capped at `scrub.retention_days` (default 4 —
  matches this deployment's measured continuous-recording window, *not*
  Frigate's `record.retain.days`). `scrub.cache_dir` **must** be on a
  different filesystem from `frigate.recordings_path`; the sidecar refuses to
  enable the cache at startup otherwise. `scrub.format` is `jpeg` by default
  (measured smaller than `webp` on real camera content).
- `frigate.proxy_base_url` — Frigate's *authenticated* origin (typically
  `:8971`), used only by the reverse proxy for app traffic. Kept separate from
  `frigate.base_url` (unauthenticated, used for the sidecar's own
  server-to-server calls) — do not point both at the same port.
- `scrub.preserve_source_aspect` (default on) — derive each camera's cell height
  from its own display aspect ratio rather than scaling every source into a
  fixed `cell_w × cell_h`. A 4:3 camera rendered into a 16:9 cell comes out
  anamorphically squeezed and nothing downstream can undo it; two of the ten
  cameras on the reference deployment are 1600×1200. The dimensions travel per
  sheet in the `cell_w`/`cell_h` metadata, so a client reading those renders each
  camera correctly. `cell_h` becomes the fallback for sources whose shape can't
  be measured.
- `scrub.match_keyframe_cadence` (default on) — generate a camera at its own
  keyframe cadence when that is coarser than `recent_interval_s`. A source whose
  GOP is longer than the target interval can only reach that interval by
  decoding every frame, at roughly 5× the cost of keyframe extraction, to
  synthesise stills the encoder never made distinct. On the reference deployment
  the three UniFi Protect cameras (5s GOP, against 1s on the seven Dahua ones)
  were ~70% of the generator's total work while being 30% of the fleet; matching
  their cadence cut cycle time from ~500s to ~150s. The interval travels per
  bucket in `interval`, so a camera generating at 5s is contract-compatible — it
  simply yields a still every 5s while scrubbing. Turn it off to force the
  configured interval everywhere and pay the decode.
- `frigate.recordings_path` — the host-side path that **replaces**
  `frigate.media_path` in `recordings.path`. Rows read
  `<media_path>/recordings/<date>/...`, so the `recordings/` segment comes from
  the DB value and must not be repeated here; pointing it at
  `…/recordings/recordings` maps every segment one level too deep and
  generation produces nothing. Deployment-specific; verify with `docker inspect`
  on the Frigate host per §8.2 of the spec, then confirm against a real
  `recordings.path` row. Startup logs an error if the path doesn't resolve, and
  the generator warns when segment files don't map.
- `sidecar.require_frigate_auth` — see [Auth](#auth) above. On by default.
- `push.*` — off by default (`push.enabled: false`). `push.transport` is `mock`
  (log-only, the default — no real APNs credentials exist yet) or `relay`
  (posts the minimal `{device_token, environment, handle, server_id,
  severity}` payload to `push.relay_base_url`, never a camera name or
  snapshot). `push.mqtt_host`/`mqtt_port` point at the same broker Frigate
  itself publishes `frigate/reviews` to. See
  [`docs/push-notifications.md`](docs/push-notifications.md).

## Status

Actively developed and running in production alongside one Frigate 0.17
deployment (with the Elsinore iOS app as its main client). What ships today:
the push notification pipeline (MQTT → attention ladder → APNs cards and Live
Activities, with replay tooling), camera calibration and zone-handling settings
pages (heading vectors, top-down layout map, world tracks, settings sync
between instances), the scrub sprite-sheet cache and reel API, the triage and
analysis UI, and a themed web interface matching the app's design language.
Still a single-deployment project at heart — portability rough edges remain
(see the deployment docs before pointing it at your own Frigate).

## License

MIT — see [LICENSE](LICENSE).
