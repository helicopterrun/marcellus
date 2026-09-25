"""Sidecar configuration: YAML file + env-var overrides.

Loading precedence (highest wins):
1. Environment variables prefixed MARCELLUS_ (nested with __). Legacy
   FRIGATE_SIDECAR_ vars are still honoured as a fallback -- see
   `_apply_legacy_env_fallback` below.
2. YAML file at MARCELLUS_CONFIG, or the default search path.
3. Defaults defined on the models below.
"""

from __future__ import annotations

import logging
import os
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_ENV_PREFIX = "MARCELLUS_"
# Pre-rename env prefix (was frigate-sidecar). Still honoured, with a
# deprecation warning, so an existing Docker deployment's env vars keep
# working -- see `_apply_legacy_env_fallback`.
_LEGACY_ENV_PREFIX = "FRIGATE_SIDECAR_"

DEFAULT_CONFIG_PATHS = (
    "/etc/marcellus/sidecar.yml",
    "/etc/frigate-sidecar/sidecar.yml",  # legacy fallback
    "./config/sidecar.yml",
)

logger = logging.getLogger(__name__)


class FrigateSection(BaseModel):
    base_url: str = "http://frigate.lan:5000"
    config_path: Path = Path("/opt/frigate/config.yml")
    # Allow POST /v1/push/frigate-config/refresh to overwrite `config_path`.
    # Default OFF: on the bare-metal install config_path is Frigate's LIVE
    # config.yml, and a refresh would replace it wholesale. Only enable on a
    # deployment where config_path is a sidecar-owned snapshot (dev).
    config_refresh_enabled: bool = False
    db_path: Path = Path("/opt/frigate/database/frigate.db")
    # Authed origin used ONLY to proxy app traffic (routes/proxy.py). Auth stays
    # entirely Frigate's — the sidecar forwards the client's own cookie and never
    # holds a password. Deliberately separate from base_url (unauth, sidecar's
    # own server-to-server calls) — do not merge them (docs/scrub-cache-and-proxy-spec.md §3.2).
    proxy_base_url: str = "http://frigate.lan:8971"
    # DB's container-side recordings root, as stored in recordings.path. Used
    # only to strip this prefix before reattaching recordings_path (§8.2).
    media_path: Path = Path("/media/frigate")
    # Host-side path that `media_path` is REPLACED BY, not the recordings tree
    # root. `recordings.path` is `<media_path>/recordings/<date>/...`, so the
    # `recordings/` segment comes from the DB value and must not be repeated
    # here: pointing this at .../recordings/recordings mapped every segment to a
    # path one level too deep, every file lookup missed, and generation produced
    # nothing at all (§8.2 M6). Verify with:
    #   python -c "from marcellus.scrub.mapping import map_recording_path"
    # against a real `recordings.path` row before trusting a new value.
    recordings_path: Path = Path("/mnt/frigate-storage/recordings")


class SidecarSection(BaseModel):
    db_path: Path = Path("/data/marcellus.db")
    bind_host: str = "0.0.0.0"
    bind_port: int = 5001
    # Every endpoint the sidecar owns -- the triage UI, /faces/captures, /analysis,
    # /toybox, /v1 -- requires the same Frigate session cookie the proxy
    # already forwards to Frigate. On by default: the sidecar sits on the same
    # LAN origin as Frigate and exposes event history, face crops and
    # label/promote writes, so leaving it open makes it a bypass of Frigate's
    # own auth. The proxy catch-all is deliberately NOT gated here (Frigate
    # authenticates that traffic itself, and its 401 must reach the client).
    # Set false only on a deployment where Frigate's own auth is disabled.
    require_frigate_auth: bool = True
    # A cookie that validated upstream is trusted this long before being
    # re-checked, so /v1 doesn't add a round-trip per request.
    auth_cache_ttl_s: float = 60.0
    # Hard cap on remembered sessions (Frigate rotates its JWT, so the key
    # space is unbounded without one).
    auth_cache_max_entries: int = 1024
    # "Stay signed in" lifetime for the sidecar's own remember-me cookie,
    # minted by POST /login/remember after a successful Frigate login. The
    # sidecar never stores the password — the cookie is a signed expiry
    # token, so revocation is "wait for expiry or rotate the secret file".
    remember_ttl_s: float = 30 * 86400.0
    # Auth-cache window granted to a request carrying a valid remember-me
    # cookie, in place of auth_cache_ttl_s. Longer, because the device has
    # proved a Frigate session and ticked "stay signed in" — but still
    # bounded, so disabling the Frigate account cuts the holder off within
    # this window rather than at remember_ttl_s.
    remember_cache_ttl_s: float = 900.0
    # Per-IP failed login attempt limit -- counts only failed POSTs to
    # /api/login proxied through (auth.py's LoginRateLimiter). Deliberately
    # does NOT count 401s from validate_frigate_session on owned routes: the
    # iOS app fires several requests in parallel on an expired cookie, and
    # guessing a valid one is infeasible (HMAC-signed) anyway. `0` disables
    # the limiter entirely. Client IP is `scope["client"][0]`; nothing sits
    # in front of uvicorn so X-Forwarded-For is never trusted.
    login_rate_limit_attempts: int = 10
    # Sliding window, in seconds, the attempts above are counted over.
    login_rate_limit_window_s: float = 60.0
    # JSON file of runtime overrides written by /settings' Tuning panel
    # (`marcellus.tuning`) -- same runtime-data class and directory as
    # `push.push_settings_path`: not part of the YAML/env config surface,
    # created on first PUT, read back on every `load_settings` (including
    # the CLI, the watchdog, and the face_capture timer processes) so an
    # override applies everywhere, not just to the running web process.
    tuning_path: str = "config/tuning.json"


