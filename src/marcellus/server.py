"""FastAPI app factory and uvicorn entry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from marcellus import __version__, fmt
from marcellus.auth import FrigateAuthMiddleware
from marcellus.config import Settings, load_settings
from marcellus.db import FrigateDBMissingError
from marcellus.errors import error_detail
from marcellus.frigate_api import FrigateClient
from marcellus.guide import load_guide
from marcellus.push import delivery, delivery_wire
from marcellus.push import store as push_store
from marcellus.push.engine import PushEngine
from marcellus.push.log_context import PushContextFilter
from marcellus.push.mqtt import MqttReviewSubscriber, compute_backoff
from marcellus.push.transport import LogTransport, PushTransport, RelayTransport
from marcellus.routes import analysis as analysis_routes
from marcellus.routes import debug as debug_routes
from marcellus.routes import encounters as encounters_routes
from marcellus.routes import enrich as enrich_routes
from marcellus.routes import face_captures as face_capture_routes
from marcellus.routes import fps_budget as fps_budget_routes
from marcellus.routes import guide as guide_routes
from marcellus.routes import health as health_routes
from marcellus.routes import login_page as login_page_routes
from marcellus.routes import map_page as map_page_routes
from marcellus.routes import motion as motion_routes
from marcellus.routes import placement as placement_routes
from marcellus.routes import proxy as proxy_routes
from marcellus.routes import push as push_routes
from marcellus.routes import push_floorplan as push_floorplan_routes
from marcellus.routes import push_map as push_map_routes
from marcellus.routes import push_settings as push_settings_routes
from marcellus.routes import replay as replay_routes
from marcellus.routes import score_histogram as score_histogram_routes
from marcellus.routes import scrub as scrub_routes
from marcellus.routes import scrub_ui as scrub_ui_routes
from marcellus.routes import search as search_routes
from marcellus.routes import settings_page as settings_page_routes
from marcellus.routes import status as status_routes
from marcellus.routes import toybox as toybox_routes
from marcellus.routes import triage as triage_routes
from marcellus.routes import tuning as tuning_routes
from marcellus.routes import zone_hits as zone_hits_routes
from marcellus.scrub.lock import ScrubCacheLock, ScrubLockHeld

_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_ROOT / "templates"
_STATIC_DIR = _PACKAGE_ROOT / "static"


class _CachedStaticFiles(StaticFiles):
    """StaticFiles with immutable caching.

    Safe because every asset reference carries `?v={{ asset_v }}` — a deploy
    changes the URL, so the browser never has to revalidate the old one.
    """

    def file_response(self, *args: object, **kwargs: object) -> Response:
        response = super().file_response(*args, **kwargs)  # type: ignore[arg-type]
        # ES modules under js/mapedit/ import each other by bare relative path
        # (no ?v= stamp possible on `import` specifiers), so they revalidate
        # via ETag instead of caching immutably.
        scope = args[2] if len(args) > 2 else kwargs.get("scope")
        path = scope.get("path", "") if isinstance(scope, dict) else ""
        if "/js/mapedit/" in path:
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

# The proxy's catch-all: everything registered before it is a route the sidecar
# owns and therefore gates behind a Frigate session (auth.py).
_PROXY_CATCH_ALL = "/{path:path}"

logger = logging.getLogger(__name__)

# Rate-limit for the low-disk-space skip warning below -- a full disk would
# otherwise repeat the same log every tick (as often as every 20s) forever.
_LOW_SPACE_WARN_INTERVAL_S = 900.0


_last_disk_check_warn = 0.0


def _below_free_space_floor(cache_dir: Path, min_free_bytes: int) -> bool:
    """True if the cache filesystem has less than `min_free_bytes` free.

    `shutil.disk_usage` fails if `cache_dir` doesn't exist yet (e.g. very
    first boot before the generator has created it) -- treat that as "can't
    tell, don't block generation on it", matching `_cache_on_separate_filesystem`.
    An OSError past that point (e.g. the filesystem underneath got yanked)
    gets a rate-limited warning, since silently returning False here means
    the free-space floor is quietly not being enforced.
    """
    global _last_disk_check_warn
    if min_free_bytes <= 0:
        return False
    try:
        return shutil.disk_usage(cache_dir).free < min_free_bytes
    except OSError:
        now_mono = time.monotonic()
        if now_mono - _last_disk_check_warn >= _LOW_SPACE_WARN_INTERVAL_S:
            _last_disk_check_warn = now_mono
            logger.warning(
                "scrub: could not stat cache filesystem (%s) -- the free-space "
                "floor is not being enforced this tick",
                cache_dir,
            )
        return False


async def _scrub_generation_loop(app: FastAPI) -> None:
    """Continuous trailing edge (docs spec §5.4 option (a)) -- NEVER hourly, per
    the spec's own blocking correction: an hourly cron reproduces the
    top-of-hour hole this cache exists to remove.

    The tick is `live_edge_interval_s`, and it is a *deadline*, not a sleep: the
    trailing-window pass runs at the top of every tick and backfill gets the time
    left over. It used to be the other way round -- a full cycle, then a fixed
    sleep -- so backfill's budget landed on top of a live-edge pass that had
    grown to ~65s and pushed the effective cadence to ~100s. Cadence is the floor
    on how stale the newest cell can be, so that came straight off the freshness
    the client is promised. Nothing here bounds *throughput*; the same segments
    are decoded either way, in smaller instalments.

    A tick that overruns is not slept off -- backfill is simply cut, and if the
    live pass alone overran, the next tick starts immediately. That is the
    correct priority: history can wait, the edge cannot.

    Retention pruning rides along on its own slower cadence: it used to be
    reachable only from the CLI, so an unattended deployment kept every sheet
    it ever generated.
    """
    from marcellus.scrub.generator import SourceProfile, generate_cycle, prune

    settings: Settings = app.state.settings
    # `generate_interval_s` remains the ceiling, so a deployment that has
    # deliberately slowed generation down keeps that setting meaningful.
    tick = min(settings.scrub.generate_interval_s, settings.scrub.live_edge_interval_s)
    next_prune = time.time() + settings.scrub.prune_interval_s
    # Measured GOP and aspect per camera, kept for the process lifetime.
    profile = SourceProfile()
    last_low_space_warn = 0.0
    app.state.scrub_low_disk = False
    while True:
        deadline = time.monotonic() + tick
        if _below_free_space_floor(settings.scrub.cache_dir, settings.scrub.min_free_bytes):
            app.state.scrub_low_disk = True
            # Below the floor: skip generation for this tick (it's what would
            # be filling the disk further) but do NOT touch scrub_last_cycle --
            # the status dashboard's staleness check (below) is what's supposed
            # to surface this condition, and updating it here would mask it.
            # Pruning still runs on its own cadence below regardless, since
            # that's what frees space back up.
            now_mono = time.monotonic()
            if now_mono - last_low_space_warn >= _LOW_SPACE_WARN_INTERVAL_S:
                last_low_space_warn = now_mono
                logger.warning(
                    "scrub: cache filesystem below free-space floor (%d bytes) -- "
                    "skipping generation this tick, pruning still runs",
                    settings.scrub.min_free_bytes,
                )
        else:
            app.state.scrub_low_disk = False
            try:
                await generate_cycle(
                    settings, now=time.time(), profile=profile, backfill_deadline=deadline
                )
            except Exception:
                logger.exception("scrub: generation cycle failed")
            else:
                # Consumed by the status dashboard: "when did a cycle last finish".
                app.state.scrub_last_cycle = time.time()
        if time.time() >= next_prune:
            next_prune = time.time() + settings.scrub.prune_interval_s
            try:
                result = await asyncio.to_thread(prune, settings)
                if any(v for k, v in result.items() if k.endswith("_deleted")):
                    logger.info("scrub: retention prune %s", result)
            except Exception:
                logger.exception("scrub: retention prune failed")
        # Zero when the tick overran, which keeps the edge pass running
        # back-to-back on a cache that is still catching up.
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))


def _cache_on_separate_filesystem(cache_dir: Path, recordings_path: Path) -> bool:
    """§8.3 hard requirement: refuse to enable scrub if its cache would land
    on the same filesystem as Frigate's recordings (which may be nearly
    full)."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return os.stat(cache_dir).st_dev != os.stat(recordings_path).st_dev
    except OSError:
        # Can't tell (e.g. recordings_path doesn't exist in this environment,
        # such as under test) -- don't block startup on an inconclusive check.
        # `_check_scrub_inputs` has already said so loudly.
        return True


