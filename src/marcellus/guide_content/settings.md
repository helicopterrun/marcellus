---
title: Settings
section: sidecar
order: 5
routes: ["/settings"]
config: ["watchdog", "proxy"]
---

[Settings](/settings) mirrors the Elsinore app's settings screen and gathers
the sidecar's own knobs in one place.

## Using it

- **Zones** (`/settings#zones`) — the zone routing policy that decides which
  zones matter for alerts, zone neighbor relationships, and export/import of
  the whole policy as JSON.
- **Push devices** (`/settings#push`) — every registered phone, with per-
  device **Test** buttons, plus the live attention-ladder table and example
  notifications rendered by the real pipeline. Currently
  **{{stat:push_devices}}** device(s) are registered. See
  [Push notifications](/guide/push-notifications).
- **Cameras** — per-camera rig facts (position, heading, FOV, calibration
  quality) with deep links into the [Map](/map) landmark editor.
- **Event alignment** (`/settings#alignment`) — fixes event bars sitting a few
  seconds away from their pictures on the scrub timeline. Frigate stamps
  events from the *detect* stream and recordings from the *record* stream —
  two separate camera connections whose clocks drift apart per camera. Press
  **Measure cameras** (it compares recent event thumbnails against the
  recordings; expect a few minutes) and **Apply** the suggested offset for
  each camera with decent confidence. For a precise manual fix — or when the
  measurement comes back low-confidence — press **Calibrate…** on a camera:
  it opens on the recent event that moved farthest across the frame (the
  clearest visual anchor; the picker sorts by movement) with the event
  snapshot and the recording shown side by side. Click the filmstrip frame
  where the scene matches the snapshot, fine-tune with the nudges or arrow
  keys (they follow the step toggle, down to 50 ms — about one recording
  frame; Shift for 1 s), and hold **compare** to blink the two images over
  each other until they align. For confidence, press **Add sample** and
  repeat on two or three events — the save takes the mean, and the sample
  count and spread are shown as you go. Applied offsets take effect
  on the next timeline refresh, in the sidecar's scrub page and the Elsinore
  reel alike.
  Setting `detect.annotation_offset` (milliseconds) in Frigate's own
  `config.yml` does the same job, also fixes Frigate's own annotation overlay,
  and wins over anything applied here — and for cameras already pinned that
  way, the calibrator's save writes the new value straight into Frigate's
  config, so the two sources never disagree. Config saves take effect on
  Frigate's next start: calibrate as many cameras as you like, then press
  **Restart Frigate to apply** once (~30 s of blind cameras). Worth doing once at setup and again if
  a camera is replaced or its streams reconfigured.
- **Faces** — a read-only view of the face pipeline configuration.
- **Help** — a link to this guide; on phones this is the guide's front door.
- **About** — version and debug links.

## Configuration

- `watchdog:` — an optional external health watchdog that can restart the
  Frigate container when it wedges. It runs as its own systemd unit and is
  off by default; the sidecar itself never restarts anything.
  - `enabled` — master on/off switch (default off).
  - `probe_path` — HTTP path probed against `frigate.base_url` to check
    liveness (default `/api/version`).
  - `interval_s` / `timeout_s` — how often it probes, and the per-probe
    timeout (default 30s / 10s).
  - `failures_before_restart` — consecutive failed probes before a restart
    is triggered (default 4).
  - `restart_command` — shell command run to restart the Frigate container
    (default `["docker", "restart", "frigate"]`).
  - `restart_timeout_s` — timeout for the restart command itself (default
    120s).
  - `cooldown_s` — after a restart, ignore failures for this long so
    Frigate's own boot can't trigger a second restart (default 180s).
  - `max_restarts_per_hour` — safety cap before the watchdog gives up and
    just logs (default 3).
- `proxy:` — behavior of the reverse proxy through which your browser
  reaches Frigate's API (timeouts, header handling). Defaults are fine for
  almost everyone.
  - `enabled` — master on/off switch for the proxy (default on).
  - `pass_request_headers` — request headers forwarded upstream (default
    `["range", "authorization", "cookie"]`).
