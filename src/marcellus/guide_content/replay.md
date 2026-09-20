---
title: Replay
section: sidecar
order: 6
routes: ["/replay"]
---

[Replay](/replay) is the push-notification workbench: it replays canned
alert scenarios through the real push pipeline so you can see exactly what
your phone will do — without waiting for an actual person to walk up the
driveway.

## Using it

- Pick a **scenario** (single event, escalating approach, multi-camera
  visit) and fire it.
- **Speed** and **stagger** controls compress a minutes-long approach into
  seconds.
- **Dry-run** evaluates the ladder and shows what *would* be sent without
  actually notifying.

It's designed to be driven from the phone itself: fire a scenario, feel the
notification, adjust the [attention ladder](/guide/push-notifications),
fire it again. Progress and results appear on the page as the scenario runs.

## If it goes wrong

If a scenario fires but nothing reaches the phone, work through the push
checks in [Troubleshooting](/guide/troubleshooting) — MQTT connectivity and
the transport setting cover most cases.
