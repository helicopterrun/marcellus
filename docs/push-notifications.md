# Push notifications

How the sidecar turns Frigate detections into APNs pushes and Live
Activities. This is the current-state design doc for everything under
`marcellus/push/`; the wire contract for card payloads lives in
`docs/apns-payload-spec.md`.

## The routing model: the merged outcome ladder

One dial per **subject × place** answering "what happens?". Subjects are
`person` / `vehicle` / `animal` / `thing`; places are `street` / `yard` /
`doors` / `private` / `off_limits`. Each cell holds one of five ordinal
outcomes (`policy_settings.OUTCOMES`):

| Outcome | Meaning |
|---|---|
| `off` | suppressed entirely — never evaluated, never logged as a card |
| `log` | recorded, visible in the app, no delivery |
| `glance` | Live Activity only — never a banner or sound |
| `notify` | banner + Live Activity |
| `alarm` | urgent: sound, re-sounds, time-sensitive |

In the settings document, **`outcomes` is authoritative**. The evaluator
itself still consumes four routing *levels* (`log < quiet < notify <
urgent`, `ladder_policy.LEVELS`), so `routing_table_v2` is derived from
`outcomes` on every save (`glance → quiet`, `alarm → urgent`, `off → log`)
— older app builds reading `routing_table_v2` and the evaluator stay in
step by construction. `off` has no legacy level, so it is enforced as
**pre-evaluation suppression**: `apply_settings` collects every `off` cell
into `ladder_policy.set_off_cells`, and the evaluator answers `SUPPRESSED`
for them before any nudge could raise the result. A legacy settings body
(no `outcomes` block) has its outcomes derived from its levels, and a
stored `off` cell survives that round trip: the legacy shape renders `off`
as `log`, so stored-off + incoming-log stays off.

**Zone overrides outrank table cells** — including `off` cells. An
explicit per-zone rule is the user's most specific statement.

The legacy `routing_table` (subjects `stranger`/`known`/`animal`/`thing`)
is still stored and validated for old clients; `startup` migrates a v1-only
file to v2 once (`migrate_v1_to_v2`: `person` := stranger row, `vehicle` :=
thing row bumped one tier at doors/off_limits, `recognition` inferred from
the stranger/known gap). The evaluator uses `routing_table_v2` when
present, falling back to `routing_table`.

**Recognition** (`recognition.known_person` / `known_vehicle`, one of
`off` / `relax_one` / `relax_to_quiet`) replaces the old `known` subject
row: a recognized subject relaxes the person/vehicle cell rather than
routing through a separate table row. `probe_recognition_available` checks
Frigate's config for face recognition and LPR so the app can hide the
controls when the capability doesn't exist.

## Event sources

`push/mqtt.py` subscribes to `frigate/reviews` and `frigate/events` over
MQTT (not `/api/events` polling). `frigate/available` is watched so a
Frigate outage is distinguished from "no devices matched";
`offline_silence_s` of broker silence triggers the same back-fill path
(`GET /api/events?after=...`) used on reconnect. Broker disconnects
reconnect with capped exponential backoff (`compute_backoff`).

Division of labor between the two topics, measured against this deployment:
`frigate/reviews` publishes only on *data* changes (a person standing still
generates no traffic), so it is the sole authority on whether anything is
push-worthy; `frigate/events` (~0.2–0.5s per object) carries
`current_zones` — live occupancy that drops a zone on exit — so dwell,
loiter, and resolution come from there. Backfilled events have no
`severity`; they are treated as `severity="alert"` (the conservative
choice) with the event's own `label` used for label filtering.

## The attention ladder (evaluation)

`push/ladder.py` answers one question, statelessly: given a detection
snapshot, how loud is it? Output is one routing level or `SUPPRESSED`. All
policy is data in `ladder_policy.py` — the live subject × place `TABLE`,
`OFF_CELLS`, `ZONE_OVERRIDES`, `WORRY_REASONS` / `CALM_REASONS` nudges,
`DANGEROUS_ANIMAL_LABELS` (reclassify as stranger/person),
`SYSTEM_CARD_LEVEL`. `policy_settings.apply_settings` rebinds these module
globals; `ladder.py` reads them at call time, so a settings PUT is live on
the very next evaluation with no cache to invalidate.

