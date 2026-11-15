---
title: Welcome
section: getting-started
order: 1
routes: ["/guide"]
---

Elsinore works with [Frigate](https://frigate.video) alone. Marcellus is
optional: it unlocks alerts and Live Activities, Encounters, the scrub reel and
People. This guide covers both:

- **Marcellus** -- the web app you're reading this in. It sits beside your
  Frigate NVR and adds review tooling, analytics, smooth timeline scrubbing,
  face recognition, and push notifications.
- **Elsinore** -- the native iOS companion app: live grid, review feed,
  widgets, and (with Marcellus) the scrub reel and Live Activities on your phone.

Right now this sidecar has been up **{{stat:uptime}}** and Frigate has logged
**{{stat:events_today}}** events in the last 24 hours.

## Where things live

| Area | Pages |
|---|---|
| Watch | [Status](/) with live camera views, the [camera list](/cameras), and a single-camera view at `/live/<camera>` |
| Review | [Triage](/triage) |
| Crossings | [Encounters](/encounters) -- one row per visit across cameras -- and the per-atom [Observations](/guide/observations) API |
| Space | [Map](/map) with camera placement and calibration |
| People | [Face captures](/faces/captures), [Identities](/enrich/clusters) |
| Tuning | [Scores](/score-histogram), [Motion](/motion), [FPS budget](/fps-budget), [Zone hits](/zone-hits), [Placement](/placement) |
| Phone | [Settings](/settings) for push devices and zone routing, [Replay](/replay) for testing alerts |
| Operations | [Debug](/debug) for version and capabilities, [`/healthz`](/healthz) for component status |

## How to use this guide

- The **sidebar** (desktop) lists every chapter; the **search box** searches
  the full text of the guide, not just titles.
- Every topic has an **Open** link in its breadcrumb that jumps straight to
  the live page it describes.
- Topics marked *walkthrough* include a tickable checklist -- your progress
  is saved on this device.

Start with [First run](/guide/first-run) if you're setting up a new install,
or jump to the topic for the page you're curious about.
