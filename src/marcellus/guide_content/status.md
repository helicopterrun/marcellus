---
title: Status & live view
section: sidecar
order: 1
routes: ["/", "/cameras", "/live/{camera}"]
---

The [Status page](/) is the landing dashboard — the "is everything fine?"
glance for the whole system.

## Using it

The page refreshes itself every few seconds and shows:

- **Service health** — each sidecar component (database, MQTT subscriber,
  scrub cache worker, face enrichment worker) with staleness detection: a
  worker that stops completing cycles turns red even if the process is alive.
- **Scrub generator** — cycle recency, sheet count, cache size, and the
  worst live-edge lag across cameras.
- **Hardware** — Coral inference speed, detection FPS, Frigate CPU/GPU,
  host load/memory/disk, and per-mount storage bars.
- **Storage** — database and cache sizes.
- **Counters** — sidecar uptime, face enrichment pipeline outcomes
  (excluded sightings counted separately), named vs. unnamed identity
  clusters, face captures awaiting review, pushes sent in the last 24h,
  open Live Activities, the auth cache size, and total triage labels.
- **Recent activity** — the latest Frigate events as thumbnails.

## Cameras

[/cameras](/cameras) is the watch surface: one live snapshot tile per
camera (refreshed every 30s) with a corner badge showing that camera's
scrub live-edge lag. Tap a tile to open its live stream.

On a phone, Status is the first tab in the bottom bar — it works well as the
glance screen when the sidecar is installed to your home screen as a web app.

## Live view

`/live/{camera}` plays the camera's stream directly in the browser using
WebRTC (falling back to MSE where WebRTC can't connect). This is the same
low-latency path the Elsinore app's Live tab uses.

## If it goes wrong

A red component on this page means the matching `/healthz` check failed —
see [Troubleshooting](/guide/troubleshooting) for what each one usually
means.
