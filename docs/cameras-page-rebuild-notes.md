---
title: "Map page (formerly `/cameras`) — rebuild notes"
banner:
  content: "Design note: written alongside the code and may lag behind it. The <a href=\"/marcellus/guide/welcome/\">guide</a> describes current behavior."
---

Observations gathered before the from-scratch rebuild (2026-08-17). The page
now lives at `/map` (`/cameras` remains an alias — `routes/map_page.py`). The page
grew feature-by-feature (map, layers, calibration, lock, visibility, rotation,
fullscreen…) and the seams show. Functionality worth keeping is listed at the
end.

## Visual / layout problems

1. **Three near-identical pill rows.** Layer tabs (`Cameras/Areas/Calibrate/View`),
   the camera visibility chips, and the landmark-calibrate camera pills all
   render as similar rounded chips stacked above/below the map. Nothing says
   which row does what; the visibility chips (strikethrough = hidden) read as
   navigation.
2. **The map dominates; the controls sprawl.** The map is a fixed huge block and
   every control lives in loose `help subsection` rows *below* it — checkboxes,
   selects, buttons and numeric inputs interleaved with prose. Using any control
   means scrolling away from the map you're controlling.
3. **Default view is not fitted.** On load the floorplan is often over-zoomed
   (screenshot: a corner of the plan fills the whole viewport). There's no
   obvious "fit to plan" affordance — Reset view is a hidden button far below.
4. **Layer-group leakage / weak coupling.** Controls belonging to one layer are
   visible while another tab is active (e.g. View-layer toggles showing under
   the Calibrate tab). The `data-layer` show/hide is fragile and the user can't
   tell which controls act on the current layer.
5. **Low-contrast overlays on light floorplans.** Coverage pies are pale orange
   on a near-white plan — washed out, and overlapping pies become an unreadable
   wash. Secure-area dashed border and zone overlays are similarly faint.
6. **Detail panel is a wall of tiny numeric inputs** (azimuth/fov/hfov/mount/
   tilt/x/y) in one inline row, appearing/disappearing below the map on
   selection. No visual link to the selected camera on the map.
7. **Prose overload.** Two long intro paragraphs plus per-section explanations
   push the actual tool below the fold; important hints (keyboard nudges) are
   buried in them.
8. **Save is at the very bottom**, disconnected from the edits, with dirty
   state shown only as small text; easy to leave the page with unsaved work.
9. **Fullscreen ⛶ button** floats over the map with no companion controls —
   fullscreen mode hides all the panels you actually need while editing.
10. **Mobile:** the below-the-map control rows wrap into a long stack; the
    tap targets (chips, numeric steppers) are small; landmark snapshot flow is
    cramped.

## Structural problems (code)

- `cameras.js` is ~2300 lines of ES5 with one giant `renderMap()` that rebuilds
  the whole SVG on every frame — full rerender per pointer-move forced the
  window-listener workarounds for iOS drag bugs.
- Layer switching, selection state, dirty state, and overlay toggles are all
  ad-hoc globals; there is no single view-state object.
- The template mixes editor (layout/calibration) and observer (live, trails,
  heatmap) concerns in one page.

## What must survive the rebuild

- Unit-coordinate geometry model (north-up, `rev`-guarded settings PUT,
  server-side validate/normalize) — the data model is fine; the *presentation*
  is the problem.
- Per-camera lock, floorplan rotation, visibility chips (as features).
- Landmark calibration flow, scale calibration, measure tool, autotune,
  neighbor suggestions.
- Window-scoped drag pattern (iOS Safari drops captured pointer streams when
  the touched element is rerendered away).
- Overlays: coverage, footprints, zones, live fused objects, walks, heatmap,
  rings/grid.

## Rebuild direction (sketch)

- Editor-style layout: map as the persistent center stage, a compact side
  panel (or bottom sheet on mobile) that swaps content per mode — Place,
  Aim/Detail, Areas, Calibrate, View.
- One mode = one set of visible tools; no orphaned control rows.
- Fit-to-plan on load and a always-visible fit button next to zoom controls.
- Higher-contrast overlay palette computed against floorplan luminance (or a
  dim-floorplan slider).
- Sticky save bar that appears when dirty.
- Incremental SVG updates (patch positions) instead of full rebuild per frame.
