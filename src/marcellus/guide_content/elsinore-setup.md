---
title: Elsinore setup
section: elsinore
order: 1
---

## Onboarding

First launch walks through:

1. **Server** — the address of your Frigate (or of Marcellus) and your Frigate
   credentials. Elsinore works with Frigate alone. Marcellus is optional: when
   the app is pointed at it, one address covers everything, because the app
   talks to Frigate *through* Marcellus.
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

- The phone must reach the address you gave it (same network, VPN, or however
  you've published it).
<<<<<<< HEAD
- Alerts, Live Activities, Encounters, the scrub reel and People need
  Marcellus. Elsinore works with Frigate alone without them.
- Push requires the sidecar's `push` transport configured for the relay, which in turn needs a relay key (see [Push notifications](/guide/push-notifications) and [What leaves your network](/guide/privacy)).
=======
- Push needs `push.enabled: true`. The default transport (`auto`) starts using the relay once the app has registered with a relay credential; a relay key is only needed for a self-run relay (see [Push notifications](/guide/push-notifications) and [What leaves your network](/guide/privacy)).
>>>>>>> 05ef6ff (Default push transport auto, -full Docker image with face recognition, relay credential guide)
- Widgets and Live Activities need notification permission granted.
