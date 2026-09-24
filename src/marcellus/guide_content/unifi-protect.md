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

`GET /v1/push/status`'s `unifi_protect` block reports the websocket's own
connection state, last-ring timestamp, and last error, the same way the
`mqtt_connected`/`frigate_available` fields report the Frigate MQTT
subscriber.
