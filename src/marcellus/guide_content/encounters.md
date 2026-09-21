---
title: Encounters
section: encounters
order: 1
routes: ["/encounters", "/encounters/{encounter_id}"]
config: ["encounters"]
---

[Encounters](/encounters) groups Frigate review segments into the thing a
person would actually describe: "a raccoon worked its way from the alley to
the shed", "two people and a dog walked past the gate" -- one row instead of
three. It's an overlay: Frigate's `event` and `reviewsegment` tables stay the
source of truth and are only ever read.

Design spec: the repo's `docs/encounters.md`.

An **atom** is one Frigate review segment (one camera's already-bundled
concurrent objects). An **encounter** is an ordered chain of atoms across
cameras and time gaps. Every membership records why it joined ("shared zone
front_garden, 40s gap") and a confidence, so the detail page can explain
itself rather than just asserting a grouping.

Off by default (`enabled`). Turning it on starts two things: a live hook off
the same MQTT review stream push already subscribes to, and a periodic
reconciler that reads `reviewsegment` directly every `reconcile_interval_s`
seconds -- the reconciler is the belt: anything the live hook missed, saw
only partially, or (on a fresh install) everything from the last
`backfill_lookback_s` seconds gets picked up there.

## How linking works

A new atom joins the most confident matching open encounter, or starts a new
one:

- **Same identity** (a shared `sub_label`) joins regardless of camera --
  confidence 0.95.
- **Same camera**, **shared zone name**, or a configured **adjacent**
  camera join with decreasing confidence (0.9 / 0.8 / 0.6), as long as the
  gap since the encounter's last activity is within `gap_s` for the label
  family the new atom and the encounter actually **share** (person/vehicle/
  animal/default, or, for a label with no named family, the label itself --
  a waste bin and a garage door are separate families, not lumped into one
  shared "default") -- three times that allowance on an identity match.
- **Companionship** (0.7): no shared label family needed if the atom's time
  span overlaps the encounter's by at least `min_copresence_s` and the
  camera is the same as or adjacent to one of the encounter's
  `recent_cameras` most-recently-visited cameras -- this is how "person and
  dog together, then the dog alone next door" stays one encounter.
- An encounter's total span is capped at `max_duration_s`; past that (or
  quiet long enough) it's sealed and no longer a linking candidate.