class FaceCaptureSection(BaseModel):
    """High-res cross-camera face capture (B2).

    When a `person` event fires on a *trigger* camera, pull the *capture*
    camera's full main-stream frame out of Frigate's recordings at that moment
    and park it under `output_dir` for human review at /faces/captures.

    The whole feature is one HTTP GET against `frigate.base_url`:
    `/api/{camera}/recordings/{unix_ts:.3f}/snapshot.jpg` returns the full
    main-stream frame (2560x1440, ~330 KB, ~0.45s on gate-face -- verified
    live) with no ffmpeg, no -ss seek, no recordings-table lookup and no
    container->host path mapping. Prior art: analysis/annotation_offset.py.

    This section never touches Frigate's Face Library.
    """

    enabled: bool = False

    # Cameras whose person events are worth a face grab. Empty means the
    # feature does nothing even when enabled -- deliberately NOT "all cameras",
    # which would grab a frame for every sidewalk pedestrian (the capture
    # camera alone fires ~166 person events a day here).
    trigger_cameras: list[str] = Field(default_factory=list)

    # The single supporting identification camera whose main stream we grab.
    # One camera, not a list: the point is an angle the triggers don't have.
    capture_camera: str = ""

    # `person` alone: a package or car event has no face to capture.
    trigger_labels: list[str] = Field(default_factory=lambda: ["person"])

    # Sample offsets from the trigger event's start_time, seconds. The capture
    # camera's OWN person event starts a median 2.2s BEFORE the trigger's
    # (p25 -6.8, p75 +5.7, measured over the six days both cameras have event
    # history) -- the visitor crosses the sidewalk on the way in. A single
    # sample at t=0 misses that pass often enough to matter; three samples 4s
    # apart cover the p25..p75 band for ~1 MB per visit.
    offsets_s: list[float] = Field(default_factory=lambda: [-4.0, 0.0, 4.0])

    # How long after a sample timestamp before we try to fetch it. THE
    # load-bearing setting: recordings/{ts}/snapshot.jpg 404s until the segment
    # covering `ts` has been COMMITTED, and segments commit at their END.
    # Measured publish lag here is 5.4-9.4s per camera (db.DEFAULT_PUBLISH_LAG_S
    # = 6.2 is the reference constant). Verified live: ts=now-5s -> 404,
    # ts=now-60s -> 200. 45s is ~4x the worst observed lag and costs nothing --
    # this is not an interrupt path, which is also why an MQTT "grab it now"
    # hook would 404 essentially every time.
    capture_delay_s: float = 45.0

    # How far back each run reconsiders trigger events. An hour of overlap makes
    # the job self-healing: a run skipped by a restart, or one that hit a
    # transient error, is picked up by the next with no cursor or queue state.
    # Idempotency comes from the UNIQUE index, not from this window.
    lookback_s: float = 3600.0

    # Consecutive trigger events within this gap are ONE visit; only the first
    # is captured. Real visits fire on 3-4 cameras within seconds: 325
    # front-camera person events over 30 days collapse to 154 visits at a 60s
    # gap (87 singletons, largest cluster 26 events). Without this, one visitor
    # costs 26 x 3 = 78 full-res grabs and 78 review cards.
    dedup_window_s: float = 60.0

    # Hard ceiling on a gap-chained visit. Pure gap-chaining runs forever while
    # someone loiters; capping at 5 minutes means a long presence yields a fresh
    # capture every 5 minutes instead of one frame from the first second.
    max_visit_s: float = 300.0

    # Bound on one run, so a first run against a long lookback cannot hold the
    # manual POST open for minutes or hammer Frigate. 60 captures ~= 27s of
    # upstream time at the measured 0.45s.
    max_captures_per_run: int = 60

    # Transport failures (status='error') are retried on later runs; a clean 404
    # (status='no_recording') never is. This bounds the retries so a permanently
    # unreachable Frigate cannot spin.
    max_attempts: int = 3

    # Fold in the TRIGGER camera's detect.annotation_offset from Frigate's
    # config. Convention, per analysis/annotation_offset.py's _measure_event
    # (which probes recordings at `t_det + off_ms/1000`):
    #     recording_time = detection_time + annotation_offset_ms / 1000
    # doorbell's -1000 means its detections lead the video by 1.0s. The +-4s
    # ladder mostly swamps a 1-3s correction, so this is a refinement -- but
    # getting the sign wrong silently is not, hence the citation.
    apply_annotation_offset: bool = True

    # Crop the PREVIEW to the head, using the CAPTURE camera's OWN concurrent
    # event box. The trigger camera's box is NOT usable and is never consulted:
    # different camera, different FOV, and different detect aspect (doorbell
    # 960x720 and package 1280x960 are 4:3; the capture camera is 16:9, exactly
    # 2x the fetched frame, so ITS normalized coords map straight on). Only ~73%
    # of trigger events have a concurrent capture-camera event, and only a box
    # LIVE at the sample instant is used -- a box borrowed from 20s away crops
    # the wrong part of the frame, worse than no crop. The full frame is always
    # kept, so a bad crop costs a thumbnail, never the evidence.
    crop_to_bbox: bool = True

    # Fraction of the person box's height, from its top, taken as the head.
    # A capture-camera person box measures ~428x856 px on the 2560x1440 frame,
    # so 0.4 yields ~428x342 -- face plus shoulders. Deliberately loose:
    # data.box is the box at event END, not at the sample instant, so the crop
    # has to absorb real drift.
    head_fraction: float = 0.4
    crop_pad: float = 0.25  # expand the head box by this fraction of its own w/h

    # Preview longest edge / quality. ~480px keeps a 60-card grid under ~2 MB
    # while staying big enough to recognise a face without opening the full frame.
    thumb_max_edge: int = 480
    thumb_quality: int = 80

    # MUST be under a path the systemd unit can write. The service runs
    # ProtectSystem=strict with ReadWritePaths=/opt/marcellus only, so
    # anything outside fails EROFS *at write time*, not at config load -- a
    # silent no-op. check_inputs() probes writability to make that loud.
    output_dir: Path = Path("/opt/marcellus/data/face-captures")

    # ~5 visits/day x 3 samples x ~330 KB = ~5 MB/day, so 30 days is ~150 MB.
    # Matched to Frigate's record.alerts.retain.days: 30 so a capture never
    # outlives the footage it was cut from.
    retention_days: int = 30

    http_timeout_s: float = 15.0


class FaceEnrichSection(BaseModel):
    """Face enrichment (B3): embeddings, temporal aggregation, clustering.

    For each ended `person` event on an enrolled camera, sample full-res
    frames from the recording (the same recordings/{ts}/snapshot.jpg endpoint
    `face_capture` uses), detect faces + landmarks, quality-score them
    (sharpness x size x frontality), embed the best N with ArcFace, aggregate
    into one embedding per event, then either match a NAMED cluster (-> write
    the event's sub_label back to Frigate) or fold it into an unnamed cluster.
    Unnamed clusters accumulate recurring strangers and are promotable to
    known people by naming them at /enrich/clusters.

    Needs the `[enrich]` extra (insightface + onnxruntime + cv2/numpy).
    Inference is CPU-only by design: the OpenVINO/iGPU path produced a
    multi-day embeddings OOM cycle on this host and is not to be revisited.

    Like `face_capture`, work is found by a lookback query over Frigate's
    events table rather than an MQTT hook: recordings commit at segment END
    (measured lag 5.4-9.4s), so the work is inherently deferred and the
    lookback makes it self-healing across restarts with idempotency coming
    from the `face_enrichments` table, not from cursor state.
    """

    enabled: bool = False

    # Cameras whose person events get enriched. gate-face is the designated
    # identification camera; empty list = feature does nothing even if enabled.
    cameras: list[str] = Field(default_factory=list)

    # Worker cadence. Enrichment is offline — seconds of latency is fine.
    interval_s: float = 15.0

    # Don't process an event until this long after its END: the last frames of
    # the event live in a segment that commits ~5-10s after the fact
    # (face_capture.capture_delay_s has the full measurement story).
    process_delay_s: float = 45.0

    # How far back each cycle reconsiders ended events. Self-healing overlap;
    # idempotency comes from face_enrichments, not from this window.
    lookback_s: float = 3600.0

    # Frame sampling across the event window: at most `max_frames`, at least
    # `min_sample_gap_s` apart. 40 frames of a long loiter is plenty; a 5s
    # walk-through yields ~5.
    max_frames: int = 40
    min_sample_gap_s: float = 1.0

    # Quality gates and the best-N cut feeding aggregation.
    best_n: int = 5
    # Face box area in px on the 2560x1440 main-stream frame. A capture-camera
    # face at identification distance measures well above this; sidewalk
    # passers-by mostly fall below it.
    min_face_area_px: int = 4000
    min_quality: float = 0.15

    # Cosine DISTANCE thresholds (1 - cosine similarity) on L2-normalized
    # ArcFace embeddings. Named clusters get the tighter bar: a wrong
    # sub_label is worse than a missed one.
    match_threshold: float = 0.45
    cluster_threshold: float = 0.55

    # Unnamed clusters not seen for this long are reaped (with their stored
    # embeddings). Named clusters never expire.
    cluster_ttl_days: int = 60

    # insightface model-pack cache (buffalo_l, ~300 MB on first download).
    # Must be writable by the service user under ProtectSystem=strict.
    model_dir: Path = Path("/opt/marcellus/data/models")

    # Bounds one cycle so a first run against a long lookback can't hog the
    # shared 4-core host; the lookback picks up the rest next cycle.
    max_events_per_cycle: int = 10

    # Transport/inference failures are retried on later cycles up to this cap;
    # a processed event (even "no faces found") is terminal.
    max_attempts: int = 3

    http_timeout_s: float = 15.0


