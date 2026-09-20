---
title: Push notifications
section: notifications
order: 1
config: ["push"]
---

The sidecar — not Frigate — decides when your phone buzzes. It subscribes to
Frigate's review items over MQTT, evaluates each against your rules, and
sends Apple push notifications through a relay. **{{stat:push_devices}}**
device(s) are currently registered (manage them at
[Settings → Push](/settings#push), which also shows your live ladder table
and example notifications rendered by the real pipeline).

## The attention ladder

Alerts aren't binary. Every detection is scored on a ladder with four rungs:

- **Log** — recorded, no alert.
- **Glance** — a silent notification.
- **Notify** — a notification with sound.
- **Alarm** — critical, breaks through Focus.

The rung comes from a matrix: **who was seen** (unknown person, known
person, animal, vehicle/thing) crossed with the **place class** of the zone
it happened in — a package zone and a secure-area zone earn very different
baseline rungs for the same subject.

Context nudges the rung up or down from there. Nudges *up*: nobody's home,
it's nighttime, the subject is lingering, or they're approaching the secure
area. Nudges *down*: a known face, low detection confidence, or the subject
is leaving. An approaching person can escalate rung by rung, and a situation
that fizzles resolves quietly instead of leaving a stale alarm.

Per-zone overrides set in Zones & routing win over the matrix outright. You
configure the ladder in the Elsinore app (Settings → Alerts) and the app's
choices sync to the sidecar. See your live matrix in
[Settings → Push](/settings#push).

## Recent Decisions and quieting a cell

Every routing decision is logged (`GET /v1/push/decisions`) with a `stage`
(which rule produced the level) and any `modifiers` that nudged or capped it:

| stage/modifier | plain English |
|---|---|
| `muted` | Alerts were paused |
| `system` | Server notice |
| `safety` | Safety exception: always urgent |
| `zone_override` | Your rule for {subject} in {zone} |
| `off_cell` | {subject} in {place} is set to Off |
| `table` | {subject} in {place} is set to {level} |
| `nudge_up` | raised one step (nobody home / night / approaching …) |
| `nudge_down` | lowered one step (known / leaving …) |
| `child_hazard_floor` | at least Notify: child-hazard zone |
| `street_cap` | capped at Quiet: street |
| `unconfirmed_cap` | capped at Quiet: detector unconfirmed |
| `quiet_hours_cap` | capped at Quiet: quiet hours |
| `reclass_dangerous_animal` | treated as a person: dangerous animal |

From a card's detail screen, **Quiet this** (`POST /v1/push/silence`) drops
the cell it routed through — the per-zone override or the outcomes-table
cell — to `quiet`, and the decisions feed marks every affected entry
`silenced`. `PUT /v1/push/overrides` is the fine-grained version for the
settings screen: set or clear one zone-override cell, or set one
outcomes-table cell directly, without needing a card to point at.

`GET /v1/push/status` is the one-glance health check: MQTT/Frigate liveness,
the last review/decision/send timestamps, quiet-hours state, and registered
device count — used by the Status page and the app's own health strip.

## Configuration

The `push:` config section:

- `enabled` and `transport` — `relay` for real APNs delivery via the push
  relay, `mock` for development.
- `relay_base_url` — where the relay lives.
- `mqtt` host/port — the sidecar must see Frigate's MQTT broker, or no
  events arrive at all.
- `card_resolution_s` — an open Live Activity "card" idle this long (default
  10 minutes) is closed silently, covering a resolve that never arrived
  (e.g. a dropped Frigate `end` or a failed write) so it doesn't leak open
  forever.
- `server_id` — short opaque id of this sidecar instance carried in APNs
  payloads, so a device with more than one server registered routes the
  redeem fetch to the right one. Generated at startup if left blank.
- `mqtt_host` / `mqtt_port` / `mqtt_username` / `mqtt_password` /
  `mqtt_client_id` — how the sidecar connects to Frigate's MQTT broker.
- `mqtt_queue_max` — hard cap on the MQTT consumer queue depth (default
  2000).
- `mqtt_topic_reviews` / `mqtt_topic_available` / `mqtt_topic_events` — the
  broker topics subscribed to (reviews is the sole push-worthiness
  authority; events is dwell input only).
- `capture_enabled` / `capture_path` / `capture_max_bytes` — the MQTT
  flight recorder: a rolling JSONL capture of every consumed message, so a
  real situation can be replayed exactly (default on, size-rotated at
  64MiB).
- `reconnect_backoff_s` / `reconnect_backoff_max_s` — MQTT reconnect
  backoff, initial and capped (default 2.0s / 60.0s).
- `offline_silence_s` — how long without broker traffic before Frigate is
  treated as possibly offline and the gap is back-filled on reconnect
  (default 60s).
- `backfill_lookback_s` — how far back to back-fill on reconnect (default
  60s).
- `relay_timeout_s` — per-attempt timeout for a relay-transport send
  (default 5.0s).
- `relay_retry_attempts` — total send attempts for retryable push kinds
  (default 3).
- `relay_breaker_failures` / `relay_breaker_open_s` — consecutive relay
  failures that open the circuit breaker, and how long it stays open before
  a half-open probe (default 3 / 30.0s).
- `handle_ttl_s` — lifetime of a v1 thumbnail-redemption handle (default
  3600s).
- `situation_handle_ttl_s` — lifetime of a situation handle with its
  pre-warmed thumbnail (default 86400s / 24h).
- `rate_limit_window_s` — window used for push rate-limiting (default
  3600s).
- `thumbnail_max_edge` / `thumbnail_quality` / `thumbnail_timeout_s` — size,
  JPEG quality and fetch timeout for a pre-warmed notification thumbnail
  (default 320px / 60 / 5.0s).
- `dwell_source` — which MQTT topic drives a situation's loiter/dwell
  clock, `events` (default) or `reviews`.
- `activity_resolution_s` — quiet period after which a Present situation
  counts as resolved (default 30s).
- `activity_dismissal_tail_s` — how long a Live Activity lingers on screen
  after the end push (default 30s).
- `activity_reap_after_s` — how long an unresolved activity is force-reaped
  (default 300s).
- `activity_sweep_interval_s` — how often the activity-resolution sweeper
  runs (default 5s).
- `delivery_enabled` — turns on the attention-ladder delivery pipeline
  (card state + alert/silent pushes) on top of `enabled` (default on).
- `delivery_zone_place_map` — superseded by the user-editable
  `settings.zone_classes` in the app; no longer read, kept only for
  backward-compatible YAML.
- `delivery_urgent_resound_s` / `delivery_urgent_resound_enabled` /
  `delivery_urgent_resound_max` — an unhandled urgent card may re-alert
  once this long after its last sound (default 120s, on, cap 5).
- `delivery_resound_sweep_interval_s` — how often the urgent re-sound sweep
  runs (default 15s).
- `delivery_backfill_staleness_s` — backfilled events older than this are
  discarded rather than replayed (default 300s).
- `delivery_la_stale_s` — Live Activity stale-date offset from now (default
  900s).
- `relay_key` — auth key sent as the `x-relay-key` header on every relay
  request.
- `external_base_url` — phone-reachable base URL for this sidecar instance,
  used to build card-contract media URLs. Empty (default) omits `media`
  entirely.
- `delivery_la_enabled` — master switch for card Live Activities,
  independent of `delivery_enabled` (default on).
- `delivery_la_families` — superseded by the user-editable
  `settings.live_activities` in the app; no longer read, kept only for
  backward-compatible YAML.
- `push_settings_path` — where the user-editable policy document (routing
  table, zone classes, LA toggles) is persisted as JSON (default
  `config/push_settings.json`).
- `floorplan_path` — where the uploaded floorplan/site image for the
  `/cameras` map is stored (default `config/floorplan`).

Devices register themselves: install Elsinore, complete onboarding, and the
phone appears in the device table with a **Test** button. Pressing it now
sends a real card (`POST /v1/push/devices/{token}/test`, alerts-slice2 §D) —
the notification service extension processes it exactly like a real alert
and posts a delivery receipt back, so the button proves the whole round
trip, not just that APNs accepted the request. It's rate-limited to one per
device per 10 seconds.

The app (and the extension) post delivery receipts to
`POST /v1/push/receipts` right after handing a notification to the OS —
`GET /v1/push/devices/{token}` surfaces the resulting send/receipt stats
(sent, received, median/p90 latency) per device, and `GET /v1/push/status`'s
`relay` block reports the sidecar's own view of relay health (last success,
last error).

## If it goes wrong

Check `/healthz` (`mqtt` component) first — no MQTT means no events at all.
Then confirm `transport` isn't still `mock`. Use [Replay](/replay) to
exercise the whole path end-to-end with canned scenarios; its dry-run mode
shows what the ladder *would* send without notifying.