def _check_scrub_inputs(settings: Settings) -> None:
    """Log the misconfigurations that otherwise make the generator a silent no-op.

    Both of these produced zero output and zero log lines before: a
    `recordings_path` that doesn't resolve (the recordings volume simply isn't
    mounted where the sidecar looks) and a missing ffmpeg.
    """
    recordings = settings.frigate.recordings_path
    if not recordings.exists():
        logger.error(
            "scrub is enabled but frigate.recordings_path (%s) does not exist -- the "
            "generator will find no segments to sample. Check the recordings mount "
            "(docs/scrub-cache-and-proxy-spec.md §8.2).",
            recordings,
        )
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            logger.error(
                "scrub is enabled but %s is not on PATH -- no frames can be extracted", binary
            )


def _build_push_transport(settings: Settings) -> PushTransport:
    """Mock/log transport by default -- the only one usable without real APNs
    credentials (spec §4). "relay" posts to `push.relay_base_url`."""
    if settings.push.transport == "relay":
        if not settings.push.relay_key:
            # Deliberately not fatal -- an LXC deploy mid-upgrade must not
            # hard-fail on a missing key -- but every push would go to the
            # relay unauthenticated, so it has to be unmissable in the log.
            logger.critical(
                "push: transport is 'relay' but push.relay_key is EMPTY -- every "
                "relay request will be sent UNAUTHENTICATED. Set push.relay_key "
                "in the sidecar config."
            )
        return RelayTransport(
            settings.push.relay_base_url,
            timeout=settings.push.relay_timeout_s,
            relay_key=settings.push.relay_key,
            retry_attempts=settings.push.relay_retry_attempts,
            breaker_failures=settings.push.relay_breaker_failures,
            breaker_open_s=settings.push.relay_breaker_open_s,
        )
    return LogTransport()