class WatchdogSection(BaseModel):
    """External health watchdog for the Frigate container.

    Polls Frigate's HTTP API and restarts the container when its backend hangs
    — connection-refused or repeated 5xx. That's the failure mode Docker's own
    restart policy can't catch: Frigate's main process can wedge on a frozen
    camera stream while its s6 PID 1 stays alive, so the container reads "Up"
    but every /api/* request 500s through nginx. Off by default; runs as its
    own process via contrib/frigate-watchdog.service (not inside the web app,
    so it survives even if uvicorn's event loop is blocked).
    """

    enabled: bool = False
    # Probed as frigate.base_url + probe_path. /api/version is the cheapest
    # endpoint that still 500s when the backend is hung; it returns 200 even in
    # safe mode, so a bad-config safe-mode boot will NOT trigger a restart loop.
    probe_path: str = "/api/version"
    interval_s: float = 30.0
    timeout_s: float = 10.0
    # Consecutive failed probes before a restart. 4 × 30s = ~2 min of sustained
    # failure, so a brief blip or a single slow probe won't trip it.
    failures_before_restart: int = 4
    restart_command: list[str] = Field(default_factory=lambda: ["docker", "restart", "frigate"])
    restart_timeout_s: float = 120.0
    # After a restart, ignore failures for this long so Frigate's boot (during
    # which probes naturally fail) can't trigger a second restart mid-startup.
    cooldown_s: float = 180.0
    # Safety cap: if Frigate is fundamentally broken, stop hammering it and log
    # loudly for manual intervention instead of restart-looping forever.
    max_restarts_per_hour: int = 3