Evaluation order: mute → system-card short-circuit (`source == "system"`
has no subject/place and returns a fixed level) → safety exceptions
(`audio_safety`, `ai_flagged` → unconditional `urgent`) → dangerous-animal
reclassification → zone override (replaces the table result outright,
bypassing nudges/floor/caps — "always" is the point) → off-cell suppression
→ base table lookup → one net worry/calm nudge (`animal` never nudges;
recognized subjects never nudge up) → child-hazard-zone floor (at least
`notify`) → street/unconfirmed-detector caps (at most `quiet`).

To change built-in policy: edit `ladder_policy.py`, then run
`pytest tests/test_push_ladder.py`. `fixtures/ladder/ladder_cases.json` is
the golden suite — hand-authored, one row per precedence rule; a policy
change that alters a case's outcome must update its `expected` value
deliberately, in the same commit.

### Subject and place classification

`delivery_wire.classify_subject` is a deliberate MVP: `person` +
`sub_labels` (known if present, never the reverse), `_VEHICLE_LABELS`,
`_ANIMAL_LABELS`, else `thing`. `classify_place` checks the user's
`settings.zone_classes` first, then `policy_settings.guess_zone_class` — a
name heuristic checking doors → off_limits → street → private → yard
patterns in that order (most specific/alarming first; the order resolves
real collisions like "front_entry_person" and "sidewalk"), falling back to
the camera name, defaulting to `yard`. Tightening either is a data change
in `delivery_wire.py`, never a change to `ladder.py`.

## Cards: the delivery pipeline

`push/cards.py` (pure), `push/card_store.py` (sqlite), `push/delivery.py`
(payload + orchestration), `push/delivery_wire.py` (wire-up), gated by
`push.delivery_enabled`.

A **card** is the unit of user-facing state: one card per ongoing subject.
Five detections of the same person over two minutes mutate one card.
`card_key` is stable and doubles as the `apns-collapse-id`:

```
{camera}:{subject_kind}:{tracked_object_id-or-opening-id}
{camera}:system:{reason}
```

Zone is deliberately **not** part of identity (a live run showed a car
crossing into a zone forking two cards when it was); zone travels on every
payload as `zone_name` and drives mutation classification, not identity.

### Mutation classification

Each new ladder evaluation against a card key is classified
(`cards.classify_mutation`):

| Mutation | Condition | Push |
|---|---|---|
| `create` | no existing (or closed) card | alert at the routed level, sound per budget |
| `enrich` | same level, new facts | silent, same collapse id |
| `escalate` | new level > old | alert with sound, subject to the budget |
| `deescalate` | new level < old | silent, same collapse id |
| `resolve` | explicit `resolved=True` signal | silent, never a sound |
| `suppressed` | ladder returns `SUPPRESSED` | no push; card closes |

`resolved` is never derived from the level — a `thing` at a non-street
place never evaluates below `quiet`, so "the subject is gone" must be an
explicit signal. It rides on `frigate/events`' object `end` message
(`handle_delivery_resolve`). Mute beats resolve.

#### Resolve visibility (alerts-slice2 §E)

Every resolve gets one final push: quiet (no sound, `interruption-level:
passive`), same `apns-collapse-id` as the story it's closing. Whether that
push is **ephemeral** (`ephemeral: true` — the app removes the delivered
row from Notification Center at once) or not depends on the story's peak:

| Story peak (`card.peak_level`) | Zone override ever hit | Resolve |
|---|---|---|
| ≤ `quiet` | no | `ephemeral: true` — removed, scoped to the event's lifetime |
| `notify` or `urgent` | — | `ephemeral: false` — banner replaced in place, body `"…left after 4 min"` |
| any | yes | `ephemeral: false`, regardless of peak |

