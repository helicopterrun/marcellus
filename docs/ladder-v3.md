# Attention ladder v3: four levels, four places

Status: approved by the owner 2026-10-16. Implementation spec for the sidecar
(this repo) and the Elsinore app. Owner-facing write-up of the same design:
the "Alert ladder v3" page shared with the owner. Evidence for the decision
(30 days of decisions, 2026-09-17 to 2026-10-16): 11,395 detections; every
Notify and Alarm was lifted by behaviour, not a table cell; the five places
behaved as two in the owner's table.

## 1. Model

### Levels (user-facing word → wire value)

| Level  | Outcome key (`ladder`) | Engine level | Phone | Notification Center |
|--------|------------------------|--------------|-------|---------------------|
| Ignore | `ignore` | `log`    | nothing; shows in the app only | nothing |
| Quiet  | `silent` | `silent` | nothing: passive push, no sound | a row, kept until cleared |
| Glance | `glance` | `quiet`  | a Live Activity | nothing |
| Urgent | `urgent` | `urgent` | time-sensitive banner, sound per mute | a row, kept until cleared |

Order (quietest first): `log < silent < quiet < urgent`. The engine keeps
`log`/`quiet`/`urgent` because installed app builds read them in decision
history, Live Activity content state and `card.<level>` categories. `silent`
is new. `notify` is retired from routing (see §4.6 for system cards and
legacy input).

`silent` is deliberately not `quiet`: the engine already uses `quiet` for
Glance.

### Places

`street` (Public) · `yard` (Grounds) · `doors` (Entrances) · `off_limits`
(Restricted). `private` is retired: everywhere it appears in input it is read
as `yard` (with the "louder wins" merge where two cells collide, §3.2).
`guess_zone_class` keywords that mapped to `private` (back, side, rear,
alley, fence) map to `yard`.

### Subjects

Unchanged: person, vehicle, animal, thing, package, bin, opening.

## 2. Settings document (`v` = 3)

### 2.1 New key `ladder` (source of truth)

```json
"ladder": {
  "person":  {"street": "ignore", "yard": "glance", "doors": "glance", "off_limits": "urgent"},
  "vehicle": {...}, "animal": {...}, "thing": {...},
  "package": {...}, "bin": {...}, "opening": {...}
}
```

All 7 subjects × 4 places, values in `ignore | silent | glance | urgent`.

**Default (the owner-approved Balanced preset).** A fresh settings file starts
here; every built-in fallback (the evaluator's own table and off cells, the
derived legacy defaults, the one-time V3-subject seeding rows) agrees with it.
Migration of an existing file is unaffected: it translates the stored table.

| Subject | street | yard | doors | off_limits |
|---------|--------|------|-------|------------|
| person  | ignore | glance | glance | urgent |
| vehicle | ignore | silent | silent | glance |
| animal  | ignore | silent | silent | glance |
| package | ignore | silent | glance | glance |
| opening | ignore | silent | glance | urgent |
| bin     | ignore | ignore | silent | silent |
| thing   | ignore | ignore | silent | silent |
On PUT, a `ladder` that is present must be complete for person, vehicle,
animal, thing; package/bin/opening rows are optional (absent row = keep the
stored row). Unknown subject, place or value → 400 `invalid_settings`, as
today.

### 2.2 Derived legacy keys (served, not authoritative)

GET keeps returning `outcomes` (7 × 5, old words) and `routing_table_v2`
(7 × 5, old levels), derived from `ladder` on every normalize, so installed
app builds and anything not yet updated keep working:

| `ladder` | `outcomes` | `routing_table_v2` |
|----------|------------|--------------------|
| `ignore` | `log` for person; `off` for every other subject | `log` |
| `silent` | `notify` | `notify` |
| `glance` | `glance` | `quiet` |
| `urgent` | `alarm` | `urgent` |

`private` column in both = the `yard` value. The legacy `routing_table`
(stranger/known/animal/thing) stays as stored, untouched, as today.

Note `silent → notify` in the legacy view: an old app shows a Quiet cell as
"Notify". That is the accepted cost; it reads back losslessly (§2.3).

### 2.3 PUT from an older client (no `ladder` in the body)

Translate the body's `outcomes` (or, if absent, `routing_table_v2`, as
today's legacy-body branch does) into `ladder`:

| old | → | `ladder` |
|-----|---|----------|
| `off`, `log` | | `ignore` |
| `notify` | | `silent` |
| `glance` (`quiet` level) | | `glance` |
| `alarm` (`urgent` level) | | `urgent` |

Columns: `street→street`, `yard`+`private→yard` (louder wins, §3.2),
`doors→doors`, `off_limits→off_limits`.

