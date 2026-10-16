---
title: External event sources
section: encounters
order: 5
routes: ["/v1/sightings", "/v1/sources/{source_id}/sightings/{key}", "/v1/sources/{source_id}/sightings/{key}/poster", "/v1/sources/{source_id}/sightings/{key}/stills/{stream}/{index}", "/v1/sources/{source_id}/sightings/{key}/clip/{stream}"]
config: ["external_sources"]
---

Not every camera is a Frigate camera. External sources are non-Frigate
devices whose events Marcellus ingests, journals and serves, so the app has
one place to ask. The first (and so far only) kind is `bernardo`: a Raspberry
Pi wildlife camera ([github.com/helicopterrun/wildlife-cam](https://github.com/helicopterrun/wildlife-cam))
with an IR/NoIR camera (`cam`), a colour camera (`cam2`), clips with audio,
and MegaDetector classification.

Off by default (`enabled`), with its own switch independent of `push.enabled`.

## How it works

- **MQTT is the live trigger.** The device publishes `<base>/availability`,
  `<base>/state`, `<base>/event` and `<base>/alert` on the same broker Frigate
  uses. A message only tells Marcellus *something happened*.
- **HTTP polling is the record and the safety net.** Marcellus fetches the
  device API for the authoritative sighting, and polls every
  `poll_interval_s` so a missed MQTT message costs a minute, not a sighting.
- **The sighting journal** lives in the Marcellus database; rows are pruned
  after `retention_days`.
- **Media is relayed** through Marcellus (poster, stills per stream, clips
  with `Range`), so clients never contact the device and never learn its URL.
- **Separate MQTT client.** This pipeline's client is not push's, so a source
  can never take push down. `mqtt_client_id` must differ from
  `push.mqtt_client_id` (two clients with one id kick each other off the
  broker in a loop; config validation refuses it). Broker settings left unset
  inherit `push.mqtt_*`.

## Setup

1. On the device, open **Settings → MQTT** and point it at the broker
   Frigate uses. A dedicated broker user for the device is recommended, so it
   can be revoked without touching Frigate or Marcellus.
2. Note the device's API origin (e.g. `http://192.168.1.37:8000`) and its MQTT
   base topic (default `bernardo`).
3. Add the source to the config and restart the sidecar:

   ```yaml
   external_sources:
     enabled: true
     sources:
       - id: bernardo
         base_url: "http://192.168.1.37:8000"
   ```

   Marcellus is read-only against the device and never needs its write token.
4. Check `/healthz` for a `source:bernardo` check, and the
   [Encounters](/encounters) page for the **Wildlife camera** section of
   recent sightings.

## Configuration: `external_sources`

- `enabled` -- default `false`; turns the whole subsystem (MQTT client, pollers, journal, routes' data) on or off.
- `mqtt_enabled` -- default `true`; when `false`, sources are polled over HTTP only (slower, no live trigger).
- `mqtt_host` -- broker host; unset inherits `push.mqtt_host`.
- `mqtt_port` -- broker port; unset inherits `push.mqtt_port`.
- `mqtt_username` -- broker user; unset inherits `push.mqtt_username`.
- `mqtt_password` -- broker password; unset inherits `push.mqtt_password`. Env-overridable (`MARCELLUS_EXTERNAL_SOURCES__MQTT_PASSWORD`), never logged.
- `mqtt_client_id` -- default `marcellus-sources`; must differ from `push.mqtt_client_id`.
- `poll_interval_s` -- default `60.0`, minimum 10; seconds between HTTP polls per source.
- `backfill_limit` -- default `200` (0..2000); rows fetched per source on startup to backfill the journal.
- `retention_days` -- default `30`; journal rows older than this are pruned. Upstream media may be gone sooner (Bernardo keeps ~14 days).
- `state_stale_s` -- default `180.0`, minimum 30; with no retained `state` message for this long a source's health is `stale`.
- `sources` -- default empty list; the devices to ingest, configured as below. Ids must be unique.

## Configuration: `external_sources.sources[]`

- `kind` -- default `bernardo`; the adapter to use (the only kind today).
- `id` -- required; lowercase letters, digits and `_`, starting with a letter, max 32. It is the journal key, the `{source_id}` URL segment and the `source:<id>` health name. Never rename once set.
- `name` -- default `Wildlife camera`; the display name shown in the app.
- `enabled` -- default `true`; set `false` to keep the entry but stop ingesting and serving it.
- `base_url` -- required; the device API origin, e.g. `http://192.168.1.37:8000`.
- `mqtt_base_topic` -- default `bernardo`; the device's MQTT base topic (its own Settings → MQTT page).
- `poll_limit` -- default `50` (1..500); rows fetched per poll, newest first.
- `max_connections` -- default `4` (1..16); concurrent upstream connections for polling plus media relay. The Pi is small; keep this low.
- `request_timeout_s` -- default `10.0`; per-request timeout to the device.
- `settle_after_s` -- default `900.0`, minimum 60; a sighting still unclassified this long after it started is settled as-is (Bernardo classifies about 45 s after an event finalises and silently gives up on events that never finish).

## API

- `GET /v1/sightings` -- query `source`, `subject`, `since`, `before`, `limit`; returns `{sightings, next_before}` (pass `next_before` as `before` for the next page).
- `GET /v1/sources/{source_id}/sightings/{key}` -- one sighting.
- Media: `.../poster`, `.../stills/{stream}/{index}` and `.../clip/{stream}` (Range supported) under the same prefix.

Media errors are explicit: `media_gone` (404) means the device already pruned
the file; `upstream_unavailable` (502) means the device could not be reached
right now. Journal rows survive a `media_gone`.

## Health and capabilities

`/healthz` gains a `source:<id>` check per source with state `starting`,
`ok`, `offline`, `unreachable`, `stale`, `alert` or `disabled`. A source
**never** makes `/healthz` fail: a flaky wildlife camera must not look like a
sick sidecar.

`/v1/capabilities` gains `external_sources: {enabled, sources: [{id, kind,
name, enabled, healthy, subjects, media}]}` so the app only shows the
Wildlife camera section when a source is actually there.
