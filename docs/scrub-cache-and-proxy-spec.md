# Marcellus: scrub-cache + Frigate proxy — implementation spec

**Status:** design spec, ready to build against. Written 2026-07-30, revised 2026-07-30 after client-side review (PR #3) and Phase 1 measurements against the live box.
**Audience:** whoever implements the sidecar side of Elsinore's reel.
**Companions (client side, in the Elsinore repo):**
- `writing/sidecar-requirements-2026-07-29.md` — what the reel wants, and the measurement behind each ask. This spec is the server-side answer to it.
- `writing/sidecar-scrub-cache-contract.md` — the earlier endpoint-shape draft (superseded here where they differ).
- `writing/frigate-api-verification-2026-07-29.md` — the live 0.17.2 measurements every number below rests on.

This document turns those requirements into something buildable inside *this* repo, using the conventions already here (FastAPI router registration in `server.py`, pydantic-settings config, the read-only `frigate.db` attach in `db.py`, and the ffmpeg + streaming-proxy patterns already in `routes/wildlife.py`).

---

## 1. What we're building

Two features, one origin.

1. **A scrub-preview cache** that serves **uniform-cadence sprite sheets** — one row-major montage of 320×180 frames at a *declared, fixed* interval (1 fps recent, thinning with age). This is the thing bare Frigate structurally cannot give: its preview cache is current-hour-only, motion-driven, irregular, and emptied at the top of every hour. We own retention, resolution, cadence, and — critically — we make the cadence *predictable*.

2. **A transparent reverse proxy** in front of Frigate, so Elsinore holds **one base URL**. Everything the sidecar doesn't handle itself (`/api/*`, `/vod/*`, `/live/*`, `/preview/*`) is streamed through to Frigate unchanged, with `Range` and auth cookies passed through. The scrub cache and proxied Frigate share an origin and an auth story.

Plus a small set of `/v1` read endpoints that collapse the reel's per-window fan-out (coverage, motion, events) into one call and answer questions bare Frigate answers ambiguously or not at all.

### The one principle everything else serves

> **Predictability over density.** 0.5 fps uniform beats 1 fps irregular. The single largest source of complexity in the reel today is that Frigate's frame cadence is bimodal and lands on no boundary. A declared interval turns frame-time into arithmetic (`cell = round((t − start) / interval)`) and deletes a stack of client code: the snap grid, the frame ticks, the 31-second nearest-frame tolerance, and the staleness badge.
>
> **This win is scoped to the continuous-retention window (~4 days) and opted-in cameras — not everywhere, always.** Past the continuous window, on cameras not opted in, and against any deployment without the sidecar, the client still runs the full bare-Frigate path with all four of those mechanisms. Client-side review confirmed both paths are permanent, not a migration. Design and document accordingly: this deletes client complexity *conditionally*, which is a smaller win than originally framed but still a real one.

### Non-negotiables (inherited from Elsinore PROJECT_PLAN §6 — not re-litigated)

- **One base URL.** The sidecar fronts Frigate; the app holds a single origin.
- **Optional always.** Every `/v1` endpoint degrades to the bare-Frigate path when `/v1/capabilities` finds nothing. The reel must stay usable against plain Frigate forever.
- **Versioned from endpoint one.** Every new path is under `/v1/`.
- **Honest data.** Frames are re-sampled from real recordings. No interpolation, no invented frames, no upsampling. If a moment has no picture, the response says so — the client now has a first-class state for that.

---

## 2. Architecture at a glance

```
                         ┌─────────────────────────────────────────────┐
   Elsinore (iOS) ──────▶│  Marcellus        (single origin, :5001)     │
   one base URL          │                                              │
                         │  /v1/scrub/…    ── sprite cache (disk)  ◀──┐  │
                         │  /v1/coverage/… ── reads frigate.db RO    │  │
                         │  /v1/reel/…     ── coverage+motion+events │  │
                         │  /v1/motion/…   ── total, zero-filled     │  │
                         │  /v1/capabilities                         │  │
                         │  /v1/highlights/… (Tier 1)                │  │
                         │                                           │  │
                         │  everything else ── reverse proxy ────────┼──┼──▶ Frigate :8971
                         │  (/api,/vod,/live,/preview)   Range+cookie │  │   (authed)
                         └───────────────────────────────────────────┼──┘
                                                                      │
   generator (background)  ── enumerate segments from frigate.db ─────┘
       every ~60s          ── ffmpeg -vf fps=1/N,scale=320:180 ──▶ tile ──▶ sheet.webp on disk
       extend toward now   ── record bucket/sheet rows in sidecar.db
                           ── read segments from Frigate's /media (RO mount)
```

Three moving parts:

- **The generator** (§5): a continuous background loop that samples real recording segments to a uniform cadence, tiles them into sheets on disk, and records what it produced in the sidecar DB.
- **The `/v1` read layer** (§4): serves sheets and coverage from disk + `frigate.db`, immutable and ETag'd.
- **The proxy** (§6): forwards everything else to Frigate so the app has one origin.

---

## 3. Two decisions that shape everything — recommended, flagged for sign-off

These two are genuinely the deployment owner's call and they change the build. Recommendations given; see §12 for the rest.

### 3.1 Generation source — **recommend: recording segments via a `/media` read-only mount**

There are two possible frame sources:

| Source | Uniform cadence? | Top-of-hour hole? | Needs `/media` mount? |
|---|---|---|---|
| **Recording segments** (`.mp4` on `/media`, sampled with `ffmpeg -vf fps=1`) | **Yes — ffmpeg guarantees it** | **No** — recordings are continuous 10 s segments | Yes (RO) |
| Preview cache over HTTP (`preview.mp4` + preview WebP frames) | No — motion-driven, irregular; resampling to "uniform" would fabricate cadence | **Yes** — `preview.mp4` 404s until the hour completes; WebP cache emptied at :00 | No |

The requirements ask for uniform cadence *and* name the top-of-hour hole as the worst thing about Frigate's own cache. **Only the recordings path satisfies both honestly** — `ffmpeg -vf fps=1` samples real frames at exact 1 s spacing, and recordings have no hourly hole. This also makes `published_through` and `recorded` free: they come straight from `frigate.db`'s `recordings` table, which the sidecar already opens read-only.

The cost is a read-only bind-mount of Frigate's recordings directory (the sidecar already bind-mounts `config.yml` and `frigate.db`; this is one more line — see §8). Where that's genuinely impossible, §5.6 gives an HTTP-only degraded generator, but it cannot promise a clean interval and should be treated as a fallback, not the design.

**This spec assumes the recordings-via-`/media` path throughout.**

### 3.2 Proxy target & auth — **recommend: proxy to Frigate's authed port (:8971), pass the app's cookie through**

Today `FrigateClient` talks to `frigate.base_url`, which points at Frigate's *unauthenticated* internal API (`:5000`). That's correct for the sidecar's own server-to-server analysis calls and must stay.

For **proxying app traffic**, the sidecar should forward to Frigate's **authenticated** endpoint (`:8971`) and pass the client's `Authorization`/`Cookie` headers through untouched. This keeps auth exactly where it is — Frigate's — so:
- Elsinore reuses `FrigateSession.playbackCookieContext()` unchanged; there's no second password story.
- The sidecar never holds or validates the user's Frigate password.
- `/vod/*` and `/live/*` playback carry the same session cookie they already do.

Config therefore grows a **second** Frigate URL: `frigate.proxy_base_url` (the authed `:8971`) distinct from `frigate.base_url` (the internal `:5000`). See §7.

**Protecting the `/v1` endpoints themselves — decided: option (a), authenticate `/v1`.** The sprite cache is not low-sensitivity: it serves footage stills, on the same hostname the `/api` proxy already demands a Frigate session for. Leaving `/v1` open behind only the LAN/NPM layer (the originally-recommended option (b)) would make the sidecar a way around Frigate's own auth for imagery — client-side review flagged this as blocking, correctly. The rule: **`/v1` must never be less protected than the endpoints it sits beside.**

Require the same Frigate auth cookie on `/v1/*`. Validating it on every request against Frigate would add real latency, so validate once and cache the result for ~60 s (per-session, keyed on the cookie value) rather than a per-request upstream round trip. This costs the client nothing — Elsinore already sends cookies on every request via `FrigateSession.playbackCookieContext()` — and costs the server a small amount of code for a real property. Unauthenticated `/v1` requests get `401` with the same `www-authenticate` shape the proxy relays from Frigate, so the client's existing 401-handling path covers both.

---

## 4. `/v1` endpoint contracts

All timestamps are **unix seconds, floating point** — never ms, never strings, never ISO-8601 (matches Frigate and the client's `TimeInterval`). All new routes live under `/v1/`. New router module: `src/marcellus/routes/scrub.py`, registered in `server.py` alongside the others.

### 4.0 Contract hygiene (applies to every endpoint below)

Each of these cost a debugging session on bare Frigate; bake them in from the start.

- **Never 200 an unknown path.** Return `404` with a JSON body, never HTML. (Frigate's SPA catch-all 200s any non-`/api` path with HTML; a typo then fails at the decoder. FastAPI won't do this for `/v1`, but the proxy must not forward SPA-shell 200s as if they were data — see §6.4.)
- **Errors carry a machine-readable reason:** `{"error": "not_generated", "message": "…"}`. The reel draws "nothing here yet" differently from "something is broken" and cannot without being told. Reason vocabulary: `not_generated`, `not_covered`, `camera_unknown`, `upstream_unavailable`, `bad_range`.
- **`Cache-Control: public, max-age=31536000, immutable`** on any sheet or frame older than the bucket's `generated_through`. They never change; `URLSession`'s own cache then does the LRU work and the client deletes its bounded cache. (Same header `wildlife.py` already puts on content-addressed posters.)
- **`ETag`** on `/v1/coverage` and `/v1/reel` (they're polled while the reel is open). A matching `If-None-Match` returns `304`.
- **Times float, always.**

### 4.1 Capability probe — `GET /v1/capabilities`

```
200 → {
  "version": "1.0.0",
  "scrub_cache": {
    "enabled": true,
    "format": "sprites",              // "sprites" | "jpegs" — which reader the client uses
    "cameras": ["doorbell", "garden"], // cameras actually being generated (see §12 Q3)
    "generated": true,                 // false = running but hasn't backfilled yet
    "intervals": [1.0, 5.0, 60.0, 300.0, 900.0, 3600.0]  // every tier this deployment is
                                        // configured to produce (decode + derived, §5.8) --
                                        // what `/sheets?interval=` may be asked for
  },
  "proxy":  { "enabled": true },
  "push":   { "enabled": false }
}
```

**No `capabilities.http2` field** (dropped — client-side review finding, correct: `URLSession` negotiates HTTP/2 via ALPN on its own without being told; `httpMaximumConnectionsPerHost` is fixed at session-creation time, before any capability probe response arrives, so the client can't act on the flag without tearing down its session anyway; and the FastAPI app has no visibility into what the terminating proxy actually negotiated with the client, so behind NPM it would just echo back whatever NPM was configured to claim, not reality). Still enable HTTP/2 on whatever fronts the origin (§6.5) — it's real and worth doing — just don't advertise it as a capability.

- A `404`, or `scrub_cache.enabled == false`, means **fall back to bare Frigate**.
- `cameras` lists what is *generated*, not merely what's enabled — a sidecar that's up but hasn't backfilled a given camera is a different state from one that has, and the reel shows the difference rather than looking broken.
- `format` lets JPEG-first ship before sheets exist without a contract change (§5.4).

### 4.2 Scrub coverage (what sprite data exists) — `GET /v1/scrub/{camera}/coverage?start={ts}&end={ts}`

This answers *"what frames do I have, and at what cadence"* — distinct from recording coverage (§4.4), which answers *"what did Frigate record"*.

```
200 → {
  "camera": "doorbell",
  "buckets": [
    { "start": 1785380400, "end": 1785384000, "interval": 1.0, "width": 320, "height": 180 },
    { "start": 1785294000, "end": 1785380400, "interval": 5.0, "width": 320, "height": 180 }
  ],
  "generated_through": 1785384060,
  "retention_days": 4
}
```

- **`interval` is a hard contract, with a stated and enforced error bound:** every frame in `[start, end)` exists within `interval / 2` of `start + n·interval`. This is not automatic — Frigate's own recording segments are *about* ten seconds and their starts drift, so per-segment sampling produces a series offset from the bucket grid and from other cameras' series. The generator **records the achieved timestamp per cell** during generation and asserts it against the bound; when it can't be held, **split the bucket** rather than silently rounding two different moments onto the same cell (§5.2, §11 test). A recording gap is the same case, handled the same way. The client relies on this bound to place frames by arithmetic with no per-frame list.
- **`generated_through`** is the newest moment with a frame behind it — the sidecar's own live-edge lag, stated so the reel never guesses.
- Buckets may **thin with age** (`interval` 1.0 recent, 5.0 older) — see §5.5 for exactly what "older" means now that continuous retention is ~4 days, not 14.
- **Buckets do not overlap in time for a given camera** — this contract is about the two decode tiers (recent/aged). The bucket schema's primary key is `(camera, start_ts, interval_s)`, so a moment could in principle carry both a 1.0 s and a 5.0 s bucket if thinning and generation raced — the generator must retire (or never emit) the finer bucket once a coarser one supersedes its span, so a client never has to choose between two buckets for the same instant. Derived tiers (§5.8) are a deliberate exception: they overlap the decode tiers and each other on purpose, so this endpoint excludes them (`grid.exclude_derived_buckets`, shared with `/v1/reel`, §4.5) and keeps its one-bucket-per-instant contract; select a derived tier explicitly via `/v1/scrub/{camera}/sheets?interval=`.
- **Past `retention_days`, there is nothing to sample, and the response says so as a distinct state.** A span older than `retention_days` is not "not generated yet" (which implies it's still coming) — it's a span that will *never* have a bucket. The client compares the queried range against `retention_days` (both fields it already has) to distinguish "will never exist" from "lagging"; no separate flag is needed, but this must be documented explicitly so the two states aren't drawn identically (client-side review finding 1).
- **No `frames[]` array anywhere.** The uniform interval makes it redundant, and a redundant array is one that can disagree with the image.
- **Never a placeholder or black cell.** A synthesized/blank frame presented as footage is worse than a hole — split the bucket instead (same rule as the gap case above).

### 4.3 Sprite sheets — `GET /v1/scrub/{camera}/sheets?start={ts}&end={ts}` and the images

```
GET /v1/scrub/{camera}/sheets?start={ts}&end={ts}
200 → {
  "sheets": [
    {
      "url": "/v1/scrub/doorbell/sheet/1785380400-1.0-96.jpg",
      "start": 1785380400, "interval": 1.0,
      "cols": 12, "rows": 8, "cell_w": 320, "cell_h": 180, "count": 96
    }
  ]
}

GET /v1/scrub/{camera}/sheet/{start}-{interval}-{count}.jpg   → image/jpeg (or image/webp), always immutable
```

- **Cell for time `t`:** `idx = round((t − start) / interval)`, laid out **row-major** — `row = idx // cols`, `col = idx % cols`. No timestamp list; nothing to get out of sync.
- **Sheet URLs are content-addressed by `(start, interval, count)`, not just `(start, interval)`.** The earlier draft kept one URL while a still-filling sheet's `count` grew, served with `Cache-Control: immutable` — that's a silent-wrong-frame bug: the client caches the image at `count=12`, the generator advances it to `count=40`, the URL doesn't change, and the client's cell-index arithmetic now points past what it has cached with no way to detect the mismatch (client-side review finding 3, blocking). Putting `count` in the filename makes **every version of a sheet its own immutable object** — the live/still-filling sheet is genuinely a different URL each time it grows, `immutable` is true unconditionally, and the client builds the URL from `count`, which `/sheets` already returns. No freshness reasoning exists anywhere in the system.
- **Why sheets, not individual frames:** measured, individual frame fetches saturate at ~88 frames/s over six HTTP/1.1 connections and going wider *worsens* per-request latency (p95 110 ms → 250 ms from 6→24 conns) without the client being able to use the throughput. One sheet fetch covering a whole drag span sidesteps the connection cap entirely.
- **Sizing:** cap a sheet at ~**96 cells** (≈ two minutes of 1 fps timeline). At 320×180 that's a decoded footprint of **~21 MB** (measured: 12×8 grid = 3840×1440 RGBA); the client holds **two**, not three (client-side review measured its own memory budget and settled on two — match that number here so both sides size to the same figure). `count` may be < `cols·rows` for the newest (still-filling) sheet.
- **`count` means "cells rendered from real imagery", and is measured from the cell store at publication — never from assignment indices.** The tiler composes onto a black canvas and pastes each cell at its own index, so *any* index below `count` without a cell file is served as a black frame while the index declares it covered. Shipped as exactly that bug: a client attributed an on-screen black frame to `stairway-tight` sheet `1785816900-60.0-45.jpg` cell 39, and a clean generation cycle over live recordings was measured leaving a one-cell hole in 9 of 14 sheets. The published count is therefore the **contiguous run of cells present from zero**; cells past a hole stay on disk but are not claimed, and the sheet extends over them (as a new URL, per the rule above) once backfill fills the hole. A client that finds a span unclaimed falls back to Frigate's preview-frames cache, which holds real stills for it — strictly better than a black cell it believes.
- **Sheets published before that rule keep their inflated count** — their span has passed, so nothing republishes them. `fsc scrub verify --repair` is the one-shot fix: it re-derives each sheet's true count from the cell store, or from the published pixels when the store is gone, and republishes at the honest count (same image, honest name).

### 4.4 Recording coverage (what Frigate recorded) — `GET /v1/coverage/{camera}?start={ts}&end={ts}`

The field that would have prevented four bugs this week. Read straight from `frigate.db`'s `recordings` table (RO) — no proxy call, no parsing hundreds of segment objects on the client.

```
200 → {
  "camera": "doorbell",
  "queried":  [1785380000, 1785384000],
  "recorded": [[1785380000, 1785381240], [1785381600, 1785383990]],
  "latest_segment_end": 1785383990,
  "authoritative_through": 1785384033.8,
  "retention_days": 4
}
```

- **`queried`** is the span this answer covers. Anything outside it is **unknown, not empty**. This one field is the whole ask: it lets the reel stop conflating "no recording" with "I haven't looked". *(Bare Frigate's empty array means both.)*
- **`recorded`** as merged intervals, not raw segments — the reel already merges them; doing it server-side saves parsing and removes segment-boundary off-by-ones.
- **`published_through` is split into two fields with different meanings — this was wrong in the original draft (client-side review finding 4, blocking).** The original single field was `MAX(end_time)` over the camera's `recordings` rows, meant as the boundary beyond which the client makes no coverage claims. But when a camera goes **offline**, `MAX(end_time)` stops advancing — so the "no claims" band grows without bound and the reel stops hatching a genuine outage, which is exactly backwards: a dead camera would render identically to a span the sidecar just hasn't fetched yet.
  - **`latest_segment_end`** — diagnostic only. The newest committed segment's `end_time`, straight from `MAX(end_time)`. Useful for a camera-health display; **must not** gate coverage claims.
  - **`authoritative_through`** — `now − measured_publish_lag`, using the *measured* commit lag (**6.2 s**, both `alley-wide` and `doorbell` measured live 2026-07-30 — within the review's estimated 4–10 s range), not the newest segment. **Only this field gates what the client claims as covered.** It keeps advancing at wall-clock rate even while a camera is silently dead, so `recorded` stops matching `authoritative_through` immediately — which is the outage signal.
- **`ETag`** required (§4.0): the window containing *now* is re-polled every ~10 s; a `304` lets the sidecar decide how often the answer really changes.

### 4.5 One call per reel window — `GET /v1/reel/{camera}?start={ts}&end={ts}&motion_scale={s}`

Collapses the three-or-four parallel requests the reel makes to paint one window (motion, segments, events, frame list) into one. Same lifetime, same cache key, same failure mode → one call.

```
200 → {
  "queried":  [start, end],
  "recorded": [[…]],
  "latest_segment_end": 1785383990,
  "authoritative_through": 1785384033.8,
  "frames": [
    { "start": …, "interval": 1.0, "count": 3600 },
    { "start": …, "interval": 5.0, "count": 120 }
  ],
  "motion": { "start": …, "interval": 10, "values": [0,4,61,88,…] },
  "events": [ { "id": "…", "label": "person", "zones": ["driveway"],
               "start": 1785381200, "end": null, "score": 0.81,
               "sub_label": "amazon", "has_clip": true, "has_snapshot": true,
               "path": { "drift": [[1785381200.0, 0.53], …], "dwell": [[1785381220.0, 1785381244.0]] },
               "continues": { "camera": "gate-walkway", "event_id": "…", "start": 1785381262.4 } } ],
  "reviews": [ { "id": "…", "start": 1785381195, "end": 1785381260,
                "severity": "alert", "objects": ["person"], "zones": ["charger"],
                "detections": ["…"] } ]
}
```

- **`motion.values` is a bare array on a declared grid** — `value[i]` covers `[start + i·interval, start + (i+1)·interval)`. An hour at `scale=10` is 360 numbers, not 360 timestamped objects. Biggest byte win on the page and it costs nothing to produce.
- **`frames` is an array of descriptors, not one descriptor.** A single reel window commonly straddles the recent/aged thinning boundary (§5.5), so one `{start, interval, count}` can't express two different cadences inside the same response — it needed the same shape as `/v1/scrub/{camera}/coverage`'s `buckets[]` (§4.2), so make it identical: an array. Each descriptor is the same start/interval/count triple, and the client places cells by arithmetic per-bucket exactly as it does against §4.2. `frames` excludes derived tiers (§5.8) the same way `/v1/scrub/{camera}/coverage` excludes them from `buckets` — same `grid.exclude_derived_buckets` helper, so the two endpoints never disagree about which rows are in play for an overlapping-tier window.
- **`events[].end` is nullable and `null` means "still in progress."** An event that hasn't closed yet (the object is still present) has no end time. The client's `ObjectTrack` keys `sawExit` off a nullable end and deliberately draws no exit cap when it's null — the server must emit `null`, not omit the field or send a placeholder timestamp, or the client draws a false exit.
- **`events[].sub_label`, `has_clip`, `has_snapshot` are column-gated** (added 2026-08-23). They are read straight off Frigate's `event` schema, so a Frigate build without a column yields `null`/`false` rather than a 500 — the same posture `zones` already had. `sub_label` is the recognised face, plate or carrier and is the entire point of an identification camera; the media flags are the difference between a track a client can open and one it cannot.
- **`reviews` is Frigate's own alert/detection decision** (added 2026-08-23), from `reviewsegment`. It is on this endpoint because it answers the one question `events` cannot: which of these tracks was the fleet meant to notify about. Confidence cannot stand in for it here — measured on this fleet the Frigate+ model's scores are bimodal and high (p10 0.83, median 0.87), so `score` separates almost nothing while severity separates cleanly. `reviews[].end` is nullable with exactly the meaning `events[].end` has. `detections` carries the review's member event ids, so a client can join back to `events[]` without a second query; it is always a list, never `null`. A Frigate with no `reviewsegment` table returns `[]` — a reel with no severity spine, not a broken endpoint.
- **`events[].path` is a trajectory digest, nullable** (added 2026-08-24), derived from Frigate's `data.path_data` blob. `drift` is `[[t, x], …]` — the object's normalized horizontal position over time, decimated to ≤16 points (the stored path runs to thousands; the wheel renders a bar a few points wide, so anything finer is wasted bytes) with the exit position always kept. `dwell` is `[[t0, t1], …]` — spans (≤4) where the object stayed within 0.05 normalized units for ≥10 s. All timestamps are shifted onto the record clock exactly like `start`/`end`. `null` when there is no blob, fewer than 3 points, or under 5 s of path — a client must treat null as "draw a straight bar", not an error. The digest is a pure function of the stored path, which matters because the reel body is ETagged on content: instability here would bust the client cache on every poll.
- **`events[].continues` is the same visit's next appearance on another camera, nullable** (added 2026-08-24): `{camera, event_id, start}` for the earliest same-label event on any other camera starting within `[event.start, event.end + 60 s]` — the crosscam pipeline's measured visit window (real visits fire on the next camera within seconds). Cross-camera time comparison happens on the record clock with each camera shifted by **its own** §4.5-adjacent annotation offset, and the returned `start` stays on the *target* camera's record clock — it is precisely the timestamp a client hands to the target camera's reel after switching. One widened window query per request, not per event.
- **`queried` is inclusive of `start`, exclusive of `end`** — `[start, end)`, matching how `recorded` and bucket spans are already documented (§4.2, §4.4). Stated explicitly here since it wasn't spelled out originally and both sides need to agree.
- Deletes client-side: three window caches, three in-flight guards, and the `ReelDataSource` latest-wins pump that exists only because those three sources could disagree about which window they described.
- **ETag** as §4.0.

### 4.6 Total motion — `GET /v1/motion/{camera}?start={ts}&end={ts}&scale={s}`

Re-serve Frigate's `/api/review/activity/motion` (which is genuinely good — 61 ms, normalised 0–100 per camera, resolves to 1 s) but fix its two measured cliffs:

- **`scale=3600` returns all zeros** over multi-day windows while claiming full coverage → the client caps `scale` at 300 and aggregates the rest itself.
- **Short windows return short answers** — a 1800 s request at `scale=60` came back with 10 buckets covering 540 s.

Contract: **any `scale`, always covering the full requested `[start, end)`, zero-filled where there is genuinely no data.** The sidecar fetches from Frigate at a safe scale (≤300), aggregates/zero-fills to the requested grid, and returns the bare-array form. Then the client's `ReelGranularity.motionScale` and `aggregatesMotion` both disappear. (This is also the `motion` block inside §4.5 — implement once, expose both.)

- **`motion_unavailable` (additive, shipped `7cddddd`).** Both `/v1/motion` and the `motion` block inside `/v1/reel` (§4.5) gained an additive boolean, true when the sidecar could not produce a motion series for the window (e.g. Frigate's motion source is down, or the window predates coverage). It exists so the zero-fill contract above doesn't get misread as "genuinely zero" — a client should draw nothing rather than a false flat line when this is set.

### 4.7 Highlights (Tier 1, after Tier 0) — `GET /v1/highlights/{camera}?before={ts}&limit=10`

Turns the reel from a ruler into a search tool — "take me to the next interesting thing", which needs a ranked index across a long span that no Frigate endpoint provides.

```
200 → { "highlights": [ { "start": …, "end": …, "reason": "person", "score": 0.9 } ] }
```

- **`highlights[].reason` is a Frigate object label** (`person`, `car`, `package`, …) — the same vocabulary as `review`/`events` label fields, not a separate category scheme. State this explicitly: the client maps labels to lanes and deliberately returns `nil` for unrecognized ones, so an undocumented or inconsistent vocabulary here would silently drop every highlight.

Cheap for the sidecar: precompute from `review` items + motion. This is the one item that adds a *feature* rather than removing client complexity — build it after Tier 0, before the rest of Tier 1.

### 4.8 On-release handoff (no new endpoint)

Sprites drive the *drag*; on release the player seeks real video via the VOD range endpoint `/vod/{camera}/start/{start}/end/{end}/index.m3u8` — which is **already reachable at this same origin through the proxy** (§6), so there's no second auth story. Nothing to build here; it's a consequence of the proxy existing.

---

## 5. The generator

The heart of feature 1. A continuous loop that keeps the sprite cache extended toward *now* and thinned with age.

### 5.1 Source of truth: `frigate.db` + `/media`

The sidecar already opens `frigate.db` read-only (`db.py::open_frigate_ro`). Frigate's `recordings` table holds one row per ~10 s segment with `camera`, `path`, `start_time`, `end_time`, `duration` (verify exact columns against the live 0.17.2 schema at runtime — reuse the `PRAGMA table_info` pattern already in `triage/sampler.py::_select_optional_columns`). This gives, for free and without a proxy round-trip:

- the **segment files to sample** (their `path`, mapped from Frigate's container path to the sidecar's mount — see §8.2),
- **`recorded`** intervals and **`published_through`** for §4.4 (`MAX(end_time)`),
- exactly which spans have footage, so the generator never wastes ffmpeg on a gap.

### 5.2 Sampling to a uniform interval — **GOP-driven, measured, not assumed**

**Measured (M1, 2026-07-30, live box):** GOP on a real `alley-wide` recording segment is **exactly 1.0 s** (15 fps stream, keyframe every 15 frames, landing on whole-second boundaries). This is the best case flagged as an open question in the earlier draft, and it resolves cleanly: **keyframe-only decode gives ~1 fps natively, at roughly a third the CPU cost of full decode-and-discard** (M2: 0.68 s wall / 111% CPU / 79 MB RSS for keyframe-only vs. 1.36 s wall / 176% CPU / 117 MB RSS for `fps=1/N` full decode, both on the same 10 s segment — extrapolated across 10 cameras continuously: ~18 CPU-hours/day keyframe-only vs. ~57 CPU-hours/day full decode).

Given that, sample with keyframe skipping, not a full-decode `fps` filter, for the 1 fps recent tier:

```
ffmpeg -nostdin -loglevel error -skip_frame nokey -vsync 0 -i <segment.mp4> \
       -vf "scale=320:180" -q:v 8 -f image2 <outdir>/%06d.jpg
```

- `-skip_frame nokey -vsync 0` decodes only keyframes and passes them through 1:1 (no cfr resync, which would otherwise duplicate frames to fill a target rate) — **this is what makes the cadence uniform and honest** on this deployment's GOP, at a fraction of full-decode cost. It selects real frames, never interpolates.
- **This assumes GOP ≈ target interval.** If a future camera or Frigate config produces a coarser GOP (e.g. one keyframe per full 10 s segment), keyframe-only decode would only yield 0.1 fps and the generator must fall back to `-vf fps=1/N` full decode for that camera, or accept a coarser default interval for it. Check GOP once per camera at startup (one `ffprobe` call) rather than assuming uniformity across the fleet — a mixed-hardware deployment could have mixed GOPs.
- For the **aged tier** (interval coarser than the measured GOP, §5.5): prefer decimating it from the recent tier's already-cached frames (§5.8-style PIL crop, no ffmpeg) whenever the recent tier already covers the span end to end — measured on a box with no decode headroom to spare, spending a slice of the tick on `-vf fps=1/N` full decode for the aged tier starved the live edge. Fall back to decode only for spans the recent tier never reached (older than it has ever covered).
- `scale=320:180` — §1.5 of the requirements: 320×180 is enough; don't spend disk on resolution. Spend budget on interval and retention instead.
- Frame `k` from segment start maps to wall-clock `segment.start_time + k·(actual keyframe spacing)`, which maps to sheet cell `round((t − bucket.start) / N)`. **Record each cell's achieved timestamp** and assert it's within `interval / 2` of the grid point (§4.2); split the bucket when it isn't — this is the client-side review's finding 7 (cadence needs an error bound and verification), now load-bearing since per-segment sampling is exactly the mechanism that can silently drift off-grid.
- Reuse the concurrency discipline already in `wildlife.py`: an `asyncio.Semaphore` (start at 3) caps simultaneous ffmpeg processes, and a per-extraction timeout kills a wedged one.

### 5.3 Tiling into sheets

Accumulate frames into a montage of `cols × rows` (target ~96 cells, e.g. 12×8). Two viable tiling paths:

- **ffmpeg `tile` filter** in one pass: `-vf "fps=1/N,scale=320:180,tile=12x8" ` writes montage(s) directly — fewest processes, no intermediate JPEGs.
- **Pillow/`opencv`** compositing from the per-frame JPEGs — more control over partial (still-filling) sheets and re-encoding to WebP.

Recommend the **ffmpeg `tile`** path for completed buckets (cheap, one process) and Pillow only for the **live, partially-filled** sheet that's re-tiled each cycle until full.

**Default output format: JPEG, not WebP** — reversed from the earlier draft. Measured (M4, 2026-07-30): a real 96-cell sheet built from consecutive `alley-wide` keyframes came out **604 KB as JPEG (`-q:v 8`) vs. 827 KB as WebP (`-lossless 0 -q:v 75`)** — WebP did not win on real (noisy) camera content the way the estimate assumed. Worse, omitting `-lossless 0` on the WebP encoder silently produces a **near-lossless ~1 MB file** — an easy mistake that would have shipped 70% oversized sheets. Ship JPEG as the default and only path initially; `capabilities.scrub_cache.format` still exists so WebP can be added later as an explicit opt-in (with `-lossless 0` mandatory and documented, not implied) without a contract change.

Write atomically: extract to a temp file in the cache dir, then `os.replace` into place (the exact atomic-publish pattern in `wildlife.py::wildlife_poster`). A sheet is only advertised in `/v1/scrub/.../sheets` once fully written.

### 5.4 Cadence: **continuous, ~60 s timer — never hourly**

⚠️ **This is the single most important operational rule.** The earlier contract draft proposed an hourly cron. **That reproduces the worst hole we have.** Measured at 19:01:40, Frigate's own WebP cache held exactly three frames because it's emptied at the top of every hour, and `preview.mp4` for the current hour 404s until the hour completes — so the first stretch of every hour, which is *exactly the recent past people look at*, is nearly empty. An hourly cron would rebuild that hole in the sidecar.

Instead: a background loop on a **short timer (~60 s)** that always extends the newest bucket toward `published_through`. A minute of live-edge lag is invisible; an hour of it is the bug we already have. Publish `generated_through` so the client knows where the edge is.

Two implementation shapes:
- **(a) In-process asyncio task** started in the FastAPI lifespan, looping every 60 s. Survives as long as uvicorn does; simplest; shares the process. **Recommended.**
- **(b) systemd timer** running `fsc scrub generate` every 60 s (mirrors the existing `contrib/frigate-sidecar-faces.timer`). Better isolation; survives a wedged event loop; matches the LXC deployment's habits.

Recommend **(a)** for the continuous forward edge, with the CLI (§5.7) also available for **(b)** and for one-shot backfill. Either way it is *not* hourly.

**The tick is a deadline, not a sleep (`live_edge_interval_s`, default 20 s).** "Loop every 60 s" was read as *cycle, then sleep 60 s*, and the cycle grew when an earlier version of the generator fed several tiers from one decode pass, reaching ~65 s on its own; backfill's budget landed on top, and the effective cadence became ~100 s with measured worst lag ~105 s — past the ~90 s the client is told to expect, and past it further whenever a slow segment stretched the cycle. Cadence *is* the freshness bound: a camera serviced at the top of one tick is untouched until the next. So the trailing-window pass now starts every `live_edge_interval_s` and **backfill is given the remainder of the tick** (bounded also by `backfill_time_budget_s`), and **derived-tier decimation (§5.8) runs last, out of whatever's left of the tick** — priority is live-edge, then backfill, then decimation. A tick that overruns is not slept off. History can wait, the edge cannot.

**Decimation needs a guaranteed floor, not pure leftovers.** Deployed and measured live: backfill's own demand does not reliably reach zero — a couple of cameras had a persistent small trickle of real holes (motion-driven recording gaps) every single cycle, each taking 10+ seconds of genuine ffmpeg work. Backfill alone consumed the entire `backfill_time_budget_s` on 4 of 10 cameras, and derived-tier decimation got zero cycles across several minutes of real operation — "leftover budget only" meant "no budget, ever" in practice. `scrub.derive_time_reserve_s` (default 5 s) carves a floor out of `backfill_time_budget_s` reserved exclusively for decimation; backfill's own deadline shrinks by that amount (capped at half its budget, so a small/misconfigured budget can't erase backfill's own "first camera in rotation is always attempted" guarantee), and decimation still gets whatever backfill leaves unused on top. Set to `0` to restore pure-leftover behaviour.

Shortening the tick does not change throughput — the same segments are decoded either way, in smaller instalments — so latency improves at equal CPU. What it costs is **sheet versions**: a still-filling sheet is published once per tick and every version is its own immutable object (§4.3), so a 96-cell 1 s sheet accumulates ~5 growing versions instead of ~2. `sheet_version_grace_s` (default 900 s) sweeps superseded *incomplete* versions back up in the retention pass, which more than pays for the extra churn — complete sheets are never superseded and are never swept by it.

### 5.5 Retention & thinning — **4 days, not 14, and this is a hard ceiling**

**⛔ Blocking correction (client-side review finding 1, independently reconfirmed, M3):** continuous (uniformly-sampleable) footage does not last 14 days. Measured `doorbell` and independently reconfirmed on `alley-wide`, `crows-nest`, and `street` (2026-07-30):

| days back | alley-wide | crows-nest | street |
|---|---|---|---|
| 1 | 100% | 100% | 100% |
| 3 | 100% | 100% | 100% |
| 4.2 | 1% | 1% | 4% |
| 7 | 1% | 0% | 14% |
| 9 | 0% | 0% | 1% |

Every camera checked shows the same shape: a **hard cliff at ~4 days** (matches `record.continuous.days: 4.0` in the live config), then a thin, per-camera-inconsistent motion-only tail out to ~8–9 days (`record.motion.days: 8.0`), then nothing. This is a config fact, not noise — expect it to hold across the fleet.

Consequences:
- **`scrub.retention_days` defaults to 4, not 14.** Past day 4, there is no continuous source to sample uniformly at all — 14 days was never buildable regardless of disk budget.
- **Days 4–8 are a distinct, explicitly degraded state**, not a thinning tier. Motion-only clips give 1–14% hourly coverage in the measurements above — a uniform-interval bucket there fragments into dozens of short buckets per hour. This is not the same shape as the recent/aged thinning below and the generator should not try to force it into that shape; either skip generation past day 4 entirely (recommended — simplest, and the CPU/disk cost buys little for what's a fragmentary, inconsistent tail) or generate it accepting heavy fragmentation and document that explicitly. **Recommend: skip past day 4.** `retention_days: 4` then means exactly what it says.
- **`aged_after_h: 24` has little room to matter inside a 4-day window** — reconsider whether a second thinning tier earns its complexity at all versus just running the whole 4-day window at 1 fps (see disk math below — 4 days at 1 fps is affordable without thinning).
- Keep a **recent tier at 1 fps** across the full continuous window; only add a coarser aged tier if the disk math below doesn't fit the deployment's actual free space (§5.5 disk math, and see the hard constraint in §8.3 on cache placement).
- A prune pass (part of the loop, or `fsc scrub prune`) drops sheets whose bucket `end < now − retention_days`, oldest-first — the bounded-by-mtime eviction `wildlife.py::_prune_poster_cache` already demonstrates.

**Disk math — measured, not estimated (M4, 2026-07-30):** a real 96-cell, 320×180 JPEG sheet (`-q:v 8`) off `alley-wide` came to 604 KB (6.3 KB/frame). At 900 sheets/day (86400 s ÷ 96 cells) that's **~544 MB/camera/day**. Across the live deployment's actual **ten** cameras (not eight — corrected count, see §5.5.1) at the corrected 4-day retention: **~21.8 GB total**, in line with the client-side review's own corrected estimate (~18 GB) once the retention correction is applied. This replaces the earlier 50–60 GB estimate (which was both over-retained at 14 days and undercounted at 8 cameras).

#### 5.5.1 Camera count correction

The deployment has **ten** cameras, not eight: `alley-wide, crows-nest, doorbell, garden, gate, package, stairway-tight, stairway-wide, street, walkway`. The "8 cameras" figure in the earlier draft and in the Elsinore requirements doc was a counting error on the client side, propagated here. Use 10 for all capacity planning in this spec and in `scrub.cameras` defaults.

### 5.6 Degraded HTTP-only generator (fallback only)

If a `/media` mount is truly impossible (§3.1), the generator can instead pull `preview.mp4` in **≤4-minute windows** (measured: the response caps at ~254 frames, so wider requests just thin the same frames) and, for the current hour, the individual preview WebP frames. **But this cannot promise a clean interval** — the source is motion-driven — so the buckets it produces must declare the *actual* achieved spacing and accept that "uniform" degrades to "as-uniform-as-the-source". Treat as a compatibility path, not the design.

### 5.7 CLI surface (mirrors the existing `fsc` groups)

Add a `scrub` typer group in `cli.py`, matching how `triage`/`analysis`/`faces` are structured:

```
fsc scrub generate  --camera doorbell [--since <ts>] [--interval 1]   # forward edge / one cycle
fsc scrub backfill  --camera doorbell --days 14 [--interval 1]        # one-time history fill
fsc scrub prune     [--camera doorbell] [--drop-interval N ...]       # apply retention, and
                                                                        # optionally sweep every
                                                                        # bucket/sheet at exactly
                                                                        # interval_s=N regardless of
                                                                        # retention (§5.8 migration)
fsc scrub coverage  --camera doorbell                                 # print what's generated (debug)
```

Backfill choice (§12 Q4): batch the whole retention window on first run, or generate forward-only and let history fill in? Affects whether the reel shows a "generating" state — `capabilities.scrub_cache.generated` exists for exactly this.

### 5.8 Derived tiers — decimated from the decode tiers, not sampled

The generator has two stages, run in this priority order every cycle: **decode tiers** (§5.2–§5.5 above — recent by ffmpeg, aged by decimating the recent tier when it already covers the span, ffmpeg-backfilled only for spans the recent tier never reached), then **derived tiers** (`scrub.derived_intervals_s`, default `[60, 300, 900, 3600]`), then nothing further that cycle.

**Aged-tier decimation runs as part of Pass 3, ahead of the `derived_intervals_s` loop**, using the same crop-and-retile machinery as derived tiers (`generate_derived_tier`'s `_decimate_source`/`_TierWriter`), parameterised on `recent_interval_s` → `aged_interval_s` instead of a decode tier → a derived interval. It registers ordinary aged buckets/sheets — `interval_s == aged_interval_s`, `complete=1` — indistinguishable from what `generate_backfill`'s aged-tier pass would have written, so the client and `_span_covered_by_aged` see no difference. Retiring the now-redundant recent-tier data (`_retire_stale_recent_buckets`) runs after this pass, in the same cycle, so the finer 1 s data doesn't pile up waiting for a decimation that already happened.

A derived tier is never sampled from a recording segment. For each configured interval it picks every Nth already-published cell out of whichever decode tier is finest over a given span (recent inside `aged_after_h`, aged behind it), crops it from that tier's own published sheet image with Pillow, and re-tiles it through the same `scrub/tiling.py` path — recording buckets and sheets exactly like a decode tier (same `scrub_buckets`/`scrub_sheets` rows, same immutable sheet-URL scheme, same `_TierWriter`). No ffmpeg process is ever spawned for a derived tier; the cost is disk I/O and PIL crops. Each configured interval must be a whole multiple of `aged_interval_s` (`ScrubSection` validates this at config time); at runtime the generator additionally skips decimating from any decode tier that isn't an exact divisor of the derived interval, since `match_keyframe_cadence` can raise a given camera's *recent* tier past its configured value in a way the static validator can't see.

Derived tiers run **last**, out of whatever's left of the tick's deadline after live-edge and backfill (§5.4) — they never compete with the two decode passes for ffmpeg concurrency, and a slow cycle simply defers decimation rather than the live edge.

This replaces an earlier `coarse_intervals_s` mechanism (deleted) that generated standalone whole-retention-window tiers directly from ffmpeg, piggybacked onto the recent/aged decode via a `_TierWriter`-fan-out (`also=`). That doubled as the fleet's ffmpeg cost scaled with tier count; decimating from already-decoded sheets instead means the cost of an extra tier is a crop and a re-tile, not another decode pass. **Migrating an existing deployment off the old mechanism:** its default intervals (10.0, 60.0) don't map cleanly onto derived tiers' decimation invariants — sweep them with `fsc scrub prune --drop-interval 10.0 --drop-interval 60.0` (or `scrub/generator.py::drop_intervals`), then let the new derived tiers regenerate from the decode tiers already on disk.

---

## 6. The reverse proxy

Feature 2: make the sidecar the single origin. New router `routes/proxy.py`, registered **last** so `/v1/*`, `/static`, the sidecar's own HTML pages, and `/healthz` win first; everything else falls through to Frigate.

### 6.1 What it forwards

A catch-all that streams to `frigate.proxy_base_url` (the authed `:8971`, §3.2): `/api/*`, `/vod/*`, `/live/*` (go2rtc MSE/WebRTC), `/preview/*`, and Frigate's own assets. The reel only *needs* `/api/*` and `/vod/*` proxied today, but a transparent catch-all is simpler and future-proofs live streaming.

### 6.3 Related wire additions, shipped `7cddddd`

Three small, additive changes landed alongside the client's build 207-208 scrub redesign:

- **`/healthz` gained a `checks.frigate` entry** reporting whether the sidecar can currently reach Frigate. This is **informational only** — it never gates `/healthz`'s own status to a 503. The watchdog still owns Frigate recovery; a client should render "sidecar healthy, Frigate unreachable" rather than treating the whole stack as down when only this check is failing.
- **`/v1/push/map/live` gained a stale flag** so a client polling the live map push can distinguish "no update because nothing moved" from "no update because the feed died."
- **`scrub.min_free_bytes`** (default 2 GB) is a floor on the disk the generator (§5) writes to. Below the floor, sheet/frame generation pauses; pruning continues regardless, so the sidecar works its way back above the floor instead of wedging with generation and pruning fighting each other.

### 6.2 How — reuse the wildlife proxy pattern verbatim

`routes/wildlife.py` already implements a correct streaming reverse proxy; the scrub proxy is the same shape, generalised:

- **`httpx.AsyncClient` with `stream=True`**, body relayed via `StreamingResponse` (`wildlife.py::wildlife_media`).
- **Forward request headers** `Range` **and** `Authorization`/`Cookie` (extend the `_REQ_PASS` allow-list — wildlife only needed `range` because its upstream is unauthenticated; here we must also pass auth through so it stays Frigate's).
- **Relay response headers** `content-type`, `content-length`, `content-range`, `accept-ranges`, `cache-control`, **`etag`**, **`set-cookie`**, **`www-authenticate`** (extend `_RESP_PASS`).
- **Mirror upstream status** — 200/206/**401**/404 all pass through unchanged, so Frigate's auth challenge reaches the client intact.
- **No read timeout on media** (`httpx.Timeout(connect=…, read=None)`) — VOD/live are long-lived streams the user pauses and seeks (`wildlife.py` already does this).
- **Traversal guard** on the captured path (`".." in path.split("/")`), as wildlife does.
- Method pass-through: the reel is read-only (GET/HEAD), but `POST /api/reviews/viewed` and the export endpoints (Elsinore Tier 3) want POST/PUT — forward the method and body generically rather than GET-only.

### 6.3 What NOT to reimplement

Front these, don't rebuild them — they're already fast on bare Frigate: VOD manifests (146 ms, ~12 KB/hr), `/api/config`, `/api/stats`, snapshots, thumbnails. The proxy exists to unify the *origin*, not to re-serve what Frigate does well.

### 6.4 The SPA-catch-all trap

Frigate 200s any non-`/api` path with the web UI's HTML shell. The proxy must not let that masquerade as data: because we forward transparently and the *client* decodes, this is mostly the client's concern — but the sidecar should still **never** synthesize its own 200-with-HTML for an unknown `/v1` path (FastAPI returns JSON 404 by default; keep it that way, §4.0).

### 6.5 HTTP/2 — the cheapest item on the page

Measured: Frigate speaks **HTTP/1.1**, so `URLSession` caps at 6 connections/host and per-request latency *doubles* between 6 and 24 connections; prefetch is currently a latency trick, useless for throughput. If the sidecar's front (uvicorn, or the **NPM/Nginx layer already terminating TLS on this host**) speaks **HTTP/2**, multiplexing lets sheet fetches, coverage polls, and frame fetches stop queueing behind each other, and prefetch becomes real read-ahead. **This is a reverse-proxy config line, not a feature** — enable HTTP/2 on whatever fronts `:5001`. `capabilities.http2` advertises it so the client knows whether to widen its prefetch. Likely the highest value-per-effort item in this whole spec.

---

## 7. Config additions (`config.py`)

Extend the pydantic-settings models. New sections; existing ones untouched. Env overrides follow the established `FRIGATE_SIDECAR_…__…` convention.

```python
class FrigateSection(BaseModel):
    base_url: str = "http://frigate.lan:5000"          # (existing) internal, unauth — sidecar's own calls
    proxy_base_url: str = "http://frigate.lan:8971"    # NEW: authed origin for app-traffic proxy (§3.2)
    config_path: Path = Path("/opt/frigate/config.yml")
    db_path: Path = Path("/opt/frigate/database/frigate.db")
    media_path: Path = Path("/media/frigate")          # NEW: the DB's container-side recordings root — used
                                                         # only to strip this prefix from recordings.path (§8.2)
    recordings_path: Path = Path("/mnt/frigate-storage/recordings/recordings")  # NEW: host-side path the
                                                         # sidecar actually reads from (M6) — deployment-specific,
                                                         # must not be assumed 1:1 with media_path

class ScrubSection(BaseModel):
    enabled: bool = False                              # off by default; opt-in per deployment
    cameras: list[str] = []                            # [] = all 10 cameras (§5.5.1); else the opt-in set (§12 Q3)
    cache_dir: Path = Path("/data/scrub")              # MUST be a separate filesystem from Frigate's
                                                         # recordings volume — see §8.3, this is now a hard
                                                         # requirement, not a suggestion
    recent_interval_s: float = 1.0                     # 1 fps recent tier, GOP-driven (§5.2)
    aged_interval_s: float = 5.0                       # thinned tier — reconsider whether this earns its
                                                         # complexity now that the window is 4 days, not 14 (§5.5)
    aged_after_h: float = 24.0                         # when to drop to the aged interval
    retention_days: int = 4                            # hard ceiling: continuous footage on this deployment
                                                         # ends at ~4 days (M3); past this, nothing to sample
    cell_w: int = 320
    cell_h: int = 180
    sheet_cols: int = 12
    sheet_rows: int = 8                                # 96 cells ≈ 2 min at 1 fps
    format: str = "jpeg"                                # "jpeg" | "webp" (§5.3) — JPEG measured smaller on
                                                         # real camera content (M4); WebP is opt-in only
    generate_interval_s: float = 60.0                  # ceiling on the loop's tick (NOT hourly) (§5.4)
    live_edge_interval_s: float = 20.0                 # trailing-window cadence = the freshness bound (§5.4)
    sheet_version_grace_s: float = 900.0               # superseded incomplete versions swept after this (§5.4)
    ffmpeg_concurrency: int = 3                         # semaphore width (matches wildlife)

class ProxySection(BaseModel):
    enabled: bool = True
    pass_request_headers: list[str] = ["range", "authorization", "cookie"]
```

Add `scrub: ScrubSection` and `proxy: ProxySection` to `Settings`, and update `config/sidecar.example.yml` with commented defaults (following the existing heavily-commented style).

---

## 8. Storage & DB

### 8.1 Sidecar DB schema — extend `SIDECAR_SCHEMA` in `db.py`

Two tables, appended to the existing `executescript` block (same style as `triage_labels`, `face_attempts`, `toybox_scores`):

```sql
CREATE TABLE IF NOT EXISTS scrub_buckets (
    camera            TEXT NOT NULL,
    start_ts          REAL NOT NULL,        -- inclusive
    end_ts            REAL NOT NULL,        -- exclusive; grows as the live bucket fills
    interval_s        REAL NOT NULL,        -- the hard-contract cadence
    width             INTEGER NOT NULL,
    height            INTEGER NOT NULL,
    generated_through REAL NOT NULL,        -- newest moment with a frame behind it
    complete          INTEGER NOT NULL DEFAULT 0,  -- 1 once end_ts is final & immutable
    PRIMARY KEY (camera, start_ts, interval_s)
);
CREATE INDEX IF NOT EXISTS idx_scrub_bucket_cam ON scrub_buckets(camera, start_ts);

CREATE TABLE IF NOT EXISTS scrub_sheets (
    camera    TEXT NOT NULL,
    start_ts  REAL NOT NULL,                -- first cell's wall-clock; also the filename key
    interval_s REAL NOT NULL,
    cols      INTEGER NOT NULL,
    rows      INTEGER NOT NULL,
    cell_w    INTEGER NOT NULL,
    cell_h    INTEGER NOT NULL,
    count     INTEGER NOT NULL,             -- filled cells (< cols*rows for the live sheet)
    path      TEXT NOT NULL,                -- on-disk relative path under scrub.cache_dir
    complete  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera, start_ts, interval_s)
);
CREATE INDEX IF NOT EXISTS idx_scrub_sheet_cam ON scrub_sheets(camera, start_ts);
```

`recorded` / `published_through` for §4.4 are **not** stored here — they're read live from `frigate.db`'s `recordings` table so they never drift from reality.

### 8.2 On-disk layout & path mapping

```
{scrub.cache_dir}/{camera}/{interval}/{sheet_start}-{interval}-{count}.jpg
```

**Corrected from the earlier draft**, which wrote `{bucket_start}.webp` — that conflates the *bucket's* start with the *sheet's* start. A bucket spans up to `retention_days` at one interval; a sheet holds only ~96 cells (≈2 min at 1 fps), so one bucket is covered by roughly thirty sheets, each with its own `start_ts` per `scrub_sheets` (§8.1). The on-disk filename must key off the sheet, and — per the immutable-URL fix in §4.3 — must include `count` too, matching the URL exactly.

Content-addressed by (camera, interval, sheet start, count) → immutable once complete → the immutable cache header applies and the sheet URL is stable forever.

**Path mapping — two-part rewrite, deployment-specific (M6, measured live):** `recordings.path` in `frigate.db` holds Frigate's *container* path, e.g. `/media/frigate/recordings/2026-07-30/14/alley-wide/30.44.mp4`. On the current deployment, `docker inspect frigate` shows that container path bind-mounted from a host directory that itself contains a **nested `recordings/` segment** — the real host path is `/mnt/frigate-storage/recordings/recordings/2026-07-30/...`, confirmed by directly reading a segment at that path. This is not a simple 1:1 substitution and must not be assumed one:

1. Strip the `frigate.media_path` prefix (the DB's container-side root, e.g. `/media/frigate`) from `recordings.path`.
2. Reattach `frigate.recordings_path` (the sidecar's own host-side path to the same tree, §7) — which may itself have deployment-specific quirks like the nested `recordings/` segment above.

Both fields are now explicit config (§7), not inferred. Get the correct value for a new deployment by the same method used here: `docker inspect <frigate-container> --format '{{json .Mounts}}'` on the Frigate host, then confirm with a direct `ls` of a `recordings.path` row rewritten through the candidate prefix.

### 8.3 Cache placement — must be a separate filesystem (hard requirement)

**Measured (M5, 2026-07-30):** the filesystem backing Frigate's own recordings (`/mnt/frigate-storage/recordings`, which is what `/media/frigate` bind-mounts from) is **99% full — 27 GB free** on the live deployment. This is worse than the client-side review's own snapshot (93% full / 124 GB free) taken hours earlier; free space on this volume is actively shrinking. Even the corrected ~22 GB disk estimate (§5.5) would consume nearly all remaining headroom and risk Frigate itself hitting `ENOSPC` on its own recordings.

**`scrub.cache_dir` must be provisioned on a separate filesystem from Frigate's recordings volume before this ships.** This was "worth stating" in the earlier draft; the current measurement makes it a hard blocker for deployment, independent of the code.

**Resolved (2026-07-30):** the box's own root filesystem (`/dev/mapper/Samsung2TB-vm--105--disk--0`, mounted at `/`) is a separate device from `/mnt/frigate-storage/recordings` (`/dev/sdb1`) — different `st_dev`, and currently **29% used / 64 GB free**, comfortably ahead of the ~22 GB estimate. `/data` already exists at the root of this filesystem and is where the sidecar keeps its own sqlite DB (`/data/frigate-sidecar.db`) today, so `scrub.cache_dir: /data/scrub` (the §7 default) needs no new volume, network share, or disk — it lands on already-separate, already-provisioned storage. No deployment prerequisite remains here.

Still verify at startup: if `scrub.cache_dir` resolves to the same filesystem as `frigate.recordings_path` (compare `st_dev`), log loudly and refuse to enable `scrub.enabled` rather than silently competing with Frigate for the last few GB. This guards future deployments where the assumption doesn't hold, not this one.

---

## 9. Deployment changes

### 9.1 `/media` read-only mount

`docker-compose.yml` gains one volume (RO), matching the existing `config.yml`/`frigate.db` mounts:

```yaml
    volumes:
      - /opt/frigate/config.yml:/opt/frigate/config.yml:ro
      - /opt/frigate/database/frigate.db:/opt/frigate/database/frigate.db:ro
      - /mnt/frigate-storage/recordings/recordings:/media/frigate:ro   # NEW — recordings, RO (M6: real host path,
                                                                          # note the nested recordings/ segment)
      - /path/to/separate-volume/scrub:/data/scrub                     # sheets live here — MUST be a different
                                                                          # filesystem than the recordings mount
                                                                          # above (§8.3, hard requirement — the
                                                                          # recordings volume is 99% full)
      - ./config/sidecar.yml:/etc/frigate-sidecar/sidecar.yml:ro
```

For the **systemd/LXC deployment** (the one actually in use — an editable install restarted via `systemctl restart frigate-sidecar.service`), the recordings path is a host path already present; point `frigate.recordings_path` directly at `/mnt/frigate-storage/recordings/recordings` (M6). No mount needed, but `scrub.cache_dir` still needs to land on a volume other than that one.

### 9.2 ffmpeg must be present — confirmed missing, now blocking for both features

`wildlife.py` already shells out to `ffmpeg`, and the generator depends on it. Confirmed live (M7, 2026-07-30): `ffmpeg`/`ffprobe` are present on the Frigate host itself but **absent from this repo's own runtime environment**, and the current `Dockerfile` (`python:3.12-slim`) does not install them — so the Docker path needs `RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg`. This is not a hypothetical gap: `wildlife.py` is *already broken* in the container image today for exactly this reason. Fixing the Dockerfile is now a **blocker for this PR**, not a side note, since scrub-cache generation depends on it too. The systemd/LXC deployment relies on host ffmpeg, which is present there.

### 9.3 Generator process

If §5.4(a) in-process: nothing new to deploy — the FastAPI lifespan starts the loop. If §5.4(b) systemd timer: add `contrib/frigate-sidecar-scrub.timer` + `.service`, cloning `contrib/frigate-sidecar-faces.{timer,service}`, running `fsc scrub generate` every 60 s.

### 9.4 HTTP/2 at the edge

Enable HTTP/2 on whatever fronts `:5001` (the existing NPM/Nginx TLS terminator is the natural place — §6.5). One config line. **Not advertised via capabilities** (§4.1) — `URLSession` negotiates it itself and the flag can only be wrong.

---

## 10. Fallback / degradation (the "optional always" guarantee)

Every capability degrades cleanly:

| Sidecar state | Reel behaviour |
|---|---|
| `/v1/capabilities` 404 or unreachable | Full bare-Frigate path: preview WebP frames (current hour) + `preview.mp4` (older), motion via `/api/review/activity/motion`, coverage via `/api/{cam}/recordings`. The reel already does this today. |
| `scrub_cache.enabled=false` | Proxy + `/v1/coverage`/`/v1/reel` still help; frames fall back to the two-source model. |
| Camera not in `scrub_cache.cameras` | That camera uses the bare-Frigate frame path; others use sheets. |
| `scrub_cache.generated=false` (backfilling) | Reel shows a "generating" state for spans without buckets rather than "no data". |
| Proxy disabled | App keeps talking to Frigate directly for `/api`,`/vod` (two origins), `/v1` still available if reachable. |

The reel must never *require* a `/v1` endpoint to be usable. This is the load-bearing constraint behind versioning from endpoint one and the capability probe.

---

## 11. Testing

Follow the existing `tests/` conventions (`test_api.py`, `test_sampler.py`, `test_recorder.py`, `conftest.py` fixtures; `pytest` + `pytest-asyncio`, already configured).

- **`test_scrub_generate`** — given a fixture segment (a tiny checked-in mp4) and a fake `recordings` row, assert the generator writes a sheet with the declared `count`, that cell `k`'s **achieved timestamp is within `interval / 2` of the grid point** `start + k·interval` (not just approximately right — assert the bound), and that a gap **splits the bucket** rather than fudging the interval. This is the highest-value test in the suite per the client-side review — the cadence guarantee is silent until two series collide on one cell.
- **`test_scrub_coverage`** — bucket rows → coverage JSON: interval contract holds, `generated_through` is the true edge, aged buckets carry the coarser interval, a span past `retention_days` is distinguishable from a lagging one.
- **`test_scrub_sheet_url_immutable`** — two generations of the same live (still-filling) sheet produce two different URLs (differing `count`); both are served with an unconditional `immutable` header; no cache-freshness logic exists anywhere in the sheet-serving path.
- **`test_reel_bundle`** — one call returns `queried`/`recorded`/`latest_segment_end`/`authoritative_through`/`frames`/`motion`/`events`; `frames` is an array of descriptors (not a single one) when the window straddles the thinning boundary; `motion.values` is a bare array on the declared grid, zero-filled to the full requested range; `queried` ≠ `recorded` when the window overhangs available footage; an in-progress event serializes `end: null`.
- **`test_authoritative_through_survives_camera_outage`** — a camera with no new segments for N minutes: `latest_segment_end` stays frozen at the last real segment, but `authoritative_through` keeps advancing at wall-clock rate — the divergence between the two is the outage signal, and the test asserts it doesn't collapse back to one field.
- **`test_motion_totalizes`** — a short-window / coarse-scale upstream response is aggregated & zero-filled to cover the *full* requested range (the two measured cliffs).
- **`test_proxy_passthrough`** — `Range` and `Authorization` forwarded; `206`/`401`/`404` mirrored; `..` traversal rejected; `set-cookie`/`etag` relayed. (Mock the upstream with `httpx` transport, as the wildlife proxy tests would.)
- **Contract-hygiene asserts** — unknown `/v1` path → JSON 404 (never HTML 200); immutable header on completed sheets; ETag → 304 on `If-None-Match`; all times float.
- **Reuse the Elsinore JSON fixtures** (`FrigateKit/Tests/.../Fixtures/*.json`: `preview_frames_sample.json`, `activity_motion_sample.json`, `events_window_sample.json`) as golden inputs so the two sides agree on shape.

---

## 12. Build-shaping decisions (mostly resolved; kept as the record)

The subsystem has shipped and survived a production repair cycle, so these are no longer awaiting sign-off — they're kept as the record of what was decided and why. The first two (from §3) changed the build shape; the rest were the requirements doc's open questions. Only #5 (default camera set) and #6 (backfill mode) remain genuinely undecided, and the shipped behavior stands in as the answer until someone cares.

1. **Generation source** — recordings via `/media` RO mount (recommended: uniform + no hourly hole) vs HTTP-only preview (no mount, but can't promise clean cadence). §3.1. **Unchanged — still recording segments.**
2. **Proxy target & `/v1` auth** — proxy to authed `:8971` with cookie pass-through. **`/v1` auth is now resolved, not open: authenticate it (option (a)), per §3.2.** The earlier "trust the LAN" option would have made the sidecar a way around Frigate's own auth for footage stills.
3. **Disk budget** — **resolved by measurement:** ~22 GB (10 cams × 4 d × 1 fps, JPEG), not the earlier 50–60 GB estimate — both the camera count (8→10) and the retention window (14→4 days) were wrong. §5.5. **Cache placement also resolved:** the box's root filesystem (separate device from the 99%-full recordings volume) has 64 GB free at `/data`, already used by the sidecar's own DB — `scrub.cache_dir: /data/scrub` needs no new volume (§8.3).
4. **Retention independent of `record.retain.days`?** **Resolved: yes, but capped.** The schema's `retention_days` is ours to set, but it cannot exceed what continuous recording actually provides (~4 days, measured, §5.5) regardless of how much disk is available.
5. **All cameras or a chosen set?** **Ten** cameras (corrected count, §5.5.1) of always-on ffmpeg, keyframe-only decode: ~18 CPU-hours/day fleet-wide (M2). `scrub.cameras` + `capabilities.scrub_cache.cameras` support a per-camera opt-in; still open whether the default is all-10 or an explicit opt-in list — the CPU number now exists to decide it with.
6. **Backfill on first run** — batch the whole retention window, or forward-only + fill history lazily? Drives whether the reel shows "generating". Still open. §5.7.
7. **Sheet span / cell size** — 96 cells (12×8) at 320×180 ≈ 2 min/sheet, **~21 MB decoded** (measured/computed precisely: 3840×1440 RGBA), client holds **two** (matches client-side review's own memory budget, tightened from "2–3"). §4.3.

---

## 13. Suggested build order

Tier 0 first — it's what changes the app. Each step is independently shippable and degrades cleanly if the next never lands.

1. **Proxy** (`routes/proxy.py`) + `frigate.proxy_base_url` — one origin. Cheapest, unblocks the on-release VOD handoff immediately, and is a near-copy of the wildlife proxy. Turn on **HTTP/2** at the edge in the same step (§6.5). **Done (2026-07-30).**
2. **`/v1/capabilities`** + **`/v1/coverage`** (reads `frigate.db`, no generation yet) — kills the four "no recording vs not looked" bugs on their own, before any sprite exists. **Done (2026-07-30).**
3. **Generator → JPEG sheets** (`routes/scrub.py`, `scrub` CLI, DB tables, continuous 60 s loop) + **`/v1/scrub/{camera}/coverage`** and `/sheets` — the uniform-cadence cache. Ship JPEG (`format:"jpeg"`); the contract doesn't change when WebP tiling arrives. **Done (2026-07-30)** — `src/marcellus/scrub/` (grid.py, mapping.py, motion.py, ffmpeg_io.py, tiling.py, generator.py), `fsc scrub generate/backfill/prune/coverage`, continuous ~60s asyncio task in `server.py`'s lifespan. **Caveat:** the dev sandbox this was built in has no `ffmpeg`/`ffprobe` on PATH, so the ffmpeg-subprocess layer (`scrub/ffmpeg_io.py`) is exercised only by mocking it in tests, not against a real segment — verify `probe_gop_seconds`/`extract_keyframes_with_pts`/`extract_fps` against a real Frigate recording before enabling `scrub.enabled` in production. The cadence-verification and gap-splitting logic itself (the highest-value test, §11) *is* tested for real, against synthetic frame series (`tests/test_scrub_grid.py`).
4. **`/v1/reel`** + **`/v1/motion`** — collapse the fan-out, totalize motion. **Done (2026-07-30).**
5. **WebP tiling** (flip `format`), **retention/thinning**, **prune**, **decode + derived tiers** (§5.8). **Done.** `scrub/tiling.py::tile_sheet_webp` (`lossless=False` hardcoded, matching the mandatory `-lossless 0`), `fsc scrub prune [--drop-interval]` + the generator's own cutoff. The two decode tiers (`recent_interval_s`/`aged_interval_s`, GOP-driven, ffmpeg) are both implemented and thin at `aged_after_h`, per §5.5. On top of them, `derived_intervals_s` (default `[60, 300, 900, 3600]`) generates further tiers by decimating the decode tiers' own published sheets — no additional ffmpeg cost — run last in each cycle, after live-edge and backfill, out of leftover deadline budget. `retention_days`-based pruning applies uniformly across every tier, decode or derived.
6. **`/v1/highlights`** (Tier 1 feature). **Done (2026-07-30)** — precomputed from `event` rows (`label` as `reason`, `top_score`/`score` as `score`).

Tier 2 (push over MQTT+APNs, a unified `/ws` change feed, QR pairing, off-LAN relay) is already owned elsewhere in Elsinore PROJECT_PLAN §6 and out of scope here — but the proxy and capability probe built above are its foundation.
