---
title: Status & live view
section: getting-started
order: 4
routes: ["/", "/cameras", "/live/{camera}"]
config: ["recording_watch"]
---

The [Status page](/) is the landing dashboard — the "is everything fine?"
glance for the whole system.

## Using it

The page refreshes itself every few seconds and shows:

- **Service health** — each sidecar component (database, MQTT subscriber,
  scrub cache worker, face enrichment worker) with staleness detection: a
  worker that stops completing cycles turns red even if the process is alive.
- **Recording** — `ok`, or how many cameras have stopped saving video and
  which (see "Cameras that stop recording" below).
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

## Cameras that stop recording

Frigate can keep *detecting* on a camera while its record stream delivers no
video, and every health number Frigate reports (frame rates, process rates)
still looks fine. So the sidecar watches the one thing that doesn't lie: how
old the newest saved recording segment is for each camera that is supposed to
be recording. Healthy is 5 to 15 seconds.

A camera counts as stalled once its newest segment is older than
`stall_after_s`, or it has no segments at all. It counts as recovered only
when a segment is newer than `recover_within_s`; in between nothing changes,
so a camera hovering near the limit can't send a stream of alerts. Only
cameras that are enabled in Frigate with recording on are watched, and Frigate's
live state is honoured too: a camera whose recording you switched off in
Frigate is not watched, so turning it off on purpose never alerts.

When anything stalls, one notification covers the whole incident, whether one
camera or all of them: "Cameras not recording" (or "gate-face isn't
recording"), naming the cameras and when recording actually stopped. More
cameras stalling, or some recovering, updates that notification quietly in
place. When none are stalled any more it changes to "Recording resumed" and
how long it lasted, and stays in Notification Center. A restart of the sidecar
does not repeat an alert that is already open. The alert is silent during
quiet hours and when sounds are muted. If Frigate itself is down, the
[watchdog](/guide/deployment) and `/healthz` report that; this watcher just
retries.

The same state is on the Status page and in `/healthz` as
`checks.recording` (informational, never degrades the sidecar's own status):
`{"status": "ok"|"stalled", "stalled": [{"camera", "since"}],
"last_check_age_s": …}`.

Only cameras recording continuously are watched: a motion-only camera has long, normal
gaps. A camera you switch off in Frigate is left alone. Times in the alert use the zone of
your registered devices.

| Setting (`recording_watch:` section) | Meaning |
|---|---|
| `enabled` | Run the watcher at all. Default `true`; off removes `checks.recording` too. |
| `notify` | Send the notification (needs push on). Default `true`; false keeps detection, `/healthz` and the Status page. |
| `interval_s` | Seconds between checks. Default `30`. |
| `stall_after_s` | How old the newest segment may get before a camera is stalled. Default `300`. |
| `recover_within_s` | How fresh the newest segment must be before a stalled camera is recovered. Default `60`. |

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
