---
title: Elsinore setup
section: elsinore
order: 2
---

## Onboarding

First launch walks through:

1. **Server** — the sidecar's URL and your Frigate credentials. The app
   talks to Frigate *through* the sidecar, so one address covers everything.
2. **Zones** — pick which zones matter to you (feeds the zone routing
   policy shared with the sidecar).
3. **Notifications** — grant permission and choose a starting alert level;
   the phone registers itself with the sidecar's push system
   (see [Push notifications](/guide/push-notifications)).

## Connection Doctor

Settings → Connection Doctor runs a staged diagnosis of the whole path:
reachability, authentication, API capabilities, stream transport, and push
registration — each with a pass/fail and a plain-language explanation. When
the app misbehaves, run the Doctor before anything else; it usually names
the broken link outright.

## Requirements

- The phone must reach the sidecar's address (same network, VPN, or however
  you've published it).
- Push requires the sidecar's `push` transport configured for the relay.
- Widgets and Live Activities need notification permission granted.
