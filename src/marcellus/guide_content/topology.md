---
title: Camera topology
section: encounters
order: 3
routes: ["/v1/topology", "/v1/cameras/{camera}/neighbours", "/v1/observations/{atom_id}/continuations", "/v1/timeline"]
config: ["encounters"]
---

[Encounters](/guide/encounters)' adjacency graph tells you *which* cameras
hand off to each other; camera topology learns *how long* that handoff
usually takes, per directed camera pair and label family.

Design spec: the repo's `docs/encounters.md`, section "Camera topology".

## Using it

Turn `transitions_enabled` on (off by default) and a periodic scan folded
into the reconciler builds the table. Consecutive atoms on the same
encounter, on different but adjacent cameras, sharing a label family, with
no human split decision on either atom, become one sample. The scan is
throttled to at most every `transition_learn_interval_s` and looks back
`transition_learn_window_days`; it writes one row per directed edge x
family into `camera_transitions`:

- **learned** -- enough samples (`transition_min_samples` or more, after a
  MAD outlier trim) to trust the observed p10/p50/p90 gap.
- **config** -- a manual `transition_overrides` entry (keyed `"camA>camB"`
  or the more specific `"camA>camB:family"`) always wins over learned stats.
- **default** -- too few samples: `transition_default_s` is written
  instead, with the true (sub-threshold) sample count recorded.

A sample whose gap exceeds `transition_max_sample_s` is discarded outright
rather than skewing the percentiles.

Turn `use_learned_gaps` on (live -- no restart, from [Settings](/settings))
to have the linker's "adjacent" reason actually use these stats: for a
directed camera-pair/family edge with enough samples (`source ==
"learned"`), the allowed gap becomes that edge's p90 times
`transition_slack` (also live) instead of the flat `gap_s[family]` --
narrower or wider, whichever the observed handoff times say. A gap inside
`[p10, p90]` links with confidence 0.65, outside it (but still within the
slack-widened allowance) 0.55 -- both below same-camera/shared-zone/
companion, so those still win when they also match. Pairs with only a
"config" or "default" row keep the flat `gap_s` allowance regardless.
Same-camera and shared-zone matches never use learned stats -- they're
"still basically where the encounter already is," not a handoff.

## Topology API

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
request, and a window no longer than `timeline_max_window_s`.

## Suggested continuations

`GET /v1/observations/{atom_id}/continuations?limit=5` answers "where might
this subject go next": for each neighbour camera (adjacency edge or a
learned-only topology edge) and each label family the source observation
shares, it searches for a candidate observation already on that camera from
the source's own start time through p90 (times a max-window factor) past
the source's end -- wide enough to catch an overlapping hand-off where the
next camera already sees the entity while the source is still seeing it
too, not just one that starts after the source ends -- and scores whatever
it finds. If nothing turns up, it falls back to the narrower predicted
window (from the p10/p90 stats, or a default when nothing's been learned
yet) and returns the bare prediction itself instead.

**Machine predictions are never shown as certain.** A suggestion is bucketed
`confirmed` ONLY when the candidate observation is already linked into the
same encounter as the source (the linker already joined them) or carries a
human pin decision to that encounter -- never from score alone, no matter
how high. Everything else buckets `likely` (score at or above
`continuation_likely_score`), `possible` (score at or above
`continuation_min_score`), or is dropped. The numeric `score` is still
returned on every suggestion for the app/debugging even when the bucket is
conservative about it.

The score blends four weighted factors -- topology (is this even a real or
learned edge), elapsed time against learned transition stats, exit-zone
direction match, and same-label vs. same-family:

| Field | Default | What it weights |
|---|---|---|
| `continuation_w_topo` | `0.30` | Whether a real adjacency or learned edge connects the two cameras at all. |
| `continuation_w_time` | `0.30` | How well the elapsed gap fits the learned transition stats for that edge. |
| `continuation_w_direction` | `0.20` | Whether the source's exit zone/heading points at the candidate camera. |
| `continuation_w_class` | `0.20` | Same label (strongest) vs. merely the same label family. |
| `continuation_min_score` | `0.25` | Below this a candidate is dropped rather than suggested. |
| `continuation_likely_score` | `0.5` | At or above this a suggestion is bucketed `likely` rather than `possible`. |

Weights need not sum to 1 -- scoring normalises over whichever factors it
actually used for a given candidate. All six are live-tunable from
[Settings](/settings).

A candidate with no observation yet in the window is returned as a
prediction (`observation_id`/`encounter_id`/`start` all `null`) carrying the
predicted `window` instead, so the app can show "expect ~shed in 10-30s"
before anything has actually happened.

Every suggestion also carries `timing`: `{"source": "learned"|"config"|
"default", "samples": <int>, "p50": <float|null>}`, mirroring which
`camera_transitions` row (if any) produced the `window`. The app must not
word a suggestion's gap as a typical or learned time unless
`timing.source == "learned"` -- `config` and `default` windows are still
useful ranges to show, just not ones backed by observed history.
