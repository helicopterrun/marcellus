---
title: Observations
section: sidecar
order: 9
routes: ["/v1/topology", "/v1/cameras/{camera}/neighbours"]
config: []
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
rather than skewing the percentiles. `use_learned_gaps` and
`transition_slack` are reserved for a future linker change that reads these
stats back into its own gap allowance -- they have no effect yet.

## API

- `GET /v1/topology` -- the adjacency graph (same edges as
  `/v1/encounters/adjacency`) with each edge's `transitions` in both
  directions, per family.
- `GET /v1/cameras/{camera}/neighbours` -- one camera's directed edges and
  their transition stats; 404 if the camera has no zones and no adjacency
  edges.