If the body has both `ladder` and legacy keys, `ladder` wins and the legacy
keys are ignored (they are re-derived).

### 2.4 Zone overrides

`zone_overrides[zone][subject]` values: `log | silent | quiet | urgent`
(engine levels, like today). Accepted on input and translated: `notify →
silent`. Still no `off`.

### 2.5 Quiet hours

`quiet_hours.mode`: `hold_urgent | mute_sounds`.
- `hold_urgent` (new): during the window, Urgent is held at Glance (`quiet`)
  except for place `off_limits` and safety triggers (§4.1 step 3). Adds the
  modifier/trace reason `quiet_hours_hold`.
- `mute_sounds`: unchanged (drops sound except on urgent).
- Input `cap_quiet` is accepted and stored as `hold_urgent`.

### 2.6 Other keys

Unchanged and preserved: recognition, live_activities (keep unknown keys,
e.g. a future `summary_row`), escalation_sound, mute_sounds, camera_* ,
zone_names, secure_area, map_scale_ft, floorplan, geometric_dedup. Sticky
keys as today.

### 2.7 Capability

`GET /v1/capabilities` → `push.ladder_version: 3` (additive). The app uses
`ladder` only when it sees this; otherwise it treats the sidecar as v2.

## 3. Migration (v2 → v3)

### 3.1 When

In `startup()`, once, when the stored file's `v` < 3 (or `ladder` is
absent). Before writing, copy the existing file to
`<settings path>.v2.bak` (do not overwrite an existing `.v2.bak`). Then
normalize and save with `v = 3`. Idempotent: a v3 file is left alone.

### 3.2 Rules

- `ladder` from stored `outcomes` via §2.3.
- `private` merges into `yard`: the louder of the two cells wins, by the
  order `ignore < silent < glance < urgent`.
- `zone_classes`: any `private` value → `yard`.
- `zone_overrides`: `notify → silent`; other values unchanged.
- `quiet_hours.mode`: `cap_quiet → hold_urgent`.
- Everything else carried over unchanged.

### 3.3 The owner's file, expected result

Stored today: person/animal/package `street: log`, everything else `glance`;
vehicle/thing/bin/opening all `off`; zone `garden` classed `private`;
overrides `back_walkway.person = quiet`, `parking_area.person = quiet`;
quiet hours 08:00–22:00 `cap_quiet`.
After migration: person/animal/package `street: ignore`, `yard/doors/
off_limits: glance`; vehicle/thing/bin/opening all `ignore`; `garden` →
`yard`; overrides unchanged (`quiet` = Glance); quiet hours `hold_urgent`.
Add this as a test fixture.

## 4. Engine (ladder evaluation)

### 4.1 Order (changes in bold)

1. muted → suppressed.
2. `source == "system"` → system card (§4.6), outside the ladder.
3. audio_safety or ai_flagged → `urgent`.
4. dangerous-animal reclass → person (unchanged).
5. zone override → returned directly (bypasses nudge, floor, caps), as today.
6. **Ignore for a non-person subject** → suppressed (today's off-cell path:
   traced as `level="off"`, `stage="off_cell"`, never escalates). Ignore for
   a person → `log`, nudgeable.
7. Table lookup (`ladder` → engine level).
8. Nudge (§4.2).
9. Child-hazard floor: a person in a child-hazard zone is raised to at least
   **`quiet` (Glance)** (today: notify).
10. Caps: `street` caps at `quiet` (unchanged); unconfirmed detector caps at
    `quiet` (unchanged).
11. **Quiet hours `hold_urgent`** (§2.5), applied after caps.

### 4.2 Nudge (decided: behaviour stops at Glance)

- One step per evaluation over `log < silent < quiet < urgent`, net worry
  +1, net calm −1, same reasons as today. Animals never nudge (unchanged);
  "known" never nudges up (unchanged).
- **A worry nudge never produces `urgent`.** Nudging up from `quiet` stays
  `quiet`. A cell already at `urgent` stays `urgent` (calm can lower it).
- Escalation over time (re-evaluations raising a story) follows the same
  ceiling: behaviour alone can take a story to Glance at most.

### 4.3 Recognition

`relax_one` drops one step; `relax_to_quiet` caps at `quiet` (Glance).

## 5. Delivery

### 5.1 Per level

| Engine level | Card push | Interruption | Sound | Live Activity | Category |
|--------------|-----------|--------------|-------|---------------|----------|
| `log`    | no  | — | — | no | — |
| `silent` | yes | `passive` | never | **no** (not a catch-all trigger) | `card.silent` |
| `quiet`  | **no**, for a device that can show a Live Activity (§5.3) | — | — | catch-all or family, as today | — |
| `urgent` | yes | `time-sensitive` | per mute / budget, repeats as today (with the muted-repeat fix) | family as today | `card.urgent` |