class ScrubSection(BaseModel):
    """Uniform-cadence sprite-sheet scrub cache (docs/scrub-cache-and-proxy-spec.md).

    Off by default; opt-in per deployment. `retention_days` is capped by how
    long continuous (non-motion-only) recording actually lasts on this
    deployment -- measured at ~4 days, not the record.retain.days config value.
    """

    enabled: bool = False
    cameras: list[str] = Field(default_factory=list)  # [] = all cameras
    cache_dir: Path = Path("/data/scrub")  # MUST be a separate filesystem from
    # frigate.recordings_path -- verified at startup, see routes/scrub.py.
    # Floor on free space (bytes) on the cache filesystem below which the
    # generation loop skips its cycle for that tick rather than grinding out
    # the same ENOSPC failure every tick forever. Pruning still runs on its
    # own cadence regardless -- it's what frees the space back up. ~2GB is
    # comfortably above one sheet's worst-case size (§5.3 M4 measured ~1.1MB)
    # with headroom for a burst of in-flight temp files.
    min_free_bytes: int = 2 * 1024 * 1024 * 1024
    recent_interval_s: float = 1.0
    aged_interval_s: float = 5.0
    # Generate a camera at its own keyframe cadence when that is coarser than
    # `recent_interval_s`, instead of full-decoding to force the configured
    # rate. A source whose GOP is longer than the target interval can only hit
    # that interval by decoding every frame -- measured at ~5x the cost of
    # keyframe extraction, and on the reference deployment the three UniFi
    # Protect cameras (5s GOP, against 1s on the seven Dahua ones) accounted for
    # roughly 70% of the generator's total work while being 30% of the fleet.
    # The cadence is per bucket and travels to the client in `interval`, so a
    # camera generating at 5s is contract-compatible; it just yields a still
    # every 5s rather than every second. Turn off to force the configured
    # interval everywhere and pay the decode.
    match_keyframe_cadence: bool = True
    # Tiers derived from the decode tiers (recent/aged) rather than sampled
    # with ffmpeg: each is generated by picking every Nth already-published
    # cell out of whichever decode tier is finest over a given span, and
    # re-tiling -- cheap disk I/O and PIL crops, no decode cost at all. Runs
    # last in each generation cycle (after live-edge and backfill), out of
    # whatever's left of the tick's deadline. An empty list disables this. The
    # default keeps 60s ("scrub a day back"), 300s and 900s, and 3600s ("scrub
    # the whole retention window") cadences on top of the recent/aged pair,
    # which is otherwise unchanged.
    derived_intervals_s: list[float] = Field(default_factory=lambda: [60.0, 300.0, 900.0, 3600.0])
    aged_after_h: float = 24.0
    retention_days: int = 4
    cell_w: int = 320
    # Fallback height, used only when the source's shape can't be measured or
    # `preserve_source_aspect` is off. Otherwise the height is derived per
    # camera from the source's display aspect ratio.
    cell_h: int = 180
    # Derive each camera's cell height from its own aspect ratio instead of
    # scaling every source into a fixed cell. A 4:3 camera rendered into a 16:9
    # cell comes out anamorphically squeezed, and nothing downstream can undo it
    # -- the pixels are already wrong. Two of the ten cameras here are 1600x1200.
    # The resulting dimensions travel per sheet in the `cell_w`/`cell_h`
    # metadata, so a client reading those renders each camera correctly.
    preserve_source_aspect: bool = True
    sheet_cols: int = 12
    sheet_rows: int = 8
    format: str = "jpeg"  # "jpeg" | "webp" -- JPEG measured smaller on real
    # camera content (see docs spec §5.3 M4); WebP requires -lossless 0.
    generate_interval_s: float = 60.0  # continuous edge, NOT hourly (§5.4)
    # How often the trailing-window pass runs, and therefore the generation
    # loop's tick. This is the floor on how stale the newest sprite cell can be,
    # because a camera serviced at the start of one tick is not touched again
    # until the next.
    #
    # It exists because the tick used to be `generate_interval_s` with the
    # backfill phase inside it: backfill's own budget landed on top of a
    # live-edge pass that had grown to ~65s (it fed several tiers from the
    # same decode at the time), so the effective cadence was ~100s and
    # measured lag ~105s -- past the ~90s
    # the client is told to expect, and past it *further* whenever a slow
    # segment stretched the cycle. Backfill is now bounded by the next tick
    # rather than the tick being bounded by backfill.
    #
    # Throughput is unchanged by shortening it: the same segments are decoded
    # either way, just in smaller instalments, so latency improves at equal CPU.
    # What it does cost is sheet versions -- a still-filling sheet is published
    # once per tick, and every version is its own immutable object (§4.3) --
    # which is what `sheet_version_grace_s` sweeps back up.
    live_edge_interval_s: float = 20.0
    # How long a *superseded* still-filling sheet version stays servable after a
    # larger version of the same sheet is published. Complete sheets are never
    # superseded and are never swept by this; retention alone removes those.
    #
    # Without a sweep, a 96-cell 1s sheet publishes ~5 growing versions at the
    # default tick and all of them live until retention: measured at ~1.1 MB for
    # a full sheet on real footage, that is roughly 3x the tier's steady-state
    # size, on the filesystem §8.3 goes out of its way to keep free. The grace
    # window is what keeps the sweep safe -- a client holding a URL from its last
    # index fetch still resolves it; one holding a 15-minute-old URL gets a 404
    # and falls back, which is the same path it already takes for a span with no
    # coverage.
    sheet_version_grace_s: float = 900.0
    # Retention sweep cadence for the in-process generator. Pruning used to be
    # CLI-only, so an unattended deployment grew past retention_days forever.
    prune_interval_s: float = 3600.0
    # Bounds concurrent ffmpeg/ffprobe children. Segments within a camera are
    # sampled serially (cell assignment is order-dependent), so today this only
    # matters if a CLI backfill runs alongside the in-process generator.
    ffmpeg_concurrency: int = 3
    # Backfill allowance for a whole cycle, split across cameras. Without any
    # cap the first cycle on a cold cache tries to sample the whole retention
    # horizon -- days of ffmpeg -- before the loop comes up for air.
    backfill_segments_per_cycle: int = 120
    # Wall-clock ceiling on the backfill phase. The segment count alone can't
    # bound the cycle, because how long a segment takes depends on the box; and
    # an over-long cycle delays the next live-edge pass, which is what let the
    # edge slip behind in the first place. Holding the edge for ten cameras at
    # 1 fps already costs most of a core, so backfill takes genuine leftovers
    # and no more.
    #
    # Cycle length is the floor on live-edge lag: a camera serviced at the start
    # of a cycle is a full cycle stale by the end of it. Measured on this
    # deployment, 35s here settled at a 100s cycle and ~105s lag -- over the 90s
    # the client is told to expect. 22s settles inside it. That is not the old
    # 20s in disguise: one decode now feeds every tier, so the same wall clock
    # buys several times the coverage it used to.
    backfill_time_budget_s: float = 22.0
    # Cap on the live-edge pass, per camera per cycle. Sized to cover the whole
    # lookback in one pass (900s / 10s segments), so a camera reaches `now` in a
    # single cycle rather than converging over several: a fixed small cap loses
    # ground whenever the cycle takes longer than the footage it generated, which
    # is exactly what happens once backfill shares the cycle.
    live_edge_segments: int = 90
    # How far back the live-edge pass will resume from. A cache that is further
    # behind than this jumps forward to the edge and leaves the gap for
    # backfill: crawling up from a day ago meant nothing recent was ever
    # generated, which is the one window clients actually scrub.
    live_edge_lookback_s: float = 900.0
    # Wall-clock reserved out of `backfill_time_budget_s`, exclusively for the
    # derived-tier decimation pass (generate_derived, run last each cycle).
    # Without this, backfill's own demand doesn't reliably hit zero -- measured
    # on this deployment, two or three cameras have a persistent small trickle
    # of real holes every cycle (motion-driven recording gaps), so backfill
    # alone consumes the whole shared deadline and decimation never runs at
    # all: traced directly, backfill burned 22s on 4 of 10 cameras and
    # derived-tier generation got exactly zero cycles across several minutes of
    # live operation. This carves out a floor for it regardless of how hungry
    # backfill is; backfill still gets everything left over. Set to 0 to
    # restore the old "decimation gets pure leftovers" behaviour.
    derive_time_reserve_s: float = 5.0
    # Minimum wall-clock seconds per cycle reserved for the backfill phase,
    # carved out of the live-edge pass rather than backfill's own budget.
    # Without this, a live edge slow enough to eat the whole tick (measured
    # live: 16-20s cycles against a 20s tick) left backfill's window already
    # closed before it started -- "(0 backfilled)" on nearly every cycle, and
    # cameras whose recent tier needs full decoding (not just keyframe
    # extraction) never got their aged tier at all. Live-edge now stops
    # early, at its per-camera boundary (never mid-segment), once the tick
    # has less than this much time left, so backfill always gets a turn.
    # 0 (or less) turns the live-edge deadline off entirely: the edge runs
    # every camera each tick as before #85. Use it on a box with no decode
    # headroom, where any backfill slice only starves the edge.
    backfill_min_share_s: float = 6.0

    @field_validator("format")
    @classmethod
    def _known_format(cls, v: str) -> str:
        fmt = v.strip().lower()
        if fmt not in ("jpeg", "webp"):
            raise ValueError(f"scrub.format must be 'jpeg' or 'webp', got {v!r}")
        return fmt

    @field_validator("generate_interval_s", "live_edge_interval_s", "sheet_version_grace_s")
    @classmethod
    def _positive(cls, v: float) -> float:
        """A non-positive tick would spin the generation loop without yielding,
        and a non-positive grace would sweep a version the index is advertising
        in the same breath it publishes it.

        `generate_interval_s` is checked too because it is now the ceiling on the
        loop's tick (`min` of the two), so a zero there defeats the check on
        `live_edge_interval_s` entirely.
        """
        if v <= 0:
            raise ValueError(f"must be > 0, got {v!r}")
        return v

    @field_validator("derive_time_reserve_s")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        """0 is a valid, explicit "no floor" setting -- unlike the tick
        constants above, there's nothing broken about turning this off."""
        if v < 0:
            raise ValueError(f"must be >= 0, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_derived_intervals(self) -> ScrubSection:
        """Every entry in `derived_intervals_s` must be strictly coarser than
        `aged_interval_s` (otherwise it isn't a derived tier, just a duplicate
        of the aged decode tier), distinct from every other entry (otherwise
        two tiers would generate and serve identical buckets under the same
        interval, silently clobbering each other), and land on the same
        epoch-anchored grid every interval uses (`grid.decimate_to_grid`,
        `grid.grid_point`): bucket/slot boundaries are `k * interval` from
        absolute epoch zero, so an interval that isn't a whole multiple of
        `aged_interval_s` puts that tier's grid points out of step with the
        aged tier's at every boundary but the first.

        This is a static, config-time check against the coarser decode tier
        only. It can't see that `match_keyframe_cadence` may raise a given
        camera's *recent* tier past its configured value -- the generator
        checks that at runtime per camera before decimating from it
        (`generator._is_whole_multiple`).
        """
        seen: set[float] = set()
        for derived in self.derived_intervals_s:
            if derived in seen:
                raise ValueError(
                    f"scrub.derived_intervals_s must not repeat a value (got {derived!r} twice)"
                )
            seen.add(derived)
            if derived <= self.aged_interval_s:
                raise ValueError(
                    "scrub.derived_intervals_s entries must each be > scrub.aged_interval_s "
                    f"(got derived={derived!r}, aged={self.aged_interval_s!r})"
                )
            ratio = derived / self.aged_interval_s
            if abs(ratio - round(ratio)) > 1e-6:
                raise ValueError(
                    "scrub.derived_intervals_s entries must each be a whole multiple of "
                    f"scrub.aged_interval_s to land on its epoch grid "
                    f"(got derived={derived!r}, aged={self.aged_interval_s!r})"
                )
        return self


class ProxySection(BaseModel):
    enabled: bool = True
    pass_request_headers: list[str] = Field(
        default_factory=lambda: ["range", "authorization", "cookie"]
    )


class EncountersSection(BaseModel):
    """Encounters (docs/encounters.md): group Frigate review segments
    ("atoms") into human-legible chains across cameras and time gaps --
    "a raccoon worked its way from the alley to the shed" as one thing,
    rather than three separate reviews. Overlay only: `event`/`reviewsegment`
    in Frigate's own DB stay the source of truth and are only ever read.

    Off by default. When on, the live MQTT review hook links as messages
    arrive and a periodic reconciler backfills/repairs from `reviewsegment`
    directly -- the reconciler is the "belt": anything the live hook missed
    or saw only partially (or everything, on a fresh install / after
    downtime) gets picked up there.
    """

    enabled: bool = False

    # Reconciler cadence. Encounters are a browsing/triage aid, not a live
    # alert path, so seconds of staleness is fine.
    reconcile_interval_s: float = 30.0

    # First-start backfill window over `reviewsegment`, when no watermark has
    # been recorded yet. Later cycles look back only `max_duration_s` behind
    # the watermark -- just enough to pick up a segment still updating.
    backfill_lookback_s: float = 86400.0

    # Max gap (seconds) between an encounter's last member end and a new
    # atom's start, keyed by the new atom's label family (linker.family_of).
    # "default" covers any label not in a named family (e.g. `package`).
    # Identity (sub_label) matches get 3x their family's allowance.
    gap_s: dict[str, float] = Field(
        default_factory=lambda: {
            "animal": 180.0,
            "person": 90.0,
            "vehicle": 45.0,
            "default": 60.0,
        }
    )

    # Hard cap on one encounter's total span (start of its first atom to the
    # start of a candidate atom) -- keeps a slow-moving, endlessly-adjacent
    # camera chain from growing into an unbounded "encounter" that covers the
    # whole day.
    max_duration_s: float = 1800.0

    # How many of an encounter's most-recently-visited distinct cameras count
    # as "nearby" for the same-camera/adjacency/companionship spatial checks.
    recent_cameras: int = 2

    # Minimum time (seconds) two atoms' spans must overlap to count as
    # companions (e.g. a person and a dog seen together) even with no shared
    # label family.
    min_copresence_s: float = 3.0

    # Extra camera-pair edges beyond what shared zone names already imply
    # (each `[camera_a, camera_b]`), e.g. for a handoff Frigate's zone naming
    # doesn't capture: `[["alley-wide", "shed"]]`.
    adjacency: list[list[str]] = Field(default_factory=list)

    # Camera-pair edges to remove even though the two cameras share a zone
    # name -- for a coincidental name collision that isn't really the same
    # ground. Config always wins over the zone-derived graph.
    not_adjacent: list[list[str]] = Field(default_factory=list)

    # How long a sealed encounter (and its members/decisions) survives
    # before the reconciler's hourly prune drops it -- mirrors
    # `face_capture.retention_days`/`scrub`'s retention fields. Unsealed
    # encounters are never pruned regardless of age.
    retention_days: int = 30

    # Camera topology (M2, docs/encounters.md "Camera topology"): learn
    # per-camera-pair, per-label-family transition times from linked
    # encounters and store them in `camera_transitions`. Off by default --
    # `use_learned_gaps` below controls whether the linker actually reads
    # what this writes.
    transitions_enabled: bool = False

    # Minimum number of samples a camera-pair/family edge needs before the
    # learner trusts its own percentiles ('learned'); below this it writes
    # `transition_default_s` instead ('default'), with the true (sub-
    # threshold) sample count recorded.
    transition_min_samples: int = 8

    # Discard a transition sample whose gap exceeds this many seconds --
    # keeps one very slow, atypical crossing from skewing the percentiles.
    transition_max_sample_s: float = 180.0

    # Minimum interval (seconds) between learning scans -- the scan is a
    # full read over `encounter_members` for the window below, so it runs on
    # its own cadence inside `reconcile()`, not every cycle.
    transition_learn_interval_s: float = 3600.0

    # How far back (days) the learning scan looks for transition samples.
    transition_learn_window_days: float = 14.0

    # Fallback {p10, p50, p90} (seconds) written for a camera-pair/family
    # edge with fewer than `transition_min_samples` samples.
    transition_default_s: dict[str, float] = Field(
        default_factory=lambda: {"p10": 2.0, "p50": 15.0, "p90": 60.0}
    )

    # Manual overrides for specific transitions, keyed `"camA>camB"` (applies
    # to every label family) or `"camA>camB:family"` (family-specific, wins
    # over the unqualified key) -- each value a {p10, p50, p90} mapping.
    # Written with `source='config'`, taking precedence over learned stats.
    transition_overrides: dict[str, dict[str, float]] = Field(default_factory=dict)

    # Whether the linker's "adjacent" reason reads `camera_transitions`
    # (M3): when on, a directed camera-pair/family edge with a `source ==
    # "learned"` row uses `p90 * transition_slack` as its allowed gap
    # instead of the flat `gap_s[family]` -- narrower or wider, whichever
    # the learned data says. A "config"/"default" row, or no row at all,
    # falls back to the flat allowance unchanged. Live -- toggling this
    # takes effect on the next reconcile cycle, no restart needed.
    use_learned_gaps: bool = False

    # Multiplier applied to a learned p90 when `use_learned_gaps` is on, to
    # allow slack beyond the observed 90th percentile. Live, same as
    # `use_learned_gaps`.
    transition_slack: float = 1.5

    # M4 /v1/timeline: hard cap (seconds) on the [start, end] window a single
    # request may ask for -- a global multi-camera composition is far more
    # expensive per second of window than one reel, so this is deliberately
    # tighter than any per-reel limit. Default 6h.
    timeline_max_window_s: float = 21600.0

    # Suggested continuations (M5, docs/encounters.md "Suggested
    # continuations"): `GET /v1/observations/{atom_id}/continuations` scores
    # candidate next-camera observations against a source atom. Weights need
    # not sum to 1 -- `score_candidate` normalises over whichever factors it
    # actually used for a given candidate.
    continuation_w_topo: float = 0.30
    continuation_w_time: float = 0.30
    continuation_w_direction: float = 0.20
    continuation_w_class: float = 0.20

    # Score thresholds bucketing a suggestion: `likely` at/above
    # `continuation_likely_score`, `possible` at/above
    # `continuation_min_score`, dropped below that. Machine predictions are
    # never bucketed `confirmed` regardless of score -- see
    # `encounters/continuations.py`.
    continuation_min_score: float = 0.25
    continuation_likely_score: float = 0.5


class PushSection(BaseModel):
    """Push notifications (docs/push-notifications.md).

    Off by default -- push is the last capability tier to light up, never a
    dependency of any other one (spec's "optional always" non-negotiable). The
    sidecar is the only APNs-facing piece; devices register against it the same
    way they authenticate against everything else (§1: reuse the sidecar's
    existing Frigate-session auth, no second credential).
    """

    enabled: bool = False
    # "mock" logs what would be sent and always succeeds -- the only transport
    # available without real APNs credentials, and the default so a fresh
    # deployment doesn't accidentally try to reach a relay that isn't there.
    # "relay" posts the minimal {device_token, environment, handle, server_id,
    # severity} payload to `relay_base_url` (spec §4).
    transport: str = "mock"
    # Short opaque id of *this* sidecar instance, carried in the APNs payload
    # so a device with more than one server registered can route the NSE's
    # handle-redeem fetch to the right base URL (spec §2). Generated at
    # startup if left blank -- see push/engine.py.
    server_id: str = ""

    # -- MQTT (event source, spec's "Architecture at a glance") --
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "marcellus-push"
    # Hard cap on the consumer queue depth (mqtt.py); consumed by that worker
    # -- this field only declares the knob and its bounds.
    mqtt_queue_max: int = Field(default=2000, ge=100, le=100000)
    mqtt_topic_reviews: str = "frigate/reviews"
    mqtt_topic_available: str = "frigate/available"
    # Dwell input only -- `frigate/reviews` stays the sole authority on
    # whether anything is push-worthy. See `dwell_source` below.
    mqtt_topic_events: str = "frigate/events"
    # -- MQTT flight recorder --
    # Rolling capture of every consumed reviews/events message, so any real
    # situation can be replayed exactly (tools/replay_capture.py) instead of
    # approximated by a hand-written scenario. JSONL, size-rotated (one .1
    # sibling kept). Empty path -> "mqtt-capture.jsonl" next to
    # push_settings_path.
    capture_enabled: bool = True
    capture_path: str = ""
    capture_max_bytes: int = 64 * 1024 * 1024

    # Reconnect backoff (spec §5, "MQTT broker unreachable from the sidecar").
    reconnect_backoff_s: float = 2.0
    reconnect_backoff_max_s: float = 60.0
    # After this long without any broker traffic, treat Frigate as possibly
    # offline and back-fill the gap on reconnect/resume (spec's stale/live
    # model, §12.6, reused verbatim) rather than silently dropping alerts.
    offline_silence_s: float = 60.0
    backfill_lookback_s: float = 60.0

    # -- Relay transport (spec §4) --
    # The deployed shared relay (github.com/helicopterrun/elsinore-push-relay):
    # holds the one team-bound APNs key and forwards content-free templated
    # alerts. Overridable for forks running their own relay under their own
    # bundle id/team.
    relay_base_url: str = "https://elsinore-push-relay.helicopterrun.workers.dev"
    # Per-attempt timeout (was a whole-send 10.0s before retry existed).
    relay_timeout_s: float = 5.0
    # Total attempts for retryable kinds (push, liveactivity start/end); 1 =
    # no retry. liveactivity update/situation/test always send once regardless
    # (transport.py's per-kind policy -- a late retried LA update can arrive
    # after an end, and situation/test have no supersession semantics to lean
    # on).
    relay_retry_attempts: int = Field(default=3, ge=1, le=10)
    # Consecutive transport failures (exception or 5xx at the attempt level;
    # 429/4xx never count) that open the circuit breaker.
    relay_breaker_failures: int = Field(default=3, ge=1, le=50)
    # How long the breaker stays open before a single half-open probe attempt.
    relay_breaker_open_s: float = Field(default=30.0, ge=1, le=600)

    # -- Handle redemption (spec §3 step 2) --
    handle_ttl_s: float = 3600.0

    # -- Situations (notification-experience plan §8) --
    # Situation handles carry a pre-warmed thumbnail and outlive the v1 ones:
    # plan §8 retains them for 24h so a notification the user comes back to
    # hours later can still redeem its image.
    situation_handle_ttl_s: float = 86400.0
    rate_limit_window_s: float = 3600.0
    # Pre-warmed thumbnail (plan §4 lever 1). ~320px/q60 lands around 10-20KB;
    # the NSE runs under a very tight memory ceiling and the phone may be on a
    # cold radio, so bigger buys nothing a notification can show.
    thumbnail_max_edge: int = 320
    thumbnail_quality: int = 60
    thumbnail_timeout_s: float = 5.0
    # Where a situation's loiter check gets its clock and its zone occupancy.
    #
    # "events" subscribes to `frigate/events` for dwell only. "reviews" is the
    # handoff's literal prescription -- dwell advanced solely by
    # `frigate/reviews` `type: update` messages. Measured on this deployment
    # (19.6 min, 2026-08-05) that topic published two review items as a `new`
    # and an `end` 30s apart with no update in between, because Frigate
    # publishes a review update when the item's *data* changes and a person
    # standing still changes nothing. A loiter threshold fed only from there
    # is never re-evaluated and never fires; "events" is the default for that
    # reason. Neither setting lets the object stream trigger a push on its own.
    dwell_source: str = "events"

    # -- Live Activities (Phase 2) --
    # Quiet period after which a Present situation counts as resolved. The
    # faster signal is Frigate's own object `end`, which the engine acts on
    # directly; this catches the case where it never arrives.
    activity_resolution_s: float = 30.0
    # An open card idle this long is closed silently -- the resolve that
    # never arrived, e.g. a dropped Frigate end or a failed write; sized so
    # a real loiter's updates keep it alive.
    card_resolution_s: float = 600.0
    # How long the activity lingers on screen after the end push.
    activity_dismissal_tail_s: float = 30.0
    activity_reap_after_s: float = 300.0
    # How often the resolution sweeper runs. Only ever *ends* activities, so
    # it is not the clock-driven keep-alive the plan forbids.
    activity_sweep_interval_s: float = 5.0

    # -- Attention ladder: delivery pipeline (Elsinore Phase 2) --
    # Off by default, independent of `enabled` -- this wraps the ladder
    # evaluator with card state and ordinary alert/silent pushes; it ships
    # dark until the wire-up's subject/place classification (currently an
    # MVP heuristic off `frigate/reviews` labels and `delivery_zone_place_map`)
    # is trusted against a live deployment. See docs/push-notifications.md.
    delivery_enabled: bool = True
    # Superseded by the user-editable `settings.zone_classes` (Elsinore
    # Phase 4, `push/policy_settings.py`) -- `delivery_wire.classify_place`
    # no longer reads this field. Left in place (unread) rather than
    # removed, since it's still a valid YAML key an existing deployment's
    # config file may set.
    delivery_zone_place_map: dict[str, str] = Field(default_factory=dict)
    # Design doc §3: an unhandled `urgent` card may re-alert once, this long
    # after its last sound.
    delivery_urgent_resound_s: float = 120.0
    delivery_urgent_resound_enabled: bool = True
    delivery_urgent_resound_max: int = 5
    # How often the urgent re-sound sweep runs. Only ever emits the one
    # re-sound a card is owed -- not a keep-alive.
    delivery_resound_sweep_interval_s: float = 15.0
    # -- Encounter-aware push (docs/encounters.md "Encounter-aware push") --
    # Group a story's pushes in Notification Center by encounter instead of
    # by camera: the APNs `thread-id` becomes the encounter id and the
    # payload carries `encounter_id`/`cameras_path`. Presentation only --
    # each camera still gets its own card unless `encounter_merge` is on.
    encounter_threading: bool = True
    # Route a later camera's review of the *same* encounter onto the card
    # the first camera already opened -- one notification per encounter,
    # its body becoming the crossing path. Off by default: with it off the
    # would-be merges are still logged (DEBUG) so they can be counted
    # against real duplicates before switching it on, same validation shape
    # as `geometric_dedup`.
    encounter_merge: bool = False
    # Budget for the synchronous encounter link the push path awaits before
    # delivery. On timeout the push goes out unthreaded/unmerged rather than
    # late -- the queued worker still links the review a moment later.
    encounter_link_timeout_s: float = 0.25
    # Backfilled events older than this are discarded rather than replayed.
    delivery_backfill_staleness_s: float = 300.0
    # Live Activity stale-date offset from now.
    delivery_la_stale_s: float = 900.0
    # Relay auth key — sent as x-relay-key header on every relay request.
    relay_key: str = ""
    # Phone-reachable base URL for *this sidecar instance*, e.g.
    # "http://192.168.50.207:5001" or "https://sidecar.example.com". Used to
    # build the complete URL the card contract's `media` field documents
    # (docs/apns-payload-spec.md) -- unlike the v1/situations flow, which
    # only ever sends `handle` + `server_id` and lets the already-registered
    # app resolve the base URL itself, the card contract is a single
    # self-authorizing URL, so the sidecar has to know its own externally
    # reachable address to build it. Never Frigate's address -- Frigate is
    # never exposed to the phone directly; the sidecar fetches the snapshot
    # itself (`frigate.base_url`, LAN-internal) and re-hosts it behind a
    # minted handle at `/v1/push/thumbnail/{handle}`, same as situations.
    # Empty (the default) omits `media` entirely -- no broken link.
    external_base_url: str = ""

    # -- Live Activities for cards (Elsinore Phase 3) --
    # Master switch, independent of `delivery_enabled` (which must also be
    # on -- a card LA is an additional output channel for the same card
    # lifecycle, never a substitute for it). Off doesn't undo `delivery_la_families`;
    # it's the fast, whole-feature kill switch.
    delivery_la_enabled: bool = True
    # Superseded by the user-editable `settings.live_activities` (Elsinore
    # Phase 4, `push/policy_settings.py`) -- `delivery_wire.py` no longer
    # reads this field for per-family gating. Left in place (unread) for
    # the same reason as `delivery_zone_place_map` above.
    delivery_la_families: dict[str, bool] = Field(default_factory=dict)

    # -- Attention ladder settings API (Elsinore Phase 4) --
    # Where the user-editable policy document (routing table, zone-class
    # assignments, LA family toggles) is persisted. JSON, not YAML -- the
    # app PUTs a JSON body and round-tripping it through YAML's type
    # coercion on the way back out is a bug factory, not a feature. Created
    # with defaults on first read if it doesn't exist yet
    # (`push/policy_settings.py`).
    push_settings_path: str = "config/push_settings.json"

    # Where the uploaded floorplan/site image behind the /cameras layout map
    # is stored (extension appended per upload type). Same runtime-data class
    # as push_settings_path, so it lives next to it.
    floorplan_path: str = "config/floorplan"

    @field_validator("dwell_source")
    @classmethod
    def _known_dwell_source(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("events", "reviews"):
            raise ValueError(f"push.dwell_source must be 'events' or 'reviews', got {v!r}")
        return s

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, v: str) -> str:
        t = v.strip().lower()
        if t not in ("mock", "relay"):
            raise ValueError(f"push.transport must be 'mock' or 'relay', got {v!r}")
        return t


class LcdPreset(BaseModel):
    """One configurable doorbell LCD reply (M-2, `unifi_protect.lcd_presets`).

    Maps to a Protect `lcdMessage` PATCH body: `type` LEAVE_PACKAGE_AT_DOOR
    and DO_NOT_DISTURB carry no `text`; CUSTOM_MESSAGE requires one.
    """

    type: Literal["LEAVE_PACKAGE_AT_DOOR", "DO_NOT_DISTURB", "CUSTOM_MESSAGE"]
    text: str | None = None
    duration_s: int
    title: str

    @field_validator("duration_s")
    @classmethod
    def _duration_bounds(cls, v: int) -> int:
        if not 1 <= v <= 86400:
            raise ValueError(f"lcd_presets duration_s must be 1..86400, got {v!r}")
        return v

    @model_validator(mode="after")
    def _custom_message_needs_text(self) -> LcdPreset:
        if self.type == "CUSTOM_MESSAGE" and not (self.text or "").strip():
            raise ValueError("lcd_presets: type CUSTOM_MESSAGE requires non-empty text")
        return self


def _default_lcd_presets() -> dict[str, LcdPreset]:
    return {
        "leave_package": LcdPreset(
            type="LEAVE_PACKAGE_AT_DOOR", duration_s=1800, title="Leave package"
        ),
        "be_right_there": LcdPreset(
            type="CUSTOM_MESSAGE",
            text="BE RIGHT THERE",
            duration_s=120,
            title="Be right there",
        ),
        "do_not_disturb": LcdPreset(type="DO_NOT_DISTURB", duration_s=3600, title="Do not disturb"),
    }


class UnifiProtectSection(BaseModel):
    """UniFi Protect doorbell-ring -> push notification (guide_content's
    `unifi-protect.md`).

    Entirely separate from `PushSection`'s Frigate-review pipeline: a ring
    bypasses the card/attention-ladder/rate-limiter path outright (a person
    at the door is never something to route through the ladder) and this
    section's `enabled` is its own switch, independent of `push.enabled` --
    though a ring send still goes out through the same registered devices
    and the same transport (`push/unifi_protect.py`, `push/doorbell.py`).

    Off by default: this is an optional integration against a UniFi OS
    console most deployments don't have.
    """

    enabled: bool = False
    # Base URL of the UniFi OS console hosting Protect, e.g.
    # "https://192.168.1.1" (no trailing /proxy/protect/...).
    console_url: str = ""
    # UniFi OS "Integration" API key (Settings -> Control Plane ->
    # Integrations -> Create API Key). Env-overridable
    # (MARCELLUS_UNIFI_PROTECT__API_KEY) and never logged.
    api_key: str = ""
    # Most UniFi OS consoles present a self-signed cert; default matches the
    # console_url:443 expectation of a LAN appliance nobody's issued a real
    # cert for.
    verify_tls: bool = False
    # Protect camera id -> Frigate camera name. A ring from an id not in
    # this map is logged (debug) and dropped -- there is no Frigate camera
    # to attribute the snapshot/send to.
    cameras: dict[str, str] = Field(default_factory=dict)
    # Server-side de-dup: a second ring event for the same (mapped) camera
    # within this many seconds of the first is dropped rather than sent
    # again -- the Protect integration API has been observed to occasionally
    # deliver a duplicate "add" event for one physical press.
    ring_dedup_seconds: float = 20.0

    # How often `ProtectRingSubscriber.device_poll_loop` polls
    # `/proxy/protect/integration/v1/cameras` for camera-health (online/lcd
    # state), independent of the ring websocket. Floored at 15s -- this is a
    # REST poll against the same console, not worth hammering faster than
    # that for state that changes rarely.
    device_poll_seconds: float = 60.0

    @field_validator("device_poll_seconds")
    @classmethod
    def _min_device_poll_seconds(cls, v: float) -> float:
        if v < 15:
            raise ValueError(f"unifi_protect.device_poll_seconds must be >= 15, got {v!r}")
        return v

    # -- M-2: doorbell LCD replies + ring snapshot ---------------------------
    # Configurable LCD reply slots (`GET/POST /v1/doorbell/{camera}/lcd*`).
    # Keyed by an operator-chosen id referenced from a device's
    # `doorbell_slots`; the three defaults below are also the fallback order
    # when a device has never set its own slots.
    lcd_presets: dict[str, LcdPreset] = Field(default_factory=_default_lcd_presets)
    # A device's own free-text LCD reply (`POST .../lcd` with `custom_text`)
    # stays lit this long before the console clears it back to nothing.
    custom_reply_duration_s: int = 120
    # Longest normalized custom-text reply accepted; the console itself takes
    # up to 34 chars but this stays conservative.
    custom_reply_max_chars: int = 30
    # An animation/image LCD reply (`GET .../files/animations`) stays lit
    # this long.
    image_duration_s: int = 300
    # Which source a ring's `media` snapshot comes from: "protect" (the
    # doorbell's own onboard snapshot) or "frigate" (today's behaviour,
    # unchanged).
    ring_snapshot: Literal["protect", "frigate"] = "protect"

    @field_validator("custom_reply_max_chars")
    @classmethod
    def _custom_reply_max_chars_bounds(cls, v: int) -> int:
        if not 1 <= v <= 64:
            raise ValueError(f"unifi_protect.custom_reply_max_chars must be 1..64, got {v!r}")
        return v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=_ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
    )

    frigate: FrigateSection = Field(default_factory=FrigateSection)
    sidecar: SidecarSection = Field(default_factory=SidecarSection)
    face_capture: FaceCaptureSection = Field(default_factory=FaceCaptureSection)
    face_enrich: FaceEnrichSection = Field(default_factory=FaceEnrichSection)
    watchdog: WatchdogSection = Field(default_factory=WatchdogSection)
    scrub: ScrubSection = Field(default_factory=ScrubSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    push: PushSection = Field(default_factory=PushSection)
    encounters: EncountersSection = Field(default_factory=EncountersSection)
    unifi_protect: UnifiProtectSection = Field(default_factory=UnifiProtectSection)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_origins(self) -> Settings:
        """Warn (don't fail) when both Frigate origins are the same.

        `base_url` is the unauthenticated origin the sidecar calls itself;
        `proxy_base_url` is the authenticated one it forwards client traffic
        to and validates sessions against. Pointing both at the unauthenticated
        port silently turns the `/v1` session check into a no-op -- any cookie
        would pass -- so it's worth a loud line in the log even though a
        Frigate install with auth disabled is a legitimate configuration.
        """
        if self.frigate.base_url.rstrip("/") == self.frigate.proxy_base_url.rstrip("/"):
            logger.warning(
                "frigate.base_url and frigate.proxy_base_url are identical (%s) -- if that "
                "origin does not require a Frigate session, the sidecar's own auth check "
                "cannot reject anything (docs/scrub-cache-and-proxy-spec.md §3.2)",
                self.frigate.base_url,
            )
        return self


class _StaticYamlSource(PydanticBaseSettingsSource):
    """A pydantic-settings source backed by a pre-loaded YAML dict.

    Implemented as a settings source (not as init kwargs) so env variables
    can still override YAML values — env_settings runs first in the source
    tuple returned by `settings_customise_sources`.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def _discover_yaml_path(explicit: str | os.PathLike[str] | None) -> Path | None:
    if explicit:
        return Path(explicit)
    if env_path := os.environ.get("MARCELLUS_CONFIG"):
        return Path(env_path)
    for candidate in DEFAULT_CONFIG_PATHS:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def _apply_legacy_env_fallback() -> None:
    """Copy legacy FRIGATE_SIDECAR_* env vars to their MARCELLUS_*
    equivalent when the new name isn't already set, and warn once summarising
    which legacy keys were used.

    Lets an existing Docker deployment's env vars (old names) keep working
    across the rename without a config change; a MARCELLUS_* value always
    wins when both are set.
    """
    legacy_keys = []
    for key, value in list(os.environ.items()):
        if not key.startswith(_LEGACY_ENV_PREFIX):
            continue
        new_key = _ENV_PREFIX + key[len(_LEGACY_ENV_PREFIX) :]
        if new_key not in os.environ:
            os.environ[new_key] = value
            legacy_keys.append(key)
    if legacy_keys:
        logger.warning(
            "FRIGATE_SIDECAR_* env vars are deprecated, use MARCELLUS_*: %s",
            ", ".join(sorted(legacy_keys)),
        )


def _unwrap_model_type(annotation: Any) -> type[BaseModel] | None:
    """If `annotation` is (or wraps in `X | None` / `Optional[X]`) a
    `BaseModel` subclass, return that subclass; otherwise `None`."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    # `X | None` resolves to `types.UnionType` on 3.10+, `Optional[X]` to `typing.Union`.
    if get_origin(annotation) in (Union, types.UnionType):
        for arg in get_args(annotation):
            found = _unwrap_model_type(arg)
            if found is not None:
                return found
    return None


def _warn_unknown_keys(data: Any, model: type[BaseModel], *, path: str = "") -> None:
    """Recursively walk raw YAML `data` against `model`'s fields and log
    every key with no matching field, at WARNING, with its dotted path.

    `Settings` (and its nested sections) use `extra="ignore"` so a stale or
    misspelled key in prod's YAML doesn't fail startup -- but silently is too
    silent: a renamed key (or a typo) then just quietly reverts to the
    default with no signal anywhere. This is the "someone still gets told"
    half of that trade-off.
    """
    if not isinstance(data, Mapping):
        return
    fields = model.model_fields
    for key, value in data.items():
        dotted = f"{path}.{key}" if path else str(key)
        field = fields.get(key)
        if field is None:
            logger.warning(
                "config: unknown key %r -- not part of the sidecar schema "
                "(typo, or a renamed/removed setting?)",
                dotted,
            )
            continue
        nested_model = _unwrap_model_type(field.annotation)
        if nested_model is not None and isinstance(value, Mapping):
            _warn_unknown_keys(value, nested_model, path=dotted)


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    _apply_legacy_env_fallback()
    yaml_path = _discover_yaml_path(config_path)
    yaml_data = _read_yaml(yaml_path) if yaml_path else {}
    _warn_unknown_keys(yaml_data, Settings)

    class _BoundSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                _StaticYamlSource(settings_cls, yaml_data),
                file_secret_settings,
            )

    settings = _BoundSettings()

    # Lazy import: `tuning.py` imports `config` (for `Settings`/the section
    # models), so importing it at module scope here would be a cycle.
    from marcellus import tuning

    tuning.snapshot_base(settings)
    overrides_file = tuning.overrides_path(settings)
    file_overrides = tuning.read_overrides(overrides_file)
    applicable = {
        key: value
        for key, value in file_overrides.items()
        if value is not None and not tuning.is_env_locked(key, os.environ)
    }
    # The file is hand-editable and may predate a rename or a tightened
    # range: drop any entry that fails validation (one at a time, so a single
    # bad key does not take the rest down) rather than trusting it blindly.
    for key in list(applicable):
        problems = tuning.validate({key: applicable[key]}, settings)
        if problems:
            logger.warning(
                "tuning: ignoring %s from %s: %s", key, overrides_file, "; ".join(problems)
            )
            del applicable[key]
    if applicable:
        tuning.apply_overrides(settings, applicable)
        logger.info(
            "tuning: applied %d override(s) from %s: %s",
            len(applicable),
            overrides_file,
            ", ".join(sorted(applicable)),
        )
    tuning.snapshot_startup(settings)
    return settings
