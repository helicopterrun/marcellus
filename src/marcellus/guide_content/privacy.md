---
title: What leaves your network
section: getting-started
order: 5
config: []
---

Marcellus runs on your own hardware and keeps its data there. This page lists
exactly what it sends elsewhere, taken from the code, so you can decide
whether you are comfortable with it. It is accurate for the version of the
code this guide ships with.

**Short version:** with push notifications off (the default), Marcellus makes
no connections outside your own network that you did not configure. With push
on, one outside service sees a small amount of alert text and routing
information, described below. Video, snapshots, face data and the database
never leave your server.

## Who sees what when push is on

An alert travels like this:

1. Frigate reports an event over MQTT.
2. Marcellus decides whether it is worth an alert.
3. Marcellus posts the finished alert to the **push relay**, a small
   Cloudflare Worker run by the app's developer.
4. The relay forwards it, unchanged, to Apple's push service (APNs), which
   delivers it to your phone.

The relay exists because Apple only lets the developer team that owns the
app sign push messages. It is the only third party Marcellus talks to on your
behalf (Apple is behind it). The relay's source is public at
`github.com/helicopterrun/elsinore-push-relay`; it states that it forwards the
alert in flight and does not store or log the message body. That is a claim
about the relay, not something this repository can prove, which is why you can
run your own (see below).

### The relay receives

Every request carries the phone's **APNs device token** and its APNs
environment (`production` or `sandbox`), an `apns-collapse-id` (the alert's
card key, below), and the shared secret `push.relay_key` as an `x-relay-key`
header. The rest depends on the kind of alert. For each, these are all the
fields (see `push/delivery.py`, `push/live_activities.py`,
`push/doorbell.py`):

**Alert banner (a "card")**

- `title` and `body` text, for example "Person near Front Door" and
  "Alley Wide camera · 2 min". These contain the subject kind (person,
  vehicle, package and so on, or Frigate's object label), your zone display
  name, the camera name, how long ago, and short status words such as
  "approaching the house" or a rounded distance in feet.
- `camera`, `zone_name`, `subject_kind`, `place_class` (Public, Grounds,
  Entrances or Restricted), `level`, `mutation` (created, escalated,
  resolved and so on), and `glyph` (an icon id).
- `card_key`: built from the camera name, the subject kind and Frigate's
  internal track id for the object.
- Timestamps, a sound name, and an interruption level.
- `media`: a URL of the form `<push.external_base_url>/v1/push/thumbnail/<handle>`.
  This is a link to **your** server, not a picture. The relay (and Apple) see
  the address you set in `external_base_url`. If you leave it empty (the
  default), `media` is left out entirely.
- When an object crossed several cameras: `encounter_id` and the ordered list
  of camera names (`cameras_path`).

**Live Activity (the Lock Screen widget)**

The `content-state` contains: `level`, `mutation`, `glyph`, `primary` and
`secondary` (the same title and body text as above), `elapsed_seconds`,
`deep_link_card_key`, `thumbnail_handle` (the same opaque handle as in the
`media` URL), `thumbnail_revision`, `state_since_ts`, `story_started_ts`,
`camera`, `extra_stories`, `cameras_path`, `encounter_id`, and three
optional drawing aids:

- `motion`: a heading word (approaching, leaving, passing, stationary) and
  optionally a speed word.
- `zones`: up to five zone display names and which one the object is in.
- `path`: up to 30 `[x, y]` points, each rounded to two decimals and
  expressed as a fraction of the camera frame (0 to 1). It is the object's
  recent trail inside one camera's picture. It is not a map position and
  carries no timestamps, and it is only a few dozen numbers.

A Live Activity start also carries `attributes`: `card_key`, `family`,
`camera` and `track_id`.

**Doorbell ring (only if you set up UniFi Protect)**

Title "Someone's at the door", a body of the camera name and the local time,
the camera name, the Protect event id, a `media` URL as above, and the labels
of the quick-reply options you configured.

**Test push**

Only the device token and environment. The relay supplies fixed text itself.

### Do people's names reach alerts?

No. The code that writes alert text, `_copy()` in
`src/marcellus/push/delivery_wire.py` (the definition begins at line 297; its
docstring states "Never takes an identity"), has no parameter through which a
name, handle or face number could arrive. Every caller passes only the subject
kind, label, camera, zone and elapsed time (call sites at lines 1193, 1588,
1860, 1935 and 2161). When a face is recognised
(`handle_recognition_event`, line 2087) the alert may be downgraded to a
quieter level, but the words are built by the same function and so are
identical to an unrecognised person's. Recognised-person names appear on the
Encounters pages and the Identities pages of your own server, never in a
push. Encounter titles are built only for those pages, and the encounters
design notes say "Push never uses these titles".

The camera and zone names are your own labels, and can be personal (for
example a zone you named after a room). Rename them if you do not want them
passing through the relay.

### What the relay never receives

- Video, recordings, snapshot or thumbnail **image bytes**. The phone fetches
  the thumbnail itself from your server using the `media` URL or handle, so
  the image goes from your server to your phone and nowhere else. (This
  requires the phone to be able to reach `external_base_url`.)
- Face crops, face embeddings, names or identity numbers.
- Your Frigate address, Frigate credentials, or anything from the database.
- Your sidecar or Frigate login. The phone's sign-in goes to your server only.

## Everything else Marcellus connects to

All of these are addresses **you** configure, on your network unless you point
them elsewhere:

- **Frigate** (`frigate.base_url`, `frigate.proxy_base_url`) for events,
  snapshots and the authenticated proxy.
- **Frigate's MQTT broker** (`push.mqtt_host`), for events when push is on.
- **UniFi Protect console** (`unifi_protect.console_url`), only if enabled.
- **External sources**, such as a wildlife camera on your network
  (`external_sources`), only if enabled.
- **The push relay** (`push.relay_base_url`), only when `push.transport` is
  `relay`.
- **Model download for face recognition.** If you install the `enrich` extra,
  the face-model library (insightface) downloads its model pack, roughly
  300 MB, the first time into `face_enrich.model_dir`. After that it works
  offline. This is the library's behaviour; Marcellus has no download code of
  its own.

Marcellus has **no telemetry, analytics, crash reporting or update check**.
A search of the source finds no code that contacts any other host, and the
web pages load no scripts, fonts or images from other sites. Updating is
something you do yourself. The login page and the Inbox page contain ordinary
links to GitHub and to Frigate's websites, which your browser only follows if
you click them.

## Running with push off

Push is off by default. Leave `push.enabled: false` and Marcellus never
contacts the relay or Apple, and runs fine without them: scrub, triage,
encounters, People and the web pages do not depend on push.

If you want to try the push features without sending anything outside,
set `push.enabled: true` and keep `push.transport: mock` (also the default).
Alerts are then built and logged but never sent anywhere.

## Running your own relay

The relay is a small open-source Cloudflare Worker. Because Apple ties push
signing to the team that owns the app, a relay can only deliver to an app
built by that same team, so this is mainly for forks of the app. Point
`push.relay_base_url` at your own deployment and set `push.relay_key` to the
`RELAY_KEY` secret you chose there.
