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

## Encounter-aware notifications

Design specs: the repo's `docs/push-notifications.md` for the push
pipeline, `docs/encounters.md` for the crossings it threads on.

When one person walks past three cameras, that is one thing happening — not
three. The sidecar's encounters feature already groups those reviews into a
single crossing, and notifications use it.

By default (`encounter_threading`) every notification from one crossing
lands in the **same Notification Center group**, whichever camera saw it, and
the notification carries the encounter so tapping through opens the
Observations screen on that crossing rather than on one camera's clip.

Turn on `encounter_merge` and it goes further: instead of one notification
per camera, you get **one notification that updates as the subject moves**.
The title stays put; the body becomes the path — "Alley Wide → Stairway
Wide → Gate Walkway" — and there is no second buzz for the same walk. It is
off by default so you can watch the logs first; with it off, the sidecar
simply records which notifications it *would* have merged.

| Field | Default | Effect |
|---|---|---|
| `encounter_threading` | `true` | Every notification from one crossing lands in the same Notification Center group, whichever camera saw it, and carries the encounter id so a tap opens the crossing. Presentation only. |
| `encounter_merge` | `false` | One notification that updates as the subject moves, instead of one per camera. With it off the sidecar only records what it would have merged. |
| `encounter_link_timeout_s` | `0.25` | How long, in seconds, a notification waits for its encounter link before sending unthreaded rather than delaying the buzz. |

All three are live-tunable from [Settings](/settings).

Two safeguards: a person and a vehicle in the same crossing never merge onto
one notification (they are different subjects), and a crossing whose
notification has already resolved never reopens — a new sighting starts a
fresh notification, still grouped with the old one.

`encounter_link_timeout_s` is how long a notification will wait for the
crossing to be worked out (a quarter second). Past that the notification
goes out ungrouped rather than late.

## Configuration

The `push:` config section:

- `enabled` (default `false`) and `transport` — push is off until you flip
  `enabled`; `transport` picks `relay` for real APNs delivery via the push
  relay, `mock` for development.
- `relay_base_url` — where the relay lives.
- `mqtt_host` — hostname or IP of Frigate's MQTT broker; the sidecar must be
  able to reach it or no events arrive at all.
- `mqtt_queue_max` — hard cap on the MQTT consumer queue depth (default
  2000).
- `mqtt_topic_reviews` — the broker topic the sidecar subscribes to for
  Frigate review items; this is the sole authority on whether anything is
  push-worthy.
- `mqtt_topic_available` — the broker topic carrying Frigate's own
  online/offline availability payload.
- `capture_path` — file path the MQTT flight recorder writes its rolling
  JSONL capture to; empty (default) uses `mqtt-capture.jsonl` next to
  `push_settings_path`.
- `backfill_lookback_s` (default 60s) — how far back, in seconds, to
  back-fill on reconnect after an offline gap.
- `card_resolution_s` — an open Live Activity "card" idle this long (default
  10 minutes) is closed silently, covering a resolve that never arrived
  (e.g. a dropped Frigate `end` or a failed write) so it doesn't leak open
  forever.
- `server_id` — short opaque id of this sidecar instance carried in APNs
  payloads, so a device with more than one server registered routes the
  redeem fetch to the right one. Generated at startup if left blank.
- `handle_ttl_s` — lifetime of a v1 thumbnail-redemption handle (default
  3600s).
- `rate_limit_window_s` — window used for push rate-limiting (default
  3600s).
- `dwell_source` — which MQTT topic drives a situation's loiter/dwell
  clock, `events` (default) or `reviews`.
- `delivery_zone_place_map` — superseded by the user-editable
  `settings.zone_classes` in the app; no longer read, kept only for
  backward-compatible YAML.
- `delivery_la_stale_s` — Live Activity stale-date offset from now (default
  900s).
- `relay_key` — auth key sent as the `x-relay-key` header on every relay
  request.
- `external_base_url` — phone-reachable base URL for this sidecar instance,
  used to build card-contract media URLs. Empty (default) omits `media`
  entirely.
- `delivery_la_families` — superseded by the user-editable
  `settings.live_activities` in the app; no longer read, kept only for
  backward-compatible YAML.
- `push_settings_path` — where the user-editable policy document (routing
  table, zone classes, LA toggles) is persisted as JSON (default
  `config/push_settings.json`).
- `floorplan_path` — where the uploaded floorplan/site image for the
  `/cameras` map is stored (default `config/floorplan`).

