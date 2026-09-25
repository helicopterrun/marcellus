---
title: UniFi Protect doorbell
section: notifications
order: 5
config: ["unifi_protect"]
---

A UniFi Protect doorbell ring goes straight to your phone -- bypassing the
attention ladder entirely. This is on purpose: a ring is a person at the
door, not a detection to route through Log/Glance/Notify/Alarm.

## Setup

1. In UniFi OS (the console's web UI, not Protect itself), go to **Settings
   → Control Plane → Integrations → Create API Key**. Copy the key.
2. Set `unifi_protect.enabled: true`, `unifi_protect.console_url` (the
   console's base URL, e.g. `https://192.168.1.1`), and `unifi_protect.api_key`
   (or the `MARCELLUS_UNIFI_PROTECT__API_KEY` environment variable -- never
   commit the key to the config file).
3. Restart the sidecar. On startup it validates the key against the
   console and logs every Protect camera's id and name
   (`unifi_protect: camera id=... name=...`) -- check `/healthz` logs for
   these lines.
4. Fill in `unifi_protect.cameras`, mapping each Protect camera id from
   step 3 to the matching Frigate camera name:

   ```yaml
   unifi_protect:
     enabled: true
     console_url: "https://192.168.1.1"
     cameras:
       "6183a1b200f1234500001234": "front_door"
   ```

   A ring from a camera id not in this map is logged and dropped -- there is
   no Frigate camera to attribute the snapshot/send to.

## What you get

A ring sends **"Someone's at the door"** with the camera's display name and
local time, a snapshot from that Frigate camera, and `sound: "default"` at
`time-sensitive` priority -- to every registered device with doorbell rings
enabled (on by default; toggle per-device in Settings → Push).

## Mute behavior

Ring pushes bypass `push_snoozes`' per-camera scope on purpose: muting
`camera:front_door` (e.g. to quiet routine passersby detections) does
**not** silence a doorbell ring on that same camera -- someone actually
pressing the button should always get through. A **global** snooze does
still mute rings, same as everything else.

## Configuration

- `enabled` -- default `false`; turns the whole feature (websocket
  subscriber and ring sends) on or off.
- `console_url` -- the UniFi OS console's base URL.
- `api_key` -- the Integration API key from step 1; env-overridable
  (`MARCELLUS_UNIFI_PROTECT__API_KEY`), never logged.
- `verify_tls` (default `false`) -- most UniFi OS consoles present a
  self-signed cert.
- `cameras` -- Protect camera id → Frigate camera name map.
- `ring_dedup_seconds` (default `20`) -- a second ring for the same camera
  within this window is dropped rather than re-sent (the Protect API has
  been observed to occasionally deliver a duplicate event for one press).
- `device_poll_seconds` (default `60`, minimum `15`) -- how often the
  sidecar polls Protect for camera health (online state, LCD support)
  independent of the ring websocket. Surfaced at `GET /v1/protect/status`
  and in `/healthz`'s `unifi_protect` check.

## Doorbell LCD replies (M-2)

For a Protect doorbell with an LCD screen, a ring's push carries up to three
quick-reply slots plus a custom-text option; `POST /v1/doorbell/{camera}/lcd`
sends the reply to the console, `GET /v1/doorbell/{camera}/lcd/options` lists
what's available, and `GET /v1/doorbell/{camera}/snapshot` proxies the
doorbell's own onboard snapshot.

- `lcd_presets` -- a map of preset id to `{type, text, duration_s, title}`,
  the quick replies offered on a ring. Defaults to three presets:
  `leave_package` (LEAVE_PACKAGE_AT_DOOR), `be_right_there` (CUSTOM_MESSAGE,
  "BE RIGHT THERE"), `do_not_disturb` (DO_NOT_DISTURB). A `CUSTOM_MESSAGE`
  preset must set `text`; `duration_s` (1..86400) is how long the message
  stays on the LCD before the console clears it.
- `custom_reply_duration_s` -- default `120` seconds a device's own free-text
  LCD reply stays lit before clearing.
- `custom_reply_max_chars` -- default `30` (1..64), the longest normalized
  custom-text reply accepted.
- `image_duration_s` -- default `300` seconds an animation/image LCD reply
  stays lit.
- `ring_snapshot` -- default `"protect"`; `"protect"` uses the doorbell's own
  onboard snapshot for a ring's `media`, falling back to the Frigate
  `latest.jpg` on any console failure. `"frigate"` uses the Frigate snapshot
  outright, unchanged from before M-2.

`POST /v1/doorbell/{camera}/lcd` response status codes: `400` for a bad
request -- neither/both of `option_id`/`custom_text` present, an unknown
`option_id`, or `custom_text` that's empty after normalization, over
`custom_reply_max_chars`, or outside the allowed character set; `404` if
`camera` isn't mapped to a Protect camera; `409` if the mapped camera has no
LCD; `502` if the console accepted the request but returned an error;
`503` if `unifi_protect` isn't enabled.

## Health & capabilities

`GET /healthz`'s `checks.unifi_protect` reports `ok` / `degraded` / `down`:
`down` means the ring websocket has been disconnected for over two minutes
(and makes the top-level status `degraded`); `degraded` means it's
connected but a mapped camera isn't reporting `CONNECTED`, or the device
poll hasn't succeeded yet. `GET /v1/capabilities`'s `unifi_protect` block
reports whether the feature is on, the mapped Frigate camera names, and
whether any mapped camera has an LCD (`lcd_message`) -- both `false` until
enabled and the first device poll completes. `GET /v1/protect/status`
(authenticated) returns the full detail: console version, per-camera state,
and each camera's last-ring timestamp.

`GET /v1/push/status`'s `unifi_protect` block reports the websocket's own
connection state, last-ring timestamp, and last error, the same way the
`mqtt_connected`/`frigate_available` fields report the Frigate MQTT
subscriber.

## Troubleshooting

There is no per-ring INFO log line -- a successful ring is silent in the
logs by design, so absence of a log entry does not mean a ring was missed.
Check these instead, in order:

1. **Push Doctor** (Settings → Push → Push Doctor, or `GET
   /v1/push/status`) -- the `unifi_protect` block shows whether the
   websocket is currently connected, the last ring timestamp it saw, and
   the last connection error if any.
2. **Sent-push history** -- query the sidecar's own SQLite DB for rows
   with `mutation = 'ring'`: `sqlite3 marcellus.db "SELECT * FROM
   push_card_sends WHERE mutation='ring' ORDER BY rowid DESC LIMIT 10;"`.
   The `card_key` column reads `doorbell:<camera>`; a missing row for a
   ring you know happened means it never reached the sidecar at all.
3. **journalctl** -- `journalctl -u marcellus -g
   marcellus.push.unifi_protect` surfaces the websocket's connect,
   reconnect, and error lines (backoff attempts, auth failures, TLS
   errors) even though ring delivery itself logs nothing.

**Known limitation:** UniFi Protect itself occasionally fails to emit a
ring event over the Integration API even though the doorbell was pressed
(a known upstream gap, not specific to this sidecar). There is also no
backfill -- a ring that arrives while the websocket is reconnecting is
simply missed, with no server-side history to replay it from afterward.
If rings go missing in a pattern (not just the occasional drop), check
step 3 above for reconnect churn before assuming a dropped upstream event.