`ephemeral` is always explicit, never omitted — an app reading it absent
treats the row as coming from an old sidecar and falls back to its own 24h
sweep, while an explicit `false` means "user-visible story, keep it around
until the user acts or the sweep ages it out". This is a sidecar policy
change from the previous slice: a peak-`quiet` story used to get no resolve
push at all (the row was left to the app's own 24h sweep); it now gets an
ephemeral one, so a "Noted" card that never became a banner is still
cleaned up promptly instead of lingering for up to a day.

### Sound accounting — the entire anti-spam policy

Sound at most twice per card: once at `create` (only if `notify`/`urgent` —
a `quiet` create never sounds), once at the first escalation past `quiet`.
Budget is spent by sounds *emitted*, not beats. Further escalations update
level and content silently. An `urgent` card unhandled after
`push.delivery_urgent_resound_s` (default 120s) may re-alert exactly once
(`push.delivery_urgent_resound_enabled`, its own sweep loop in
`server.py`); this third sound doesn't touch `sound_count`. Settings-side,
`mute_sounds` and `quiet_hours` (below) can further quiet all of this.

### Level → APNs mapping

| Level | Push? | `interruption-level` | Sound |
|---|---|---|---|
| `urgent` | yes | `time-sensitive` | default (no Critical Alerts entitlement; `critical` is never attempted) |
| `notify` | yes | `active` | default |
| `quiet` | yes | `passive` | none |
| `log` | no | — | recorded for the decision trace/timeline |

Silent mutations reuse the alert channel with `aps.sound` omitted and the
same collapse id, so the card replaces in place. A `glance` outcome cell
demotes the card's banner entirely — Live Activity only (see below).

### Cross-camera deduplication

Overlapping fields of view mean one physical event can produce a card per
camera. When a fresh `(camera, track_id)` carries a zone,
`_resolve_card_for_track` looks for an open card with the same
`subject_kind` and `zone_name` created within the last 15s
(`_DEDUP_WINDOW_S`); a hit aliases the track onto it
(`push_card_track_aliases`) instead of minting a new card. The merged
card's `camera` stays whichever camera created it; the enriching camera
appears in the copy (`"… · also on {camera}"`). `camera_neighbors` in
settings extends this: declared-adjacent cameras merge same-kind cards even
with disjoint zone sets (symmetric at read time — declaring one direction
is enough). Resolution is asymmetric: an aliased track resolving drops its
alias silently; only the owning track's resolve closes the card.

### Encounter-aware push

Encounters (`docs/encounters.md`) already know that the alley-wide review
and the stairway-wide review three seconds later are one crossing. Push
uses that directly. `PushEngine.handle_event` resolves the review's
encounter *synchronously* before delivery — `EncounterService.link_now`
run through `asyncio.to_thread` under
`push.encounter_link_timeout_s` (0.25s) — because a payload needs the id
before it is built. On timeout or error the push goes out exactly as it did
before this feature (per-camera card, camera `thread-id`) and the log is
rate-limited to once a minute; the ordinary queued `on_review` worker links
the review a moment later regardless, and the two paths are idempotent
(`store.upsert_atom` only ever re-homes a sealed-donor or lone-founder
atom).

Two flags, independently useful:

- `push.encounter_threading` (default **on**) — presentation only. The APNs
  `thread-id` becomes the encounter id instead of the camera, so every
  camera of one crossing collapses into a single Notification Center group,
  and the payload carries `encounter_id`. Cards stay per-camera.
- `push.encounter_merge` (default **off**) — routing. A later camera's
  review of the same encounter is aliased onto the card the first camera
  already opened: one card, one collapse id, one notification that updates
  in place. With the flag off the would-be merge is logged at DEBUG
  (`"encounter_merge would have routed <track> onto <card_key>"`) so real
  duplicates can be counted against it before switching it on — the same
  validation shape `geometric_dedup` used.

Routing order in `_resolve_card_for_track`: track alias → **encounter** →
zone/geo dedup → natural key. The encounter step is gated on subject
family (`encounters.linker.family_of` over the card's stored label and the
event's), so the person and the car they arrived in can share an encounter
without sharing a card. A card that has already resolved or closed is never
a merge target: the encounter's story does not reopen — the new review
mints its own card and is grouped by `thread-id` instead.

An encounter-stamped card records `cameras_path` (ordered distinct cameras,
first-seen order, persisted as `cameras_path_json`). Once it holds two or
more entries the notification body *becomes* the path — `"Alley Wide →
Stairway Wide → Gate Walkway"`, capped at four entries with a leading `…` —
replacing the `" · also on X"` suffix, which stays for ordinary
(non-encounter) zone/geo dedup merges. A camera-crossing update on an open
card is an ordinary ENRICH (visible, no re-sound) unless the ladder itself
says ESCALATE; no new mutation kind was added. `apns-collapse-id` remains
the card key.

`encounter_id` and `cameras_path` are additive payload fields (no `v` bump)
and additive Live Activity content-state fields, exactly like
`extra_stories`: both stay off the wire when absent, so a single-camera
story's content state is byte-identical to before.

### Payload contract

`docs/apns-payload-spec.md` is the versioned (`"v": 1`) contract:
`card_key`, `mutation`, `level`, `subject_kind`, `place_class`, `camera`,
`zone_name`, a semantic `glyph` id (icon mapping is client-side),
`primary`/`secondary` copy (state-what-is-true grammar), `event_ts`,
`state_since_ts`, optional `media`/`deep_link`. Zone display names in copy:
the sidecar-edited `zone_names` setting wins, Frigate's `friendly_name` is
the fallback, humanized key last (`policy_settings.zone_display_name`).

`media` is minted on `create`/`enrich` only (never
escalate/deescalate/resolve): a handle is minted and the thumbnail
pre-warmed concurrently with the send — a slow or failed Frigate fetch
costs the notification its image, never its existence. `media` is one
complete URL built from `push.external_base_url` (omitted until set);
Frigate itself stays LAN-internal and the sidecar re-hosts the snapshot
behind the handle.

## Live Activities

`push/live_activities.py` (pure) plus `_deliver_live_activities` in
`delivery_wire.py`, gated by `push.delivery_la_enabled` (default on, only
reachable through `push.delivery_enabled`).

**Three tokens.** The device's alert token carries ordinary pushes.
`push_to_start_token` (one per install, on the registration) creates
activities. A per-activity token, uploaded via `POST
/v1/push/activity/token` once iOS mints it, carries updates and the end —
iOS rejects update/end on the push-to-start token. There is always a window
where an activity is on screen the sidecar cannot yet update; updates
resume on the next observation. A card that resolves before its token
arrives is flagged `pending_end` and the token-upload route sends the
deferred end immediately (`end_activity_if_card_closed`).

**Families** (`should_start_activity`, gated by the per-family booleans in
`settings.live_activities`): `package`, `bins`, `openings` (with
`opening_picks` — empty means "nothing curated yet, everything qualifies",
not "nothing qualifies"), `person` (person at `doors`), and an `activity`
catch-all used when Live Activities are the sole surface.

**Glance / la_only.** A `glance` outcome cell is la_only applied per cell:
the card runs a Live Activity and its banner push is demoted to
passive/silent. The global `live_activities.la_only` flag does the same for
every pushable card — starts carry no sound, updates never carry an alert
dict, the urgent re-sound is silent. Both `la_only` and
`live_activities.delivery` (`la_first` | `notifications`) are **sticky**
across PUTs that omit them, because the app's settings model round-trips
through a fixed Codable type that drops unknown keys.

**Lifecycle.** `start` (push-to-start token, on a qualifying create;
carries `attributes` / `attributes-type: "ElsinoreActivityAttributes"` —
the exact Swift type ActivityKit routes by) → `update` (per-activity token,
on later mutations, silent by construction) → `end` (on resolve;
`dismissal-date` is timestamp + 30 so the resolved state shows briefly).
`content-state` field names are snake_case to match the Swift `CodingKeys`:
`level`, `mutation`, `glyph`, `primary`, `secondary`, `elapsed_seconds`,
`deep_link_card_key`, `thumbnail_handle`, `thumbnail_revision` (the same
handle the card push's `media` uses — no second handle per snapshot).
Updates are rate-limited per activity (`_LA_UPDATE_MIN_INTERVAL_S`, 3s) and
delta-gated (in-memory previous-state snapshot; a restart just means the
first post-restart push always goes out). `camera_headings` /
`camera_layout` / `secure_area` / `map_scale_ft` in settings feed the LA's
heading chip and map trail (`derived_camera_heading` projects "toward home"
from drawn geometry; an explicit heading always wins) — display only, never
routing.

Activities live in the `push_activities` table keyed on
`(apns_token, situation_id, track_id)` — one activity per (device, card),
which a nullable column on `push_cards` could not represent. The track id
is parsed from `card_key`'s final component, so cross-camera dedup keeps
updating the same activity. `DELETE /v1/push/activity/token/{activity_id}`
drops the row when the app ends the activity locally.

## Situations (retired — Phase 5 §1)

The situations pipeline (situation-only evaluation and the v1
camera/label/severity dispatch that preceded it) is retired: the
card/attention-ladder pipeline above (`push/delivery_wire.py`) is the only
alert path, and no review or object message emits a situation push. What
survives, for back-compat with older app builds: registrations carrying a
`situations` array are still accepted and stored (`push/situations.py`
keeps the parser/model), the starter library
(`GET /v1/push/situations/library`) still serves, and
`POST /v1/push/test/{situation_id}` still fires one legacy-shaped push on
demand. The situation and Live Activity payload builders
(`push/payload.py`, `push/activity.py`) remain as the wire-shape record
(see `docs/apns-payload-spec.md`).

## The settings document

`push/policy_settings.py` owns the shape, defaults, validation,
persistence (`config/push_settings.json` — JSON, not YAML, because the app
PUTs JSON and YAML type coercion on the way back out is a bug factory), and
application. Top-level keys, per `default_settings()`:

| Key | Contents |
|---|---|
| `v` | `SETTINGS_VERSION` (1), bumped only on breaking shape changes |
| `outcomes` | **authoritative** subject × place → outcome grid (v2 subjects) |
| `routing_table_v2` | derived levels the evaluator consumes |
| `routing_table` | legacy v1 (stranger/known) table, kept for old clients |
| `recognition` | `known_person` / `known_vehicle`: `off` \| `relax_one` \| `relax_to_quiet` |
| `zone_classes` | zone → place class (user-confirmed) |
| `zone_names` | zone → display name for notification copy; wins over Frigate's `friendly_name` |
| `zone_overrides` | `{zone: {subject: level}}` — outranks everything but mute/system/safety |
| `live_activities` | per-family booleans, `opening_picks`, `delivery`, `alert_all_changes`, `la_only` |
| `escalation_sound`, `mute_sounds` | sound tuning |
| `quiet_hours` | `null` or `{start, end, mode}` (HH:MM, wrap-around ok; `cap_quiet` \| `mute_sounds`) |
| `camera_neighbors` | camera → adjacent cameras, for cross-camera dedup |
| `camera_headings` | camera → unit `{dx, dy}` "toward home" vector (LA heading chip) |
| `camera_layout` | camera → `{x, y[, azimuth, fov]}` on the layout map |
| `secure_area`, `map_scale_ft` | drawn secure rectangle and map scale (world projection) |
| `camera_optics` | camera → `{hfov, mount_ft, tilt_deg[, vfov, faces, lens, note]}` rig facts — seeded once from `optics.DEPLOYMENT_SEED` at startup, edited via /cameras onboarding; feeds `ground.camera_ground` |
| `floorplan` | `null` or `{ext, w, h, uploaded_at, calibration}` — the uploaded layout-map background (`POST/GET/DELETE /v1/push/floorplan`); `calibration` remembers the drawn scale-reference line, `map_scale_ft` stays the operative scale |

`validate_settings` returns human-readable errors; unknown *top-level*
fields are ignored (forward compat), but an unknown subject/place/family
key inside a known block is a 400 — those vocabularies are closed, so a
typo there is more likely a client bug than a field the sidecar hasn't
learned. `normalize_settings` merges a partial document onto defaults so an
older-shaped file works forever; empty zone-override rows are dropped on
save. `zone_overrides` outer keys are unrestricted (the user may configure
a zone before it exists in Frigate); the inner vocabulary is closed.
`save_settings` is write-then-rename; `load_settings` falls back to plain
defaults on a missing or corrupt file rather than failing every evaluation.

### Settings sync: `GET`/`PUT /v1/push/settings`

`GET` returns the *live, applied* policy (`get_active()`), never an
independent disk read, so it can never show something other than what the
evaluator is using; a fresh install's first `GET` creates the file. The
response wraps `settings` with derived, read-only context so the app needs
no second call: `available_cameras`, `available_zones` (each with cameras,
`guessed_class`, `friendly_name`), `available_openings`, `derived_headings`,
`placement_deployments`, `recognition_available`.

`PUT` validates, normalizes, persists, and applies immediately
(`apply_settings` → `set_table` / `set_off_cells` / `set_zone_overrides`;
everything else is read via `get_active()` per card event). Config-side
keys the app has no UI for (`camera_neighbors`, `camera_headings`,
`camera_layout`, `zone_names`, `camera_optics`) are sticky unless
explicitly sent, as are `la_only` and `delivery`; `secure_area` /
`map_scale_ft` / `floorplan` distinguish absent (sticky) from explicit
null (clear). `placement_deployments` in the `GET` response is the
settings-backed `camera_optics` table under its historical name.

## Decision trace and the tuning loop

`GET /v1/push/decisions` serves `push/decision_trace.py`: a durable log in
the sidecar SQLite DB (`push_decisions`, 30-day retention, 200 served max
per page, `before`/`card_key` cursor+filter) of routing decisions, one per
event pre-fanout, newest first — a restart no longer loses the trail. Each
entry: `id`, `ts`, `camera`, `label`, `subject`, `zones`, `place`, `level`,
`reasons`, `event_id`, plus `stage`, `modifiers`, `card_key`, `mutation`,
`zone`, `sound`, `sent` (and, once annotated, `family`/`la_started`/
`la_reason`) and a `silenced` field computed at read time against
`push_silences`. `stage` names which rule produced the level and
`modifiers` are the nudges/caps applied on top:

| stage/modifier | plain English |
|---|---|
| `muted` | Alerts were paused |
| `system` | Server notice |
| `safety` | Safety exception: always urgent |
| `zone_override` | Your rule for {subject} in {zone} |
| `off_cell` | {subject} in {place} is set to Off |
| `table` | {subject} in {place} is set to {level} |
| `nudge_up` | raised one step (nobody home / night / approaching …) |
| `nudge_down` | lowered one step (known / leaving …) |
| `child_hazard_floor` | at least Notify: child-hazard zone |
| `street_cap` | capped at Quiet: street |
| `unconfirmed_cap` | capped at Quiet: detector unconfirmed |
| `quiet_hours_cap` | capped at Quiet: quiet hours |
| `reclass_dangerous_animal` | treated as a person: dangerous animal |

Append never raises and a write failure is
logged and swallowed rather than dropping the notification it's describing.
This feeds the app's Recent Decisions screen — the tuning loop is: see a
decision you disagree with, see which cell/override/reason produced it,
change that cell (or one-tap **Quiet this** via `POST /v1/push/silence`),
and the next evaluation uses the new policy. Events silenced by an `off`
cell are traced too (level `"off"`, reason `suppressed`, once per track) —
otherwise the feed goes dark for exactly the cells the user silenced and
there is no evidence trail to dial one back up. `POST /v1/push/feedback`
logs a per-card verdict (tuning trace only; no routing changes yet).

### Status, silence, and overrides

- `GET /v1/push/status` — one-glance health: `frigate_available` /
  `mqtt_connected` from the live MQTT subscriber, `last_review_at` /
  `last_decision_at` / `last_sent_at` (+ level/card_key) and
  `decisions_since_last_sent` from `push_decisions`, `quiet_hours_active`,
  `devices` registered. Cheap — one or two SQL queries plus in-memory flags,
  no Frigate HTTP round-trip. `paused_until` is always `null`: there is no
  global timed-mute concept, only the per-cell/per-zone silences below.
  Alerts-slice2 §C adds `relay`: `{"last_ok_at", "last_error",
  "last_error_at", "last_status_code"}`, an in-memory, process-lifetime
  record updated by `push/transport.py`'s `RelayTransport` on every relay
  HTTP response (a restart resets it — a "since last restart" snapshot).
- `POST /v1/push/silence` — `{"card_key": ...}` drops the cell that card
  routed through (its zone override, or its outcomes-table cell if it has no
  zone) to `quiet`, through the same validate → normalize → save → apply
  path as `PUT /settings`, and records a `push_silences` audit row. 404
  `card_not_found` for an unknown key.
- `PUT /v1/push/overrides` — the fine-grained editor: set or clear one
  `zone_override` cell (`level: null` removes it), or set one `outcome_cell`
  directly (`level` may not be `null` there — an outcomes cell always has a
  current value). 422 for a bad `kind`/`level` enum or missing required
  fields for the given `kind`.

## HTTP surface (`routes/push.py`)

All routes share the sidecar's Frigate-session auth (no second credential)
except `GET /v1/push/thumbnail/{handle}`, protected by the handle itself
being opaque, unguessable, and short-lived — the NSE holds no session.

- `PUT` / `DELETE /v1/push/devices/{apns_token}` — idempotent registration
  keyed on the token; the response echoes the `schema_version` the sidecar
  will evaluate under, `situations_accepted`, and Live Activity readiness.
  Unknown body fields are accepted, dropped, and logged by name. Omitting
  `snoozes` leaves existing ones alone; explicit `[]` clears them.
- `POST /v1/push/devices/{apns_token}/test` — alerts-slice2 §D: sends a
  real **card** payload (`mutation: "test"`, synthetic `card_key`,
  `v:1`/`mutable-content:1`) through the same relay call real cards use
  (`transport.send_situation`), bypassing filters but not environment
  routing. The NSE processes it and can post a receipt exactly like a real
  alert — this is a genuine round trip, not just "APNs accepted the
  request". Recorded in `push_card_sends`; explicitly skips
  `push_decisions` (it isn't a routing decision). Rate-limited to one per
  token per 10s (429 otherwise). 404 means "token not registered"
  (reserved — the released client maps it to a specific message), 503
  `push_disabled`, 502 `test_send_failed`. Response:
  `{"sent": true, "card_key": "test:…", "sent_ts": …}`.
- `POST /v1/push/receipts` — alerts-slice2 §A: batch delivery receipts from
  the NSE (right after handing content to the OS) and the app (flushing
  what the NSE couldn't deliver, or `willPresent` foreground observations).
  Paired against `push_card_sends` by `(apns_token, card_key, mutation)`,
  nearest prior send within 24h (this sidecar's send-side table carries no
  `state_since_ts` to pair on exactly). Duplicates
  (`apns_token`+`card_key`+`mutation`+`state_since_ts`) are ignored via a
  unique index, not errors. Unknown tokens are still stored, never 404.
  Response `{"accepted": n, "matched": m}`. 30-day retention.
- `GET /v1/push/devices/{apns_token}` — alerts-slice2 §B: registration
  facts plus send/receipt stats over a window (`window_days`, default 7,
  max 90): `sent`/`received` counts, `median_latency_s`/`p90_latency_s`
  (nearest-rank percentile over paired receipts), `last_sent_at`/
  `last_received_at`, `last_send_error`/`last_send_error_at`. 404 for an
  unknown token.
- `GET /v1/push/situations/library`, `GET /v1/push/sounds` — starters
  (legacy, see "Situations (retired)") and the sound catalog (keyed on `app_version`; the `.caf` assets ship in the
  app bundle).
- `POST /v1/push/snooze`, `DELETE /v1/push/snooze/{scope}` — **deprecated**,
  superseded by `registration.snoozes` (full-state replace on every device
  PUT); kept one release. Scopes: `global`, `situation:<id>`,
  `camera:<name>` (per-device — snoozing the iPad must not quiet the
  iPhone). Expiry is a timestamp, not a scheduled job.
- `POST /v1/push/test/{situation_id}` — fire one legacy situation-shaped
  push at the named device (the pipeline itself is retired; this test
  button still works for devices registered with situations).
- `POST /v1/push/activity/token`, `DELETE /v1/push/activity/token/{id}` —
  Live Activity token upload / local-end teardown (see above).
- `GET /v1/push/thumbnail/{handle}`, `GET /v1/push/handle/{handle}` —
  pre-warmed snapshot bytes; handle → `{camera, event_id, snapshot_url}`.
- `GET /v1/push/decisions`, `GET`/`PUT /v1/push/settings`,
  `POST /v1/push/feedback` — see above.
- `GET /v1/push/status`, `POST /v1/push/silence`, `PUT /v1/push/overrides`
  — see "Status, silence, and overrides" above.

## Privacy model and the relay

The relay's inputs for v1 pushes are exactly `{device_token, environment,
handle, server_id, severity}` — no camera name, label, or anything
content-bearing reaches the transport layer. Camera/label/thumbnail are
only available after the NSE redeems the handle from the user's own server;
the handle→event mapping never appears in the APNs payload. Snapshots never
transit the relay at all — the sidecar pre-warms a ~320px/q60 thumbnail
under the handle for 24h and the NSE fetches it locally.

The transport is an interface (`push/transport.py`): `LogTransport` (the
default, `push.transport: mock`, what every test runs against) and
`RelayTransport`, which posts to
[elsinore-push-relay](https://github.com/helicopterrun/elsinore-push-relay)
(a Cloudflare Worker holding the one team-bound APNs key). Four relay
routes, because they differ in exactly the fields the relay controls:

- `/v1/relay/push` — v1-shape, relay templates the text by severity.
- `/v1/relay/test` — fixed test text, no `handle`, no `mutable-content`.
- `/v1/relay/situation` — sidecar-built full APNs body forwarded verbatim
  (a situation's title is user-authored; a severity template can't produce
  it). The relay signs the JWT, sets topic/push-type/priority, validates
  `payload.aps`, and 422s anything over 4KB. Card payloads ride this route
  too (`send_situation`). The relay forwards these bytes in flight without
  persisting, logging, or inspecting them — "content-free *at rest*".
- `/v1/relay/liveactivity` — `apns-push-type: liveactivity`, topic
  `<APNS_TOPIC>.push-type.liveactivity`, `event: start|update|end` so the
  relay can validate shape (a start must carry `attributes`). Delivery
  hints ride as `apns_priority`/`apns_expiration` (underscores — the
  relay's key spelling; hyphenated keys are ignored).

**`prod` vs `production`.** This sidecar's API, its DB CHECK constraint,
and the spec all spell it `prod`; the relay's wire API spells it
`production` and 422s anything else. `RelayTransport` translates at that
one boundary so `prod` stays the only spelling everywhere else here.
Registration requires the app to state `environment` explicitly (read from
its own `aps-environment` entitlement) — sandbox and production APNs are
different endpoints and it is never inferred. The test-push route
deliberately keeps environment routing so a black-holed mismatch fails
visibly there.

**`apns-collapse-id` is capped at 64 bytes** (Apple, and the relay
truncates rather than rejects). `build_collapse_id` trims from the head and
keeps the track id whole, so two subjects 30s apart never share a collapse
id.

## Failure modes

- A `410`/`400` from the transport is a permanent dead token: the device
  row is pruned immediately (`push/engine.py`), never retried. This is the
  primary cleanup path — the app can't promise to DELETE before uninstall.
- A retryable relay failure (network error, 5xx, 429) gets bounded in-request
  retries with exponential backoff and jitter (`transport.py:40-68`,
  `compute_retry_delay` — 0.5s/1.5s/4.5s... capped, or the relay's own
  `Retry-After` capped at 5s) up to `relay_retry_attempts` (default 3) before
  giving up on that one send. There is still no *durable* retry queue behind
  it: once those in-request attempts are exhausted the send is abandoned and
  left for the next live event, not persisted for a later replay — the
  system degrades to no notifications, not a crash.
- A missing/corrupt settings file falls back to defaults, never a failed
  evaluation.
- Every thumbnail failure path costs the notification its image, never its
  existence; a thumbnail/handle miss is a 404 and the alert delivers
  without an image.
- MQTT outages reconnect with backoff and back-fill the missed window.

## Tests

- `tests/test_push_ladder.py` + `fixtures/ladder/ladder_cases.json` — the
  evaluator's golden suite (hand-authored precedence rows).
- `fixtures/ladder/delivery_cases.json` + `tests/test_push_delivery.py` —
  golden *sequences*: ordered snapshots against one card key with an
  expected `(mutation, level, sound, push)` per step.
- `tests/test_push_cards.py` / `test_push_card_store.py` /
  `test_push_delivery_payload.py` — classifier, sound budget, persistence,
  payload/orchestration against `LogTransport`.
- `tests/test_push_live_activities_wire.py` — family detection, payload
  shapes, and full lifecycles including token-race and dedup cases.
- `tests/test_push_policy_settings.py` / `test_push_settings_routes.py` /
  `test_push_delivery_wire.py` — settings defaults, validation, the
  zone-guessing heuristic, persistence, that `apply_settings` changes real
  evaluation output, and the HTTP surface end to end.

Note for tests: `ladder_policy.TABLE`'s built-in literal and
`policy_settings.DEFAULT_ROUTING_TABLE` are deliberately separate
baselines; a real deployment always runs `policy_settings.startup` from
`server.py`'s lifespan, so the distinction is invisible in production.
`tests/conftest.py` snapshots and restores `ladder_policy.TABLE` around
every test.