### MQTT connection

| Field | Default | Effect |
|---|---|---|
| `mqtt_client_id` | `marcellus-push` | Client id the sidecar identifies itself with to the Frigate MQTT broker. |
| `mqtt_port` | `1883` | TCP port of the Frigate MQTT broker. |
| `mqtt_username` | none | Broker username, if the broker requires auth; unset connects anonymously. |
| `mqtt_password` | none | Broker password paired with `mqtt_username`. |
| `mqtt_topic_events` | `frigate/events` | Topic subscribed for dwell/loiter timing only — `frigate/reviews` stays the sole authority on whether anything is push-worthy. |
| `reconnect_backoff_s` | `2.0` | Initial delay, in seconds, before retrying a dropped MQTT connection. |
| `reconnect_backoff_max_s` | `60.0` | Cap, in seconds, the reconnect backoff grows to. |
| `offline_silence_s` | `60.0` | How long without any broker traffic before Frigate is treated as possibly offline and the gap is back-filled on reconnect. |

### MQTT flight recorder

| Field | Default | Effect |
|---|---|---|
| `capture_enabled` | `true` | Turns on the rolling JSONL capture of every consumed reviews/events MQTT message, so a real situation can be replayed exactly via `tools/replay_capture.py`. |
| `capture_max_bytes` | `67108864` | Size, in bytes, the capture file is rotated at (one `.1` sibling kept); default 64MiB. |

### Relay transport

| Field | Default | Effect |
|---|---|---|
| `relay_base_url` | the shared `elsinore-push-relay` worker | Base URL the sidecar posts content-free templated alerts to. Override only when running your own relay fork under your own bundle id/team. |
| `relay_timeout_s` | `5.0` | Per-attempt timeout, in seconds, for a relay-transport send. |
| `relay_retry_attempts` | `3` | Total send attempts for retryable push kinds (push, Live Activity start/end); `1` disables retry. Live Activity update/situation/test always send once regardless. |
| `relay_breaker_failures` | `3` | Consecutive transport failures (exception or 5xx; 429/4xx never count) that open the circuit breaker. |
| `relay_breaker_open_s` | `30.0` | How long, in seconds, the breaker stays open before a single half-open probe attempt. |

### Delivery and resound

| Field | Default | Effect |
|---|---|---|
| `delivery_enabled` | `true` | Turns on the attention-ladder delivery pipeline (card state plus alert/silent pushes) on top of `enabled`. |
| `delivery_backfill_staleness_s` | `300.0` | Backfilled events older than this, in seconds, are discarded rather than replayed. |
| `delivery_urgent_resound_enabled` | `true` | Lets an unhandled urgent card re-alert once after `delivery_urgent_resound_s`. |
| `delivery_urgent_resound_s` | `120.0` | How long, in seconds, an unhandled urgent card waits since its last sound before it may re-alert. |
| `delivery_urgent_resound_max` | `5` | Cap on the number of times a single card may re-sound. |
| `delivery_resound_sweep_interval_s` | `15.0` | How often, in seconds, the urgent re-sound sweep runs. |

### Live Activity lifecycle

| Field | Default | Effect |
|---|---|---|
| `delivery_la_enabled` | `true` | Master switch for card Live Activities, independent of `delivery_enabled`. |
| `activity_resolution_s` | `30.0` | Quiet period, in seconds, after which a Present situation counts as resolved even without Frigate's own `end`. |
| `activity_dismissal_tail_s` | `30.0` | How long, in seconds, a Live Activity lingers on screen after the end push. |
| `activity_reap_after_s` | `300.0` | How long, in seconds, an unresolved activity is force-reaped rather than left open forever. |
| `activity_sweep_interval_s` | `5.0` | How often, in seconds, the activity-resolution sweeper runs. |
| `situation_handle_ttl_s` | `86400.0` | Lifetime, in seconds, of a situation handle with its pre-warmed thumbnail (24h). |

### Notification thumbnail capture

| Field | Default | Effect |
|---|---|---|
| `thumbnail_max_edge` | `320` | Long-edge pixel size a pre-warmed notification thumbnail is resized to; the notification service extension runs under a tight memory ceiling, so bigger buys nothing a notification can show. |
| `thumbnail_quality` | `60` | JPEG quality used when re-encoding the pre-warmed thumbnail. |
| `thumbnail_timeout_s` | `5.0` | Timeout, in seconds, for fetching the source snapshot to build the thumbnail from. |

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