- A **lone founder** -- an atom that started its own encounter because
  nothing matched yet -- can be re-homed into a better-matching encounter
  once a later message shows a real link (e.g. companionship once two
  atoms' spans actually overlap). This only ever moves an atom that's still
  alone in its own encounter; once it shares an encounter with another
  atom, it stays put.

Camera adjacency (`/v1/encounters/adjacency`, and the Adjacency section on
the [Encounters](/encounters) page) comes from Frigate's own zone names: two
cameras sharing a zone name are adjacent. `adjacency` adds edges that
naming misses; `not_adjacent` removes a same-named pair that isn't really
the same ground. Config always wins over the zone-derived graph. Both are
live [Settings](/settings) knobs -- editing either takes effect on the next
reconcile, no restart.

## Reading the pages

The list is the last 48 hours, newest first: camera path (`alley-wide →
shed`), label/identity chips, atom count, and an open/sealed badge. Expand a
row for its atoms -- each with camera, span, labels, zones, and the
`link_reason`/confidence that put it there. The detail page adds links to
each atom's underlying Frigate events.

## Correcting encounters

Each member row on the detail page has three actions, and the page itself
has a fourth:

- **Split out** -- pulls one atom into a brand new encounter of its own. Use
  this when the linker grouped something in that doesn't belong.
- **Move to** -- pins one atom into a specific encounter (by id, typed into
  the field next to the button). Works even if the target is already
  sealed.
- **Undo decisions** -- appears once an atom has any recorded decisions;
  clears them so future automatic linking is no longer biased toward or
  away from a particular encounter (it doesn't move the atom itself).
- **Merge another encounter into this one** -- a page-level form; folds
  every atom from another encounter (by id) into the one you're viewing.

A split or a pinned move is sticky: once you've made it, the linker never
automatically moves that atom again, even across reconcile cycles or a
sealed donor/target. The same actions are available as `/v1/encounters/...`
JSON routes for scripting.

## Retention

Sealed encounters (and their members and decisions) older than
`retention_days` (default 30) are dropped by an hourly prune folded into the
reconciler; unsealed encounters are never pruned regardless of age. Run it
by hand with `marcellus encounters prune`.

## Observations and topology

Each member also carries a best-effort direction guess -- see
[Observations](/guide/observations) for the `first_zone`/`last_zone`/
`direction`/`heading_deg`/`dir_source` fields and the `/v1/observations`
read API that surfaces atoms directly instead of grouped by encounter.

[Camera topology](/guide/topology) covers the learned per-camera-pair
transition times, the `/v1/topology` and `/v1/cameras/{camera}/neighbours`
read APIs, the multi-lane `GET /v1/timeline`, and suggested continuations.

## Configuration

Every knob lives under `encounters:` in `sidecar.yml`. "Live" knobs can be
edited from [Settings](/settings) and take effect on the next reconcile
cycle, with no restart; the others need a restart.

| Field | Default | Live | Effect |
|---|---|---|---|
| `enabled` | `false` | no | Master switch: default off, and with it off nothing links and no reconciler runs. |
| `reconcile_interval_s` | `30.0` | yes | Seconds between reconciler cycles, the belt that catches whatever the live MQTT hook missed. |
| `backfill_lookback_s` | `86400.0` | yes | How far back over `reviewsegment` the very first cycle reaches when no watermark exists yet. |
| `gap_s` | `{animal: 180, person: 90, vehicle: 45, default: 60}` | yes | Max seconds between an encounter's last activity and a new atom, per label family (identity matches get 3x). |
| `max_duration_s` | `1800.0` | yes | Hard cap on one encounter's total span; past it the encounter seals and stops accepting atoms. |
| `recent_cameras` | `2` | yes | How many recently-visited distinct cameras count as "nearby" for the adjacency and companionship checks. |
| `min_copresence_s` | `3.0` | yes | Minimum span overlap for two atoms to count as companions with no shared label family. |
| `adjacency` | `[]` | yes | Extra camera-pair edges (`[[a, b], ...]`) beyond what shared zone names already imply. |
| `not_adjacent` | `[]` | yes | Camera-pair edges to remove despite a shared zone name; config always beats the zone-derived graph. |
| `retention_days` | `30` | no | Age at which the hourly prune drops sealed encounters; unsealed ones are never pruned. |
| `transitions_enabled` | `false` | no | Whether the learner writes per-camera-pair transition times into `camera_transitions`. |
| `transition_min_samples` | `8` | yes | Samples an edge needs before its own percentiles are trusted (`learned`) rather than defaulted. |
| `transition_max_sample_s` | `180.0` | yes | Discard a transition sample with a bigger gap than this, so one slow crossing can't skew percentiles. |
| `transition_learn_interval_s` | `3600.0` | yes | Minimum seconds between learning scans, which are a full read over `encounter_members`. |
| `transition_learn_window_days` | `14.0` | yes | How many days back the learning scan looks for transition samples. |
| `transition_default_s` | `{p10: 2, p50: 15, p90: 60}` | yes | Fallback percentiles written for an edge with too few samples to learn from. |
| `transition_overrides` | `{}` | yes | Manual per-transition percentiles keyed `"camA>camB"` or `"camA>camB:family"`, written as `source='config'`. |
| `use_learned_gaps` | `false` | yes | Whether the linker's "adjacent" reason actually reads learned stats instead of the flat `gap_s`. |
| `transition_slack` | `1.5` | yes | Multiplier on a learned p90 when `use_learned_gaps` is on, allowing slack beyond the observed 90th percentile. |
| `timeline_max_window_s` | `21600.0` | no | Hard cap (default 6h) on the window one `GET /v1/timeline` request may ask for. |
| `continuation_w_topo` | `0.30` | yes | Continuation scoring weight for whether a real or learned edge connects the two cameras. |
| `continuation_w_time` | `0.30` | yes | Continuation scoring weight for how well the elapsed gap fits learned transition stats. |
| `continuation_w_direction` | `0.20` | yes | Continuation scoring weight for the source's exit-zone direction matching the candidate camera. |
| `continuation_w_class` | `0.20` | yes | Continuation scoring weight for same label vs. merely the same label family. |
| `continuation_min_score` | `0.25` | yes | Below this score a continuation candidate is dropped instead of suggested. |
| `continuation_likely_score` | `0.5` | yes | At or above this score a suggestion is bucketed `likely` rather than `possible`. |

The continuation weights and thresholds are explained in context under
[Camera topology](/guide/topology) "Suggested continuations".

## If it goes wrong

- Nothing appears: check `enabled`, and that `/healthz` reports an
  `encounters` state other than `disabled` (see
  [Troubleshooting](/guide/troubleshooting)).
- Rows linked before a write guard landed can carry a bad `start_time` or a
  missing `end_time`. `fsc encounters repair --dry-run` reports how many;
  drop the flag to fix them (see the [CLI reference](/guide/cli)).
- Old encounters piling up: run the prune by hand with
  `fsc encounters prune`.
