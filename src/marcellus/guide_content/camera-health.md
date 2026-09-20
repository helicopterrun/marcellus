---
title: Camera & detector health
section: analysis
order: 3
routes: ["/fps-budget", "/placement"]
---

## [FPS budget](/fps-budget)

Your detector (e.g. a Coral) has a fixed inference budget: how many frames
per second it can score. This page compares that budget against what your
cameras collectively demand, with color-banded utilization. If demand
approaches budget, detections start queueing and latency climbs — the fixes
are lower detect FPS, smaller detect streams, or better motion masks
(see [Motion & zones](/guide/motion-and-zones)).

## [Placement](/placement)

A calculator for planning cameras before you mount them: enter focal
length/zoom and it derives the horizontal field of view, then shows how many
pixels an object (person, face, plate) occupies at a given distance. Lens
presets cover common hardware.

Rules of thumb it encodes: identification needs far more pixels than
detection, and doubling distance quarters your pixels. Check a planned
mounting spot here before drilling holes.

Pick an already-**Deployed** camera from its selector to load a real
camera's lens, resolution, mount height, and tilt instead of starting from a
lens preset — handy for checking an existing mounting spot rather than
planning a new one. The px/distance chart's **DORI legend** (below the
chart) reads out the distance at which this setup crosses Frigate's
identify/recognise thresholds, in feet, for the currently selected object.
The **"Object target (editable)"** details block lets you override the
target object's width, aspect ratio, and target pixel width directly —
useful for sizing against something other than the built-in object presets
(a package, a license plate) without leaving the page.
