---
title: Observations
section: sidecar
order: 9
routes: ["/v1/topology", "/v1/cameras/{camera}/neighbours", "/v1/observations/{atom_id}/continuations"]
config: [
  "encounters.continuation_w_topo",
  "encounters.continuation_w_time",
  "encounters.continuation_w_direction",
  "encounters.continuation_w_class",
  "encounters.continuation_min_score",
  "encounters.continuation_likely_score",
]
---

**Observations** (docs/encounters.md "Observations") is a read-only view
over [Encounters](/encounters)' membership table: instead of atoms grouped
under an encounter, `/v1/observations` hands back one row per atom directly
-- the unit a script or an alert investigation actually wants when it's
starting from "what happened on this camera in this window" rather than
"walk me through this whole visit."

## Direction fields

Each observation carries a best-effort read of which way the subject moved,
derived once at link time from the atom's underlying Frigate event rows and
never recomputed unless the atom itself changes:

- `first_zone` / `last_zone` -- the first and last named zone the track
  entered, from Frigate's own zone order.
- `direction` -- `out:<zone>` when the zone trail crosses a boundary,
  otherwise a coarse compass bucket (`l2r`, `r2l`, `toward`, `away`) from a
  path or bounding-box heading, or `''` when nothing usable was found.
- `heading_deg` -- a numeric heading (0-360) when a path or box fit produced
  one; `null` for a zone-only or empty verdict.
- `dir_source` -- which tier won: `zones`, `path`, `box`, or `''`.

Zones win when they're available (cheapest, most reliable); path beats box
because a box's centroid is a much coarser motion signal. None of the three
raise on bad input -- a malformed or missing Frigate row just leaves the
observation's direction empty, same as `''`/`null`/`''`.

Existing membership rows written before this landed default to
`dir_source=''` and can be backfilled with
`marcellus encounters backfill-direction --limit N`, which rewalks rows in
that state and recomputes them against Frigate.

## API

- `GET /v1/observations?start=&end=&cameras=&labels=&limit=` -- observations
  whose span overlaps `[start, end]` (default: the last 24 hours), newest
  first, optionally filtered to a comma list of `cameras` and/or `labels`
  (any-match). `limit` caps at 500.
- `GET /v1/observations/{atom_id}` -- one observation plus its parent
  encounter summary and `neighbours.prev`/`neighbours.next` -- the sibling
  atoms immediately before/after it in the same encounter, or `null` at
  either end.

Both are read-only and add no new linking behaviour -- they're a different
lens on the same `encounter_members` rows [Encounters](/encounters) already
writes.

## Camera topology

[Encounters](/guide/encounters)' adjacency graph tells you *which* cameras
hand off to each other; camera topology (`encounters.transitions_enabled`,
off by default) learns *how long* that handoff usually takes, per directed
camera pair and label family, from linked encounters -- consecutive atoms
on the same encounter, different but adjacent cameras, sharing a label
family, with no human split decision on either atom become one sample. A
periodic scan folded into the reconciler (throttled to at most every
`transition_learn_interval_s`, looking back `transition_learn_window_days`)
writes one row per directed edge x family into `camera_transitions`:

- **learned** -- enough samples (`transition_min_samples` or more, after a
  MAD outlier trim) to trust the observed p10/p50/p90 gap.
- **config** -- a manual `transition_overrides` entry (keyed `"camA>camB"`
  or the more specific `"camA>camB:family"`) always wins over learned stats.
- **default** -- too few samples: `transition_default_s` is written
  instead, with the true (sub-threshold) sample count recorded.

A sample whose gap exceeds `transition_max_sample_s` is discarded outright
rather than skewing the percentiles.

Turn `encounters.use_learned_gaps` on (live -- no restart, from
[Settings](/settings)) to have the linker's "adjacent" reason actually use
these stats: for a directed camera-pair/family edge with enough samples
(`source == "learned"`), the allowed gap becomes that edge's p90 times
`encounters.transition_slack` (also live) instead of the flat
`encounters.gap_s[family]` -- narrower or wider, whichever the observed
handoff times say. A gap inside `[p10, p90]` links with confidence 0.65,
outside it (but still within the slack-widened allowance) 0.55 -- both
below same-camera/shared-zone/companion, so those still win when they also
match. Pairs with only a "config" or "default" row (too few samples) keep
the flat `gap_s` allowance regardless. Same-camera and shared-zone matches
never use learned stats, only the flat allowance -- they're "still
basically where the encounter already is," not a handoff.

## API

- `GET /v1/topology` -- the adjacency graph (same edges as
  `/v1/encounters/adjacency`) with each edge's `transitions` in both
  directions, per family.
- `GET /v1/cameras/{camera}/neighbours` -- one camera's directed edges and
  their transition stats; 404 if the camera has no zones and no adjacency
  edges.

## Global timeline

`GET /v1/timeline` composes multiple cameras' reels into one call, for
"what happened across the property in this window" instead of one camera at
a time:

- `start`/`end` (epoch seconds) and `cameras` (comma list) are required,
  unless `encounter=<id>` is given -- then the window is that encounter's
  `[start_time, end_time or now]` padded by `pad_s` (default 60s) on each
  side, and `cameras` defaults to the encounter's own camera list (an
  explicit `cameras` still overrides it).
- `motion_scale` has the same meaning as `/v1/reel`'s.
- Each `lanes[]` entry is exactly what `/v1/reel/{camera}` would return for
  the same window, plus `camera` and `observations` (the encounter-linked
  atoms overlapping the window on that camera: `id`, `start`, `end`,
  `encounter_id`, `labels`, `direction`, `severity`).
- `encounters[]` lists the distinct encounter summaries those observations
  belong to, ordered by start.
- `truncated` is `true` when the observation overlay hit its internal cap
  (2000 rows) -- the lanes themselves are never truncated.

Guards, all `400` with a machine-readable `error`/`message` body (`422` for
missing `start`/`end`/`cameras` outside the `encounter=` form, `404` for an
unknown camera or encounter, mirroring `/v1/reel`): at most 12 cameras per
request, and a window no longer than `timeline_max_window_s` (see
[Encounters](/guide/encounters) "Global timeline").

## Suggested continuations

`GET /v1/observations/{atom_id}/continuations?limit=5` (M5) answers "where
might this subject go next": for each neighbour camera (adjacency edge or
camera topology's learned-only edge) and each label family the source
observation shares, it predicts a time window from camera topology's
p10/p90 (or a default window when nothing's been learned yet), looks for a
candidate observation already in that window, and scores whatever it finds
-- a real candidate or, if none exists yet, the bare prediction itself.

**Machine predictions are never shown as certain.** A suggestion is bucketed
`confirmed` ONLY when the candidate observation is already linked into the
same encounter as the source (the linker already joined them) or carries a
human pin decision to that encounter -- never from score alone, no matter
how high. Everything else buckets `likely` (score >=
`continuation_likely_score`), `possible` (score >= `continuation_min_score`),
or is dropped. The numeric `score` is still returned on every suggestion for
the app/debugging even when the bucket is conservative about it.

The score blends four weighted factors (`continuation_w_topo`,
`continuation_w_time`, `continuation_w_direction`, `continuation_w_class`):
topology (is this even a real or learned edge), elapsed time against learned
transition stats, exit-zone direction match, and same-label vs. same-family.
A candidate with no observation yet in the window is returned as a
prediction (`observation_id`/`encounter_id`/`start` all `null`) carrying the
predicted `window` instead, so the app can show "expect ~shed in 10-30s"
before anything has actually happened.
