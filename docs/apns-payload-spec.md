# Card push payload contract (v1)

The wire contract the iOS app (Elsinore) and its Notification Service
Extension build against for the attention ladder's delivery pipeline
(`docs/push-notifications.md` § "Attention ladder: delivery pipeline
(Elsinore Phase 2)"). Every push the delivery pipeline sends -- create,
enrich, escalate, deescalate, resolve, and the urgent re-sound -- carries
this shape. It is a **different payload family** from the situations
payload (`push/payload.py`, `docs/push-notifications.md`'s "Situations"
section) and from the Live Activity payloads (`push/activity.py`) -- a
device can receive any mix of the three, distinguished by top-level shape
(`situation_id` vs. `card_key` vs. `attributes-type`), not by a shared
envelope. (The situations family is retired — its builder remains only for
the wire-shape record.)

This payload family covers ordinary alert or silent pushes. Nothing here
starts, updates, or ends a Live Activity (`push/activity.py`).

**Vocabulary note:** the wire speaks the legacy *level* names — `log`,
`quiet`, `notify`, `urgent`. The user-facing outcome ladder maps onto them
before payload building (`glance→quiet`, `alarm→urgent`; `off` suppresses
the event before any payload exists), so no payload ever carries an
outcome name.

## Versioning

`"v": 1`. A breaking change to any field's meaning or a removed field bumps
this; an additive, optional field does not. The app should treat an unknown
`v` as "don't parse the rest of this payload, fall back to the plain
`aps.alert` text" -- never crash on a version it doesn't recognize yet.

## Example

```json
{
  "aps": {
    "alert": {"title": "Person at Front Door", "body": "Front Door · 6s"},
    "sound": "default",
    "interruption-level": "time-sensitive",
    "mutable-content": 1
  },
  "v": 1,
  "card_key": "front:stranger:track-42",
  "mutation": "escalate",
  "level": "urgent",
  "subject_kind": "stranger",
  "place_class": "doors",
  "camera": "front",
  "zone_name": "doors",
  "glyph": "person.stranger",
  "primary": "Person at Front Door",
  "secondary": "Front Door · 6s",
  "event_ts": 1785952622.704,
  "state_since_ts": 1785952618.0,
  "media": "https://sidecar.local/v1/push/thumbnail/h_9f3a",
  "deep_link": "elsinore://card/front:stranger:track-42?t=1785952618.0"
}
```

A silent mutation (enrich/deescalate/resolve, or a `quiet`-level card at any
mutation) is the same shape with no `aps.sound` key:

```json
{
  "aps": {
    "alert": {"title": "Package delivered", "body": "Back Yard · 0s"},
    "interruption-level": "passive",
    "mutable-content": 1
  },
  "v": 1,
  "card_key": "back:thing:pkg-1",
  "mutation": "create",
  "level": "quiet",
  "subject_kind": "thing",
  "place_class": "yard",
  "camera": "back",
  "zone_name": "yard",
  "glyph": "thing.package",
  "primary": "Package delivered",
  "secondary": "Back Yard · 0s",
  "event_ts": 1785952000.0,
  "state_since_ts": 1785952000.0,
  "deep_link": "elsinore://card/back:thing:pkg-1?t=1785952000.0"
}
```

## `aps`

| Field | Notes |
|---|---|
| `alert.title` / `alert.body` | Mirrors `primary` / `secondary` below. Some clients (widgets, watch complications) read `aps.alert` without parsing custom keys, so the human-readable text is duplicated here rather than assumed derivable. |
| `sound` | Present (`"default"`) only when this mutation earned a sound -- see the sound-accounting rule below. **Absent, not `null` or `""`**, for a silent push -- APNs itself treats any present `sound` key as "play something". |
| `interruption-level` | `time-sensitive` (`urgent`), `active` (`notify`), or `passive` (`quiet`). Never `critical` -- there is no Critical Alerts entitlement, and none should be attempted. Reflects the **card's current level**, not the mutation -- a silent deescalate still carries the level it deescalated to. |
| `mutable-content` | Always `1`. Runs the NSE so it can attach `media`'s thumbnail; present even when `media` is absent so a later enrich on the same card can still deliver one. |

`category` and `thread-id` are deliberately **not** part of this contract
(unlike the situations payload, which sets both) -- `card_key` already
serves as the grouping key via `apns-collapse-id`, and there is no
per-situation category to select actions by in this phase.

## Top-level fields

| Field | Type | Notes |
|---|---|---|
| `v` | int | Contract version, `1`. |
| `card_key` | string | Stable per ongoing subject; also sent as the `apns-collapse-id` HTTP header (not duplicated into the JSON body a second time under a different name). Scheme: `{camera}:{subject_kind}:{tracked_object_id-or-opening-id}`, or `{camera}:system:{reason}` for a system card. **Zone is deliberately not part of it** -- a card's zone can change over its lifetime (`zone_name` below), and the key must not, or the same subject fragments into multiple cards as it moves. Opaque to the app beyond one documented rule: the camera is always the first `:`-separated component. See `delivery.build_card_key`. |
| `mutation` | string | One of `create`, `enrich`, `escalate`, `deescalate`, `resolve`. Never `suppressed` -- a suppressed card sends no push at all (design doc §2). |
| `level` | string | `log`, `quiet`, `notify`, or `urgent` -- the card's level *after* this mutation. (`log`-level cards never reach this payload; see "What never gets here" below.) |
| `subject_kind` | string | `stranger`, `known`, `animal`, or `thing`. Empty for a system card. |
| `place_class` | string | `street`, `yard`, `doors`, `private`, or `off_limits`. Empty for a system card. |
| `camera` | string | Frigate camera name. |
| `zone_name` | string | The zone this card is about; empty if none applies (e.g. a system card, or a detection with no zone). |
| `glyph` | string | A semantic glyph id, e.g. `package.delivered`, `bins.at-curb`, `person.identified`, `gate.open`. **The app maps ids to SF Symbols/custom assets -- the sidecar never sends an icon file name or asset catalog reference.** Unrecognized ids should fall back to a generic per-`subject_kind` icon client-side, not fail to render. |
| `primary` | string | State-what-is-true grammar: `"Person at Front Door"`, never `"Unknown person"` or anything else asserting an identity that hasn't resolved (design doc §5). This is `aps.alert.title` verbatim. |
| `secondary` | string | `place · elapsed`, e.g. `"Front Door · 6s"`. This is `aps.alert.body` verbatim. |
| `event_ts` | float | Unix epoch seconds, sub-second precision, stamped the moment this payload was built -- the last timestamp the sidecar controls before the bytes leave for the relay (same rationale as the situations payload's `sent_at`). |
| `state_since_ts` | float | When the **current level** became true -- not since the first detector event ever seen for this subject, and not reset by an `enrich` (same level, new facts) or a `resolve` (reports how long the resolved state held). Elapsed time for `secondary` is `event_ts - state_since_ts`. |
| `media` | string, optional | Snapshot URL for the NSE to fetch and attach (`GET {push.external_base_url}/v1/push/thumbnail/{handle}`, unauth, self-authorizing -- same mechanism as the situations payload's handle, just resolved to a complete URL server-side instead of left to the app). Present only on `create` and `enrich` -- the card is still active and the snapshot may improve as Frigate refines the bounding box. Never present on `escalate`/`deescalate` (unaddressed this round) or `resolve` (the card is about to leave Notification Center; spending the NSE's ~15s fetch budget on an image nobody will see is waste). Omitted, not `null`, when there is nothing to show -- including when `push.external_base_url` isn't configured. The URL is sent optimistically alongside the push; if the Frigate fetch behind it is slow or fails, the notification simply lands without an image, never without existing at all. |
| `deep_link` | string, optional | `elsinore://card/<card_key>[?t=<state_since_ts>]`. Omitted, not `null`, when there is nothing more specific than "open the app" to link to. The `t` query param is always appended when `deep_link` is present -- same rounded-to-milliseconds float as `state_since_ts` above, formatted identically. With `t`, the app opens the camera timeline parked at that moment; without a `deep_link` at all, it falls back to the review feed. Additive per the versioning rule above -- adding `t` did not bump `v`. |

## Sound-accounting summary (design doc §3)

A push either carries `aps.sound` or it doesn't; the app does not need to
compute the budget itself; the sidecar decides per-push whether this one
sounds. For reference, the rule this contract's `sound`/`mutation`
combinations follow:

- Sound at most **twice** per `card_key`: once at `create` (only if
  `level` is `notify` or `urgent` -- a `quiet` create never sounds), once
  at the first `escalate` past `quiet`.
- Budget is spent by *sounds emitted*, not by mutation count -- a silent
  `quiet` create doesn't spend it, so the card's first-ever `escalate` can
  still be sound #1.
- `enrich`, `deescalate`, and `resolve` never carry `aps.sound`.
- A third, **urgent-only** re-sound may fire once per card if it is still
  `urgent` and unhandled after a configurable interval (default 120s).
  On the wire this looks exactly like another `escalate` push with
  `aps.sound` set and the same `level` as before (the card didn't change
  level, it just re-alerted).

## What never gets here

| Level | Push? |
|---|---|
| `log` | No push at all -- recorded server-side for the timeline/digest (out of scope this phase), never sent. |
| `suppressed` | No push, no card visible client-side. The card closes silently server-side. |

If a card's level is ever `log` or the evaluation is `suppressed`, no
payload of this shape (or any shape) is sent for that mutation.

## A fourth family: `doorbell.ring` (`push/doorbell.py`)

A UniFi Protect doorbell ring is **not** a card-pipeline payload -- it
bypasses the attention ladder, `card_key`/`mutation` vocabulary, and the
sound-accounting budget above entirely (see
[the UniFi Protect guide](../src/marcellus/guide_content/unifi-protect.md)
and `docs/push-notifications.md` § "Event sources" for why). It is
distinguished on the wire by `aps.category == "doorbell.ring"`.

```json
{
  "aps": {
    "alert": {"title": "Someone's at the door", "body": "Front Door · 6:42 PM"},
    "sound": "default",
    "interruption-level": "time-sensitive",
    "category": "doorbell.ring",
    "thread-id": "doorbell",
    "mutable-content": 1
  },
  "media": "https://sidecar.local/v1/push/thumbnail/h_9f3a",
  "doorbell": {
    "camera": "front_door",
    "protect_event_id": "6183a1b200f1234500005678",
    "ts": 1785952622.704
  }
}
```

- `aps.sound` is always `"default"` and always present -- unlike card
  pushes, a ring is never silent; there is no `quiet`-equivalent level.
- `aps.category` is `"doorbell.ring"` and `aps.thread-id` is always
  `"doorbell"` -- both deliberately used here (unlike the card family
  above), so the app can group all rings in one thread and offer
  ring-specific notification actions by category.
- `media`, when present, is the same top-level handle-URL mechanism the
  card family uses (`push/delivery.py`'s NSE-fetched snapshot) -- here it
  is Frigate's `latest.jpg` for the mapped camera, not an event-specific
  crop, since a ring has no Frigate tracked-object event to snapshot.
- `doorbell.camera` is the Frigate camera name (from `unifi_protect.cameras`
  mapping), `doorbell.protect_event_id` is Protect's own event id (useful
  for correlating with Protect's own UI/logs), and `doorbell.ts` is the
  same unix-epoch-seconds-with-subsecond-precision convention as
  `event_ts` above.
- No `v`, `card_key`, `mutation`, `level`, or any other card-family field
  is present -- this is a separate contract, not an extension of `v: 1`.
- Delivery is recorded in `push_card_sends` like any other send, with
  `card_key = "doorbell:<camera>"` and `mutation = "ring"`, so Push Doctor
  and the receipts table can report on it the same way -- but it is a
  record of the send, not evidence this payload has `card_key`/`mutation`
  keys on the wire.
