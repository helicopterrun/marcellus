---
title: Encounters
section: sidecar
order: 8
routes: ["/encounters", "/encounters/{encounter_id}"]
config: ["encounters"]
---

[Encounters](/encounters) groups Frigate review segments into the thing a
person would actually describe: "a raccoon worked its way from the alley to
the shed", "two people and a dog walked past the gate" — one row instead of
three. It's an overlay: Frigate's `event` and `reviewsegment` tables stay the
source of truth and are only ever read.

An **atom** is one Frigate review segment (one camera's already-bundled
concurrent objects). An **encounter** is an ordered chain of atoms across
cameras and time gaps. Every membership records why it joined ("shared zone
front_garden, 40s gap") and a confidence, so the detail page can explain
itself rather than just asserting a grouping.

Off by default (`enabled`). Turning it on starts two things: a live hook off
the same MQTT review stream push already subscribes to, and a periodic
reconciler that reads `reviewsegment` directly every `reconcile_interval_s`
seconds — the reconciler is the belt: anything the live hook missed, saw
only partially, or (on a fresh install) everything from the last
`backfill_lookback_s` seconds gets picked up there.

## How linking works

A new atom joins the most confident matching open encounter, or starts a new
one:

- **Same identity** (a shared `sub_label`) joins regardless of camera —
  confidence 0.95.
- **Same camera**, **shared zone name**, or a configured **adjacent**
  camera join with decreasing confidence (0.9 / 0.8 / 0.6), as long as the
  gap since the encounter's last activity is within `gap_s` for the new
  atom's label family (person/vehicle/animal/default) — three times that
  allowance on an identity match.
- **Companionship** (0.7): no shared label family needed if the atom's time
  span overlaps the encounter's by at least `min_copresence_s` and the
  camera is the same as or adjacent to one of the encounter's
  `recent_cameras` most-recently-visited cameras — this is how "person and
  dog together, then the dog alone next door" stays one encounter.
- An encounter's total span is capped at `max_duration_s`; past that (or
  quiet long enough) it's sealed and no longer a linking candidate.
- A **lone founder** — an atom that started its own encounter because
  nothing matched yet — can be re-homed into a better-matching encounter
  once a later message shows a real link (e.g. companionship once two
  atoms' spans actually overlap). This only ever moves an atom that's still
  alone in its own encounter; once it shares an encounter with another
  atom, it stays put.

Camera adjacency (`/v1/encounters/adjacency`) comes from Frigate's own zone
names: two cameras sharing a zone name are adjacent. `adjacency` adds edges
that naming misses; `not_adjacent` removes a same-named pair that isn't
really the same ground. Config always wins over the zone-derived graph.

## Reading the pages

The list is the last 48 hours, newest first: camera path (`alley-wide →
shed`), label/identity chips, atom count, and an open/sealed badge. Expand a
row for its atoms — each with camera, span, labels, zones, and the
`link_reason`/confidence that put it there. The detail page adds links to
each atom's underlying Frigate events.

There's no pin/split UI yet — a human overriding the linker for one atom is
a later slice.
