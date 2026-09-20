---
title: Motion & zone analysis
section: analysis
order: 2
routes: ["/motion", "/zone-hits"]
---

Two pages for understanding what your cameras are reacting to:

## [Motion](/motion)

A form, not a set of query-string presets: pick a date range (today /
yesterday / a date / a date range) and hit compute, or use one of the 4
preset buttons for the common cases. Leaving the baseline field blank gives
a single-window view — one row per camera, classified by activity level, to
spot noisy cameras (wind-blown foliage, flags, shadows) burning detector
capacity. Filling in a baseline range switches to A/B compare mode: it
classifies each **camera**, not each hour, against the baseline — noise
spike, real activity spike, quiet drop, or flat. That's the tool for
answering "did my motion mask change actually help?"

## [Zone hits](/zone-hits)

Counts how often each zone is entered, per camera, over a window
(`?days=30`). It also surfaces **mask candidates** — regions with lots of
motion but never a meaningful object, which are usually safe to mask out in
Frigate. Fewer wasted motion regions means the detector spends its budget
where it matters (see [Camera health](/guide/camera-health)).