async def _push_subscriber_loop(app: FastAPI) -> None:
    """Keep the MQTT subscriber alive for the life of the process.

    `run_forever` already retries broker-connect failures internally: this
    outer retry exists for the class of bug it can't protect against -- an
    unhandled exception escaping `run_forever` itself (2026-08-11: one such
    bug took the whole subscriber down and it silently stayed down for 41
    hours, since this task previously just logged and returned).
    """
    subscriber: MqttReviewSubscriber = app.state.push_subscriber
    attempt = 0
    while True:
        try:
            await subscriber.run_forever()
            return  # run_forever only returns after subscriber.stop()
        except Exception:
            delay = compute_backoff(attempt, base=5.0, cap=300.0)
            attempt += 1
            logger.exception(
                "push: mqtt subscriber loop crashed, restarting in %.0fs", delay
            )
            await asyncio.sleep(delay)


async def _activity_sweep_loop(app: FastAPI) -> None:
    """End Live Activities whose situation has gone quiet.

    Resolution is the one transition nothing announces -- no message arrives
    to say "the object stopped being reported" -- so it needs a sweep. This
    only ever *ends* activities; it never refreshes one, so the rule that
    stage transitions are the only thing minting an LA push still holds.
    """
    engine: PushEngine = app.state.push_engine
    interval = app.state.settings.push.activity_sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            await engine.sweep_activities()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("push: activity sweep failed")


