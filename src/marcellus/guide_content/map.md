---
title: Map & calibration
section: sidecar
order: 4
routes: ["/map"]
---

The [Map](/map) is a CAD-style top-down layout of your property: place
cameras, aim them, draw the secure area, and optionally trace over an
uploaded floorplan image. A calibrated map is what powers spatially-aware
features like approach direction in push notifications.

## Using it

- **Place & aim** — drag a camera onto the canvas, rotate its field-of-view
  wedge to match reality. Lens presets prefill the horizontal FOV.
- **Scale** — calibrate the map's scale by marking a known distance.
- **Optics** — the landmark solver refines a camera's position/heading from
  point correspondences: mark a spot in the camera image and the same spot
  on the map, a few times, and let auto-tune solve the rest.
- **Secure area** — draw the boundary that counts as "on the property".

## Walkthrough: calibrate a camera

```walkthrough
- Open the Map page and add your camera if it isn't placed yet
- Drag it to its true mounting spot and roughly aim the wedge
- Set the map scale using a known distance (fence line, driveway width)
- Open the camera's landmark calibration and mark 3-4 image↔map point pairs
- Run the solve and check the wedge now matches what the camera really sees
- Save, then verify the camera's rig facts on the Settings page
```

## If it goes wrong

A solve that lands the camera somewhere absurd almost always means one
landmark pair is mismatched — delete the outlier pair and re-solve.
Calibration state per camera is summarized on [Settings](/settings), which
deep-links back into the map's landmark editor.
