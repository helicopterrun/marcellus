"""Liveness/version endpoints.

/healthz reflects background-task liveness, not just process liveness: the
MQTT subscriber once died silently and stayed down for 41 hours while the
static "ok" healthcheck kept reporting healthy (server.py `_push_subscriber_loop`
docstring). A degraded check returns 503 so the Docker/compose healthchecks
(which treat any non-2xx as unhealthy) and plain `curl -f` both notice.

Frigate reachability (`checks["frigate"]`) is still informational -- a
Frigate outage must not restart the *sidecar* (`watchdog.py` restarts the
Frigate container; a sidecar restart loop would only drop push/MQTT state).
What DOES gate the status code, since the 2026-09-10 incident, is the
sidecar's *own* ability to reach Frigate through the proxy's own path: the
probe hits `settings.frigate.proxy_base_url` through the same stream-client
pool `routes/proxy.py` uses (frigate_api.get_stream_client) and reports
`proxy_stalled` on a timeout or pool error, with `reason: proxy_stalled` in
the body. That day every Frigate-backed route hung/502'd for hours while
`/healthz` stayed 200, because the probe used its own fresh connection
against a different origin and never saw the wedged proxy. `checks
["upstream_pool"]` and `checks["api_pool"]` additionally report pool stats
for both pools and flip to degraded when a pool is at `max_connections` with
nothing idle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from marcellus import __version__, db
from marcellus.frigate_api import (
    _DEFAULT_LIMITS,
    _STREAM_LIMITS,
    get_stream_client,
    pool_stats,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["meta"])

# How long the stream pool has to stay wedged (all connections active, none
# idle -- see routes/proxy.py's incident note on why a cancelled header-wait
# could leave a connection stuck ACTIVE forever) before /healthz gives up
# waiting for it to clear on its own and recycles the client. Long enough
# that a real, brief burst of concurrent viewers isn't mistaken for a wedge;
# short enough that a genuine wedge (previously: a restart, by hand) clears
# within a couple of probe intervals.
_STALLED_POOL_RECYCLE_S = 30.0


def _note_stalled_pool(app: Any, now: float) -> None:
    """Record when the stream pool was first observed saturated.

    Idempotent across repeated saturated probes: only the first observation
    sets the timestamp, so `_STALLED_POOL_RECYCLE_S` measures how long the
    condition has *persisted*, not just that it was seen once.
    """
    if getattr(app.state, "_stalled_pool_since", None) is None:
        app.state._stalled_pool_since = now


def _recycle_stream_client(app: Any) -> None:
    """Swap in a fresh stream client and retire the wedged one in the background.

    The old client's transport may hang closing the wedged connections (that
    is the whole problem), so it must never be awaited inline here -- this
    function runs from inside the `/healthz` request path.
    """
    old = getattr(app.state, "stream_http_client", None)
    logger.warning(
        "healthz: stream pool wedged for >=%.0fs, recycling stream client",
        _STALLED_POOL_RECYCLE_S,
    )
    get_stream_client_forced_fresh(app)
    app.state._stalled_pool_since = None
    if old is not None:

        async def _close_old() -> None:
            with contextlib.suppress(Exception):
                await old.aclose()

        asyncio.ensure_future(_close_old())


def get_stream_client_forced_fresh(app: Any) -> httpx.AsyncClient:
    """Build a brand-new stream client via the same factory `get_stream_client` uses.

    `get_stream_client()` reuses `app.state.stream_http_client` if it's set
    and open, so recycling has to build the replacement before installing it
    rather than calling that function directly against the still-wedged one.
    """
    app.state.stream_http_client = None
    return get_stream_client(app)


# A scrub cycle that hasn't finished in this many ticks is stuck, not slow --
# the loop is deadline-based, so healthy cycles land every tick even when the
# cache is cold. Generous on purpose: a restart storm is worse than a late alarm.
_SCRUB_STALE_TICKS = 10

# Frigate reachability probe: cached at app-state level so /healthz polls
# (Docker/systemd hit this every few seconds) don't hammer Frigate with an
# extra request on top of everything else already probing it.
#
# Shortened from 30s: the proxy-path probe below (2026-09-12) is what
# actually catches a wedged proxy (CLOSE-WAIT pile-up on Frigate's nginx
# half-closing keepalive sockets while /healthz kept reporting ok) -- a 30s
# cache let that condition go undetected for up to half a minute per poll.
_FRIGATE_PROBE_INTERVAL_S = 10.0
_FRIGATE_PROBE_TIMEOUT_S = 3.0

# The proxy-path probe below has its own tighter total budget: it exists
# specifically to catch a wedged proxy fast, so it shouldn't itself wait as
# long as the plain reachability probe above.
_PROXY_PROBE_TIMEOUT_S = 2.0


# How long the Protect websocket may stay disconnected before /healthz
# calls it "down" rather than "degraded" -- long enough that a normal
# reconnect-with-backoff cycle (compute_backoff caps at 60s) isn't flagged
# as an outage on its own.
_PROTECT_DISCONNECT_DOWN_S = 120.0


def _protect_health_state(subscriber: Any, now: float) -> str:
    """ok / degraded / down for `checks["unifi_protect"]` (M-1 spec).

    down: websocket disconnected for more than `_PROTECT_DISCONNECT_DOWN_S`
    (covers both "never connected since start" -- `disconnected_since` is
    seeded at construction -- and "dropped and hasn't come back").
    degraded: connected, but a mapped camera isn't CONNECTED, the last
    device poll failed, or no poll has completed yet.
    ok: connected, every mapped camera CONNECTED, last poll succeeded.
    """
    disconnected_since = getattr(subscriber, "disconnected_since", None)
    if disconnected_since is not None and (now - disconnected_since) > _PROTECT_DISCONNECT_DOWN_S:
        return "down"

    status = subscriber.status()
    if not status.get("connected"):
        return "degraded"
    if status.get("last_poll_at") is None:
        return "degraded"
    if status.get("last_poll_error"):
        return "degraded"
    if not status.get("mapped_cameras_connected"):
        return "degraded"
    return "ok"


async def _probe_frigate(app: Any, settings: Any, now: float) -> tuple[str, str | None]:
    """Cheap `/api/version` check through the proxy's own base URL and
    stream-client pool, rate-limited per app instance.

    Goes through `settings.frigate.proxy_base_url` -- the same origin
    routes/proxy.py forwards `/api/*` to -- and `get_stream_client()`, the
    same pool the proxy uses, so this probe sees exactly what a real proxied
    request would see. The 2026-09-10 incident showed `/api/version` against
    `frigate.base_url` on a throwaway client staying "ok" for hours while the
    proxy's own pool was wedged; probing the proxy's own path/pool is what
    catches that.

    Returns `(status, reason)`. `status` is `ok` / `error` (non-200) /
    `unreachable` (connect/read failure) -- all informational, see module
    docstring -- or `proxy_stalled`, the one outcome that gates /healthz: a
    timeout or pool error on the proxy's own path means the sidecar's proxy
    is wedged and no proxied request can get through, which a restart fixes.
    """
    cache = getattr(app.state, "_frigate_health_cache", None)
    if cache is not None and (now - cache[0]) < _FRIGATE_PROBE_INTERVAL_S:
        return cache[1]  # type: ignore[no-any-return]
    url = settings.frigate.proxy_base_url.rstrip("/") + "/api/version"
    timeout = httpx.Timeout(_PROXY_PROBE_TIMEOUT_S, pool=_PROXY_PROBE_TIMEOUT_S)
    result: tuple[str, str | None]
    try:
        stream_client = get_stream_client(app)
        resp = await stream_client.get(url, timeout=timeout)
        # The proxy origin is Frigate's authenticated port, so `/api/version`
        # answers 401 here -- any HTTP answer at all proves the proxy path
        # and pool are alive, which is what this probe is for. Only a 5xx
        # (Frigate up but broken) reads as `error`.
        result = ("ok" if resp.status_code < 500 else "error", None)
    except (httpx.PoolTimeout, httpx.TimeoutException):
        result = ("proxy_stalled", "proxy_stalled")
    except httpx.HTTPError:
        result = ("unreachable", None)
    app.state._frigate_health_cache = (now, result)
    return result


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    app = request.app
    settings = app.state.settings
    now = time.time()
    checks: dict[str, Any] = {}
    ok = True

    frigate_status, frigate_reason = await _probe_frigate(app, settings, now)
    checks["frigate"] = frigate_status
    reason: str | None = None
    if frigate_status == "proxy_stalled":
        ok = False
        reason = frigate_reason

    stream_client = getattr(app.state, "stream_http_client", None)
    if stream_client is not None:
        stats = pool_stats(stream_client)
        if stats:
            checks["upstream_pool"] = stats
            max_connections = _STREAM_LIMITS.max_connections
            saturated = (
                max_connections is not None
                and stats.get("connections", 0) >= max_connections
                and stats.get("idle", 0) == 0
            )
            if saturated:
                ok = False
                _note_stalled_pool(app, now)
                since = app.state._stalled_pool_since
                if since is not None and now - since >= _STALLED_POOL_RECYCLE_S:
                    _recycle_stream_client(app)
            else:
                app.state._stalled_pool_since = None

    http_client = getattr(app.state, "http_client", None)
    if http_client is not None:
        stats = pool_stats(http_client)
        if stats:
            checks["api_pool"] = stats
            max_connections = _DEFAULT_LIMITS.max_connections
            saturated = (
                max_connections is not None
                and stats.get("connections", 0) >= max_connections
                and stats.get("idle", 0) == 0
            )
            if saturated:
                ok = False

    # `push_subscriber` is set by the lifespan when push starts; its absence
    # means the lifespan hasn't run (tests, bare create_app under another
    # runner), where "degraded" would be noise rather than signal.
    subscriber = getattr(app.state, "push_subscriber", None)
    if settings.push.enabled and subscriber is not None:
        connected = subscriber.connected
        checks["mqtt"] = "connected" if connected else "disconnected"
        # Disconnected is degraded even during startup/backoff: push is not
        # being delivered either way, and the reconnect loop clears it fast.
        ok = ok and connected

        try:
            conn = db.open_sidecar(str(settings.sidecar.db_path))
            try:
                conn.execute("SELECT 1")
            finally:
                conn.close()
            checks["db"] = "ok"
        except Exception:
            checks["db"] = "error"
            ok = False

    scrub_low_disk: bool | None = None
    if settings.scrub.enabled:
        scrub_low_disk = bool(getattr(app.state, "scrub_low_disk", False))
        if getattr(app.state, "scrub_locked", False):
            # Another process (a restarting predecessor, or a concurrent CLI
            # `fsc scrub` invocation) holds the cache lock -- the generation
            # loop was never started. Distinct from "stale" (a loop that
            # started and died) so an operator knows to check for a stray
            # process rather than a wedge.
            checks["scrub"] = "locked"
            ok = False
        else:
            tick = min(settings.scrub.generate_interval_s, settings.scrub.live_edge_interval_s)
            last_cycle = getattr(app.state, "scrub_last_cycle", None)
            started_at = getattr(app.state, "started_at", now)
            if last_cycle is not None:
                age = now - last_cycle
                checks["scrub_last_cycle_age_s"] = round(age, 1)
                if age > tick * _SCRUB_STALE_TICKS:
                    checks["scrub"] = "stale"
                    ok = False
                else:
                    checks["scrub"] = "ok"
            elif now - started_at > tick * _SCRUB_STALE_TICKS:
                # Never completed a cycle and we're well past startup grace.
                checks["scrub"] = "stale"
                ok = False
            else:
                checks["scrub"] = "starting"

    if settings.face_enrich.enabled:
        # Same staleness shape as scrub. The worker stamps last_cycle only on
        # a completed run_cycle, so a wedged model load or a dead task both
        # read as stale here rather than as silence.
        tick = settings.face_enrich.interval_s
        last_cycle = getattr(app.state, "face_enrich_last_cycle", None)
        started_at = getattr(app.state, "started_at", now)
        if last_cycle is not None:
            age = now - last_cycle
            checks["face_enrich_last_cycle_age_s"] = round(age, 1)
            if age > tick * _SCRUB_STALE_TICKS:
                checks["face_enrich"] = "stale"
                ok = False
            else:
                checks["face_enrich"] = "ok"
        elif now - started_at > tick * _SCRUB_STALE_TICKS:
            checks["face_enrich"] = "stale"
            ok = False
        else:
            checks["face_enrich"] = "starting"

    if settings.encounters.enabled:
        encounters_service = getattr(app.state, "encounters", None)
        if encounters_service is not None:
            enc_status = encounters_service.status()
            checks["encounters"] = enc_status["state"]
            if "last_reconcile" in enc_status:
                checks["encounters_last_reconcile"] = enc_status["last_reconcile"]
            if enc_status["state"] == "error":
                ok = False
        else:
            checks["encounters"] = "starting"
    else:
        checks["encounters"] = "disabled"

    if settings.unifi_protect.enabled:
        protect_subscriber = getattr(app.state, "protect_subscriber", None)
        if protect_subscriber is None:
            checks["unifi_protect"] = "degraded"
            ok = False
            if reason is None:
                reason = "unifi_protect"
        else:
            protect_status = _protect_health_state(protect_subscriber, now)
            checks["unifi_protect"] = protect_status
            if protect_status == "down":
                ok = False
                if reason is None:
                    reason = "unifi_protect"
            protect_last_ring_at = protect_subscriber.status().get("last_ring_at")
            if protect_last_ring_at is not None:
                checks["unifi_protect_last_ring_age_s"] = round(now - protect_last_ring_at, 1)

    body: dict[str, Any] = {"status": "ok" if ok else "degraded", "checks": checks}
    if reason is not None:
        body["reason"] = reason
    if scrub_low_disk is not None:
        body["scrub_low_disk"] = scrub_low_disk
    return JSONResponse(body, status_code=200 if ok else 503)


@router.get("/version")
def version() -> dict[str, str]:
    return {"version": __version__}