async def _face_enrich_loop(app: FastAPI) -> None:
    """Drive faces/enrich.py's run_cycle on a fixed cadence.

    The cycle itself is sync (onnx inference, HTTP fetches, SQLite) and runs
    via asyncio.to_thread, so a slow event never blocks the event loop; the
    loop body is the same shape as `_activity_sweep_loop`. Sets
    `app.state.face_enrich_last_cycle` for /healthz staleness.
    """
    from marcellus.faces import enrich

    settings: Settings = app.state.settings
    while True:
        await asyncio.sleep(settings.face_enrich.interval_s)
        try:
            await asyncio.to_thread(enrich.run_cycle, settings)
            app.state.face_enrich_last_cycle = time.time()
        except asyncio.CancelledError:
            raise
        except enrich.EnrichUnavailable:
            logger.exception("face_enrich: dependencies missing; stopping the worker")
            return
        except Exception:
            logger.exception("face_enrich: cycle failed")


async def _encounters_loop(app: FastAPI) -> None:
    """Drive `EncounterService.reconcile` on a fixed cadence -- the "belt"
    catching anything the live `on_review` hook missed or saw only
    partially. Sync work (SQLite, no network) runs via `asyncio.to_thread`,
    same shape as `_face_enrich_loop`.
    """
    settings: Settings = app.state.settings
    service = app.state.encounters
    while True:
        await asyncio.sleep(settings.encounters.reconcile_interval_s)
        try:
            await asyncio.to_thread(service.reconcile)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("encounters: reconcile cycle failed")


async def _encounters_worker_loop(app: FastAPI) -> None:
    """Run `EncounterService.run_worker` -- the live-hook queue consumer.

    A separate task from `_encounters_loop` on purpose: this one drains
    `observe_review`'s queue continuously (not on a fixed cadence), doing
    each review's sqlite work via `asyncio.to_thread` so a WAL busy-timeout
    wait against the reconciler never blocks the event loop that also
    serves MQTT and push delivery.
    """
    service = app.state.encounters
    await service.run_worker()


