---
title: Observations
section: encounters
order: 2
routes: ["/v1/observations", "/v1/observations/{atom_id}"]
config: []
---

**Observations** is a read-only view over [Encounters](/encounters)'
membership table: instead of atoms grouped under an encounter,
`/v1/observations` hands back one row per atom directly -- the unit a script
or an alert investigation actually wants when it's starting from "what
happened on this camera in this window" rather than "walk me through this
whole visit."

Design spec: the repo's `docs/encounters.md`, section "Observations".

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
- `dir_source` -- which tier won: `zones`, `path`, `box`, `none`, or `''`.
  `none` means direction derivation actually ran against Frigate's event
  rows and found nothing usable in any of the three tiers -- attempted and
  empty, done. `''` means it hasn't been attempted yet (a row written before
  M1) or the attempt couldn't even run (Frigate's DB was unreachable, or no
  matching event rows were found) -- still eligible for backfill.

Zones win when they're available (cheapest, most reliable); path beats box
because a box's centroid is a much coarser motion signal. None of the three
raise on bad input -- a malformed or missing Frigate row just leaves the
observation's direction empty, same as `''`/`null`/`''`.

## The observations API

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

## If it goes wrong

Membership rows written before direction landed default to `dir_source=''`
and stay empty until backfilled. Run
`fsc encounters backfill-direction --limit N` (see the
[CLI reference](/guide/cli)), which rewalks exactly those rows and
recomputes them against Frigate. Rows whose Frigate DB was unreachable at
link time are repaired the same way -- as long as Frigate is reachable on
the rerun. A row where Frigate answered but nothing was derivable gets
`dir_source="none"` instead and is not rewalked again: the CLI's `none`
count in its JSON summary is how many rows this run marked that way, versus
`updated` (all rows touched) and `scanned` (all rows read).