- `SOUNDED_LEVELS` = (`urgent`). `PUSHABLE_LEVELS` = (`silent`, `urgent`)
  plus the §5.3 fallback. Fix the existing disagreement between
  `cards.PUSHABLE_LEVELS` and `delivery.LEVEL_APNS`: one definition.
- `la_first` demotion of a card push to passive is no longer needed for
  Glance (Glance pushes no card). Keep it for an urgent story covered by a
  Live Activity only if today's behaviour does (escalations are exempt
  today; keep that).

### 5.2 Notification Center rows that stay

RESOLVE payloads: `ephemeral = not kept_row and not zone_override_hit`.
`kept_row` (a sticky `push_cards` column) turns true once any `silent` or
`urgent` card push has been delivered for the story, so a story that sent a
Quiet row and was later lifted to Glance keeps that row. The Glance fallback
(§5.3) sets a separate sticky `fallback_row`, which keeps the row on the
devices that got the fallback only. Quiet and Urgent rows are kept until the
person opens or clears them (the app removes a row when its story is opened,
unchanged).

### 5.3 Glance on a device that can't show a Live Activity

`device.can_live_activity` false (no push-to-start token, `la_capable`
false — e.g. the Mac): a `quiet` card is delivered to that device as a
`silent` card (passive, no sound, `card.silent`, kept). Trace it per device
as today's la_reason `device_not_la_capable` plus the fallback.

### 5.4 Eligibility and registration

- `_LEVEL_TO_SEVERITY`: `urgent → alert`, `silent → detection`,
  `quiet → detection`, `log → log`.
- Device registration default `min_severity` → `"detection"` (was
  `"alert"`, which silently dropped Glance for any client that omitted the
  field — the 2026-09-22 app bug).

### 5.5 Live Activities

- Catch-all starts when the table cell is `glance` and no family matches
  (unchanged); a `silent` cell never starts one.
- Families unchanged, except: the person family starts at `quiet` or
  `urgent` (today: notify or urgent), so Glance for a person uses the person
  family; `person_restricted` unchanged (any level at `off_limits`).
- Content-state `level` never carries `silent`. Relevance: urgent 1.0,
  quiet 0.5, log 0.25 (drop notify's 0.75).

### 5.6 Decision trace and history

`level` values: `off` (suppressed Ignore), `log`, `silent`, `quiet`,
`urgent`. Silence entries record the applied level. `GET /v1/push/cards`
`level`/`peak_level` likewise. `encounters/notability.py` level→outcome map
updated.

### 5.7 Silence and overrides endpoints

- `POST /v1/push/silence` applies **`silent`** (Quiet) — zone override
  `silent`, or outcome cell `silent` — and responds `applied: "silent"`.
- `PUT /v1/push/overrides`: `level` in `log | silent | quiet | urgent`
  (`notify` accepted → `silent`); `kind: outcome_cell` takes the four
  levels plus nothing else (`off` accepted → `ignore`).

## 6. System cards and legacy `notify`

System cards (test pushes, Connection Doctor round trip) keep their own
delivery: active push with sound, as today (`SYSTEM_CARD_LEVEL` stays a
separate path, not a ladder level). Any other place that still produces
`notify` (legacy input, fixtures) is translated to `silent` at the boundary.

## 7. Web settings page

`routes/settings_page.py` (read-only matrix) and `static/js/policy.js`
(editor) read and write `ladder` with four places and the four words
(Ignore · Quiet · Glance · Urgent); place labels Public · Grounds ·
Entrances · Restricted; quiet-hours mode labels "Hold Urgent at Glance" and
"Mute sounds". `zones.js` offers four classes. An unknown level must not
500 the page.

## 8. Tests and fixtures

- Unit tests for: schema validation, derivation (§2.2), legacy PUT
  translation (§2.3), migration incl. §3.3 and the `.v2.bak` copy and
  idempotence, engine order and the nudge ceiling, child-hazard floor,
  hold_urgent, delivery per level (§5.1–5.3), ephemeral rule, registration
  default, silence/overrides translation, capability flag.
- `fixtures/ladder/ladder_cases.json` and `delivery_cases.json` updated to
  the new levels (keep ids stable where the case still exists).
- Contract goldens regenerated (`CONTRACT_GOLDEN_REGEN=1`): settings GET/PUT,
  silence, decisions, capabilities, card push. The app vendors them with
  `tools/sync-contract-fixtures.sh`.
- Full suite green; ruff and mypy clean (pre-push hook).