async def _delivery_resound_sweep_loop(app: FastAPI) -> None:
    """The urgent-only re-sound timer (design doc §3): an `urgent` card
    still unhandled after `delivery_urgent_resound_s` gets exactly one more
    sound. Only ever adds a sound to a card that already exists and already
    sounded once -- it never creates or ends anything, so it's the same
    kind of "only tightens, never a keep-alive" sweep as
    `_activity_sweep_loop`.
    """
    engine: PushEngine = app.state.push_engine
    settings: Settings = app.state.settings
    interval = settings.push.delivery_resound_sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            from marcellus import db

            conn = db.open_sidecar(engine.db_path)
            try:
                devices = push_store.list_devices(conn)
                await delivery.sweep_urgent_resound(
                    conn, engine.transport, devices,
                    interval_s=settings.push.delivery_urgent_resound_s,
                    enabled=settings.push.delivery_urgent_resound_enabled,
                    max_resounds=settings.push.delivery_urgent_resound_max,
                    payload_for_resound=delivery_wire.resound_payload_for,
                )
            finally:
                conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("push: delivery re-sound sweep failed")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # One-shot probe so the UI knows whether to surface Plus controls.
    # Best-effort and off the event loop: if Frigate is down or slow right now,
    # we just hide them rather than stalling startup on a sync HTTP call.
    async def _probe_plus() -> None:
        def _probe() -> bool:
            with FrigateClient(settings.frigate.base_url) as fc:
                return fc.plus_enabled()

        with contextlib.suppress(Exception):
            app.state.plus_enabled = await asyncio.to_thread(_probe)

    probe_task = asyncio.create_task(_probe_plus())

    if settings.face_capture.enabled:
        # Logged from the server too, not just from the timer job: a
        # misconfigured output_dir under ProtectSystem=strict makes the feature
        # a silent no-op and /faces/captures a permanent empty state, and the
        # server's log is where someone looks first. No task is created -- the
        # job runs in a separate process behind its own systemd timer.
        from marcellus.faces import crosscam as _crosscam

        _crosscam.check_inputs(settings)

    task: asyncio.Task[None] | None = None
    scrub_lock: ScrubCacheLock | None = None
    app.state.scrub_locked = False
    if settings.scrub.enabled:
        _check_scrub_inputs(settings)
        if not _cache_on_separate_filesystem(
            settings.scrub.cache_dir, settings.frigate.recordings_path
        ):
            logger.error(
                "scrub.cache_dir (%s) is on the same filesystem as "
                "frigate.recordings_path (%s) -- refusing to start the generator "
                "(docs spec §8.3)",
                settings.scrub.cache_dir, settings.frigate.recordings_path,
            )
        else:
            scrub_lock = ScrubCacheLock(settings.scrub.cache_dir)
            try:
                # A restarting predecessor process (systemd RestartSec=5) may
                # still be releasing the lock -- give it 30s to exit before
                # giving up, rather than refusing to start over a race.
                await asyncio.to_thread(scrub_lock.acquire, 30.0)
            except ScrubLockHeld as exc:
                logger.error(
                    "scrub: %s -- generation loop NOT started, HTTP keeps serving", exc
                )
                app.state.scrub_locked = True
                scrub_lock = None
            else:
                task = asyncio.create_task(_scrub_generation_loop(app))

    encounters_task: asyncio.Task[None] | None = None
    encounters_worker_task: asyncio.Task[None] | None = None
    if settings.encounters.enabled:
        from marcellus.encounters.adjacency import build_adjacency
        from marcellus.encounters.service import EncounterService
        from marcellus.zones import load_camera_zones

        adjacency = build_adjacency(
            load_camera_zones(settings.frigate.config_path),
            extra=settings.encounters.adjacency,
            removed=settings.encounters.not_adjacent,
        )
        app.state.encounters = EncounterService(settings, adjacency=adjacency)
        encounters_task = asyncio.create_task(_encounters_loop(app))
        encounters_worker_task = asyncio.create_task(_encounters_worker_loop(app))

    push_task: asyncio.Task[None] | None = None
    sweep_task: asyncio.Task[None] | None = None
    delivery_sweep_task: asyncio.Task[None] | None = None
    if settings.push.enabled:
        from marcellus import db
        from marcellus.push import card_store, policy_settings

        _conn = db.open_sidecar(str(settings.sidecar.db_path))
        try:
            collapsed = card_store.migrate_drop_zone_from_card_keys(_conn)
            if collapsed:
                logger.info(
                    "push: collapsed %d zone-bearing card row(s) onto the "
                    "camera/subject-kind/track-id identity", collapsed,
                )
        finally:
            _conn.close()

        # Load the user-editable routing/zone/LA policy (Elsinore Phase 4)
        # and apply it before the first card can ever be evaluated --
        # `ladder_policy.TABLE` must never be read in its own unmodified
        # default state on a deployment that has a settings file.
        policy_settings.startup(settings.push.push_settings_path)
        policy_settings.load_zone_display_names(settings.frigate.config_path)

        server_id = settings.push.server_id or f"s_{id(app):x}"
        transport = _build_push_transport(settings)
        app.state.push_transport = transport
        engine = PushEngine(
            db_path=str(settings.sidecar.db_path),
            transport=transport,
            server_id=server_id,
            situation_handle_ttl_s=settings.push.situation_handle_ttl_s,
            frigate_base_url=settings.frigate.base_url,
            rate_limit_window_s=settings.push.rate_limit_window_s,
            thumbnail_max_edge=settings.push.thumbnail_max_edge,
            thumbnail_quality=settings.push.thumbnail_quality,
            thumbnail_timeout_s=settings.push.thumbnail_timeout_s,
            dwell_source=settings.push.dwell_source,
            activity_resolution_s=settings.push.activity_resolution_s,
            card_resolution_s=settings.push.card_resolution_s,
            activity_dismissal_tail_s=settings.push.activity_dismissal_tail_s,
            activity_reap_after_s=settings.push.activity_reap_after_s,
            push_config=settings.push,
            on_review=(
                app.state.encounters.observe_review
                if getattr(app.state, "encounters", None) is not None
                else None
            ),
        )
        app.state.push_engine = engine
        subscriber = MqttReviewSubscriber(
            settings.push, engine, frigate_base_url=settings.frigate.base_url
        )
        app.state.push_subscriber = subscriber
        push_task = asyncio.create_task(_push_subscriber_loop(app))
        sweep_task = asyncio.create_task(_activity_sweep_loop(app))
        delivery_sweep_task = asyncio.create_task(_delivery_resound_sweep_loop(app))

    enrich_task: asyncio.Task[None] | None = None
    if settings.face_enrich.enabled:
        from marcellus.faces import enrich as _enrich

        try:
            _enrich.check_available()
        except _enrich.EnrichUnavailable:
            # Loud at startup, not a silent every-15s failure loop.
            logger.exception("face_enrich enabled but its dependencies are missing")
        else:
            enrich_task = asyncio.create_task(_face_enrich_loop(app))

    try:
        yield
    finally:
        for pending in (
            task,
            probe_task,
            push_task,
            sweep_task,
            delivery_sweep_task,
            enrich_task,
            encounters_task,
            encounters_worker_task,
        ):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        if scrub_lock is not None:
            scrub_lock.release()
        delivery.cancel_deferred()
        running_subscriber = getattr(app.state, "push_subscriber", None)
        if running_subscriber is not None:
            running_subscriber.stop()
        running_engine = getattr(app.state, "push_engine", None)
        if running_engine is not None:
            await running_engine.aclose()
        running_transport = getattr(app.state, "push_transport", None)
        if isinstance(running_transport, RelayTransport):
            await running_transport.aclose()
        client = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()
        stream_client = getattr(app.state, "stream_http_client", None)
        if stream_client is not None:
            await stream_client.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="Marcellus",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    # Cache-buster for /static assets: the newest mtime under static/.
    # Safari heuristically caches JS/CSS (no Cache-Control on StaticFiles),
    # so a deploy could leave phones running old JS against new HTML —
    # `?v=` makes every deploy a brand-new URL.
    _asset_v = 0
    for _p in _STATIC_DIR.rglob("*"):
        if _p.is_file():
            _asset_v = max(_asset_v, int(_p.stat().st_mtime))
    app.state.templates.env.globals["asset_v"] = _asset_v
    app.state.templates.env.globals.update(
        {
            "fmt_ts": fmt.fmt_ts,
            "fmt_score": fmt.fmt_score,
            "fmt_duration": fmt.fmt_duration,
            "fmt_bytes": fmt.fmt_bytes,
            # Display names for stored triage labels (tp/fp/skip stay the
            # wire/DB values — the app and API depend on them).
            "triage_name": {"tp": "real", "fp": "false alarm", "skip": "skip"}.get,
        }
    )
    app.state.plus_enabled = False
    # User guide topics (guide_content/*.md): loaded in the factory, not the
    # lifespan, so a malformed topic fails `create_app()` — and tests — fast.
    app.state.guide = load_guide()
    # /healthz uses this as the grace window before a never-completed scrub
    # cycle counts as stale.
    app.state.started_at = time.time()

    @app.exception_handler(FrigateDBMissingError)
    async def _frigate_db_missing(request: Request, exc: FrigateDBMissingError) -> object:
        # A dev instance without Frigate's SQLite (this Mac): the triage and
        # analysis surfaces can't work, but they should say so instead of
        # 500ing. Pages get a friendly empty state; API callers get 503 JSON.
        # Never reached for /v1 or the proxy — those don't open the DB.
        wants_html = "text/html" in request.headers.get("accept", "")
        if request.method == "GET" and wants_html:
            page = "triage" if request.url.path.startswith(("/triage", "/event/")) else ""
            return app.state.templates.TemplateResponse(
                request,
                "frigate_db_missing.html",
                {"page": page, "db_path": settings.frigate.db_path},
            )
        return JSONResponse(
            status_code=503,
            content={"detail": error_detail("frigate_db_missing", str(exc))},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> object:
        # Additive on top of FastAPI's default {"detail": [...]} body: keeps
        # the same {"error", "message"} envelope every other 4xx/5xx uses,
        # while still surfacing the full per-field error list for debugging.
        errors = exc.errors()
        first_message = str(errors[0]["msg"]) if errors else "invalid request"
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    **error_detail("validation", first_message),
                    "errors": jsonable_encoder(errors),
                }
            },
        )

    app.mount("/static", _CachedStaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(health_routes.router)
    app.include_router(status_routes.router)
    app.include_router(scrub_ui_routes.router)
    app.include_router(debug_routes.router)
    app.include_router(zone_hits_routes.router)
    app.include_router(triage_routes.router)
    app.include_router(encounters_routes.router)
    app.include_router(encounters_routes.v1_router)
    app.include_router(motion_routes.router)
    app.include_router(score_histogram_routes.router)
    app.include_router(fps_budget_routes.router)
    app.include_router(placement_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(face_capture_routes.router)
    app.include_router(enrich_routes.router)
    app.include_router(toybox_routes.router)
    app.include_router(scrub_routes.router)
    app.include_router(search_routes.router)
    app.include_router(push_routes.router)
    app.include_router(push_settings_routes.router)
    app.include_router(tuning_routes.router)
    app.include_router(push_floorplan_routes.router)
    app.include_router(push_map_routes.router)
    app.include_router(replay_routes.router)
    app.include_router(settings_page_routes.router)
    app.include_router(map_page_routes.router)
    app.include_router(login_page_routes.router)
    app.include_router(guide_routes.router)

    # Everything registered so far is the sidecar's own surface and requires a
    # Frigate session; the proxy catch-all below must not (Frigate does its own
    # auth and its 401 has to reach the client).
    owned_routes = [r for r in app.routes if getattr(r, "path", None) != _PROXY_CATCH_ALL]
    app.add_middleware(FrigateAuthMiddleware, owned_routes=owned_routes)
    # Compress HTML/JSON responses (auth runs inside, gzip outside).
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Proxy is a catch-all (/{path:path}) and MUST be registered last so every
    # other route -- /v1/*, /static, /healthz, the sidecar's own pages -- wins
    # first (docs/scrub-cache-and-proxy-spec.md §6).
    app.include_router(proxy_routes.router)
    return app


def run() -> None:
    import uvicorn

    settings = load_settings()
    # uvicorn's log_level only configures uvicorn's own loggers; the root logger
    # keeps its WARNING default, so everything the sidecar itself logs below
    # that -- the per-cycle scrub telemetry in particular -- was written and
    # then dropped. The watchdog entry point has always done this; the server
    # never did.
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s%(push_ctx)s: %(message)s",
    )
    # Push-pipeline lines carry " [cam=... track=...]" after the logger name so
    # a start/end/result line can be tied back to the frame that caused it.
    # The filter has to sit on the handler, not the logger: logger filters
    # only see records logged directly to that logger, handler filters see
    # everything that propagates through.
    for handler in logging.getLogger().handlers:
        handler.addFilter(PushContextFilter())
    # httpx logs a line per request at INFO. Every proxied request goes through
    # it, so at this cadence that is thousands of lines an hour burying anything
    # the sidecar has to say (the watchdog quiets it for the same reason).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.sidecar.bind_host,
        port=settings.sidecar.bind_port,
        log_level=settings.log_level.lower(),
    )
