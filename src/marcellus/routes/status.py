"""Status dashboard: the landing page for a distributed install.

`GET /` renders the page server-side; `GET /status.json` returns the same
payload for the page's ~10s refresh loop. Deliberately NOT under /v1 -- the
iOS capabilities contract stays untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from marcellus import __version__, db
from marcellus.auth import _cache
from marcellus.errors import error_detail
from marcellus.push.stats import STATS

router = APIRouter(tags=["status"])

_PROBE_TIMEOUT_S = 3.0


def _file_size(path: object) -> int | None:
    try:
        return os.stat(str(path)).st_size
    except OSError:
        return None


async def _probe_frigate(base_url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(base_url.rstrip("/") + "/api/version")
            resp.raise_for_status()
            return {"reachable": True, "version": resp.text.strip()}
    except Exception as exc:  # noqa: BLE001 -- any failure is just "unreachable"
        return {"reachable": False, "error": str(exc)}


def _scrub_status(settings: Any, now: float, last_cycle: float | None) -> dict[str, Any]:
    scrub = {
        "enabled": settings.scrub.enabled,
        "last_cycle_s_ago": (round(now - last_cycle, 1) if last_cycle else None),
        "cameras": [],
        "cache_bytes": None,
        "cache_bytes_capped": False,
    }
    if not settings.scrub.enabled:
        return scrub
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        cams = [
            r["camera"]
            for r in conn.execute("SELECT DISTINCT camera FROM scrub_buckets ORDER BY camera")
        ]
        exclude = tuple(settings.scrub.derived_intervals_s)
        for cam in cams:
            through = db.latest_generated_through(conn, cam, exclude_intervals_s=exclude)
            scrub["cameras"].append(
                {
                    "camera": cam,
                    "lag_s": (round(now - through, 1) if through else None),
                }
            )
        row = conn.execute("SELECT COUNT(*) AS n FROM scrub_sheets").fetchone()
        scrub["sheet_count"] = int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 -- schema may predate these tables
        pass
    finally:
        conn.close()
    scrub["cache_bytes"], scrub["cache_bytes_capped"] = _dir_size_capped(
        settings.scrub.cache_dir
    )
    return scrub


def _dir_size_capped(root: object, max_files: int = 50_000) -> tuple[int | None, bool]:
    """(total bytes under `root`, capped?). Stops counting past `max_files`
    so a pathological cache can't stall the status page — the partial total
    is still reported, flagged as a floor rather than an exact size."""
    total = 0
    seen = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(str(root)):
            for name in filenames:
                seen += 1
                if seen > max_files:
                    return total, True
                with contextlib.suppress(OSError):
                    total += os.stat(os.path.join(dirpath, name)).st_size
    except OSError:
        return None, False
    return total, False


def _frigate_hardware(settings: Any) -> dict[str, Any]:
    """Detector / CPU / GPU / storage numbers from Frigate's /api/stats.

    Everything degrades to absent keys — the status page never 500s over a
    slow or missing Frigate."""
    from marcellus.frigate_api import FrigateClient

    hw: dict[str, Any] = {"available": False}
    try:
        with FrigateClient(settings.frigate.base_url, timeout=_PROBE_TIMEOUT_S) as client:
            stats = client.stats()
    except Exception:  # noqa: BLE001 -- unreachable Frigate is just "no data"
        return hw
    hw["available"] = True
    with contextlib.suppress(Exception):
        hw["detectors"] = {
            name: round(float(d.get("inference_speed", 0.0)), 1)
            for name, d in stats.get("detectors", {}).items()
        }
    with contextlib.suppress(Exception):
        hw["detection_fps"] = stats.get("detection_fps")
        sys_row = stats.get("cpu_usages", {}).get("frigate.full_system", {})
        hw["frigate_cpu"] = float(sys_row["cpu"])
        hw["frigate_mem"] = float(sys_row["mem"])
    with contextlib.suppress(Exception):
        gpus = stats.get("gpu_usages") or {}
        name, row = next(iter(gpus.items()))
        hw["gpu"] = {"name": name, "usage": row.get("gpu")}
    with contextlib.suppress(Exception):
        storage = {}
        for mount, row in (stats.get("service", {}).get("storage") or {}).items():
            total = float(row.get("total") or 0)
            used = float(row.get("used") or 0)
            if total:
                # Frigate reports MB.
                storage[mount] = {
                    "total_bytes": int(total * 1024 * 1024),
                    "used_bytes": int(used * 1024 * 1024),
                    "pct": round(100 * used / total, 1),
                }
        hw["storage"] = storage
        hw["frigate_uptime_s"] = stats.get("service", {}).get("uptime")
    return hw


def _host_stats(settings: Any) -> dict[str, Any]:
    """Sidecar-host vitals, stdlib only (LXC/Docker on Linux; dev on macOS)."""
    host: dict[str, Any] = {}
    with contextlib.suppress(OSError):
        load1, load5, _ = os.getloadavg()
        host["load_1m"] = round(load1, 2)
        host["load_5m"] = round(load5, 2)
        host["cpus"] = os.cpu_count()
    with contextlib.suppress(Exception):
        import shutil

        du = shutil.disk_usage(str(settings.sidecar.db_path.parent))
        host["disk_total_bytes"] = du.total
        host["disk_used_bytes"] = du.total - du.free
        host["disk_pct"] = round(100 * (du.total - du.free) / du.total, 1)
    with contextlib.suppress(Exception):
        # Linux only; absent on the dev Mac. MemAvailable is the honest number.
        info: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024
        host["mem_total_bytes"] = info["MemTotal"]
        host["mem_used_bytes"] = info["MemTotal"] - info["MemAvailable"]
        host["mem_pct"] = round(100 * host["mem_used_bytes"] / info["MemTotal"], 1)
    with contextlib.suppress(Exception):
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS bytes.
        host["rss_bytes"] = rss * 1024 if rss < 1 << 32 else rss
    return host


def _push_status(app_state: Any, settings: Any) -> dict[str, Any]:
    push: dict[str, Any] = {
        "enabled": settings.push.enabled,
        "transport": settings.push.transport,
        "device_count": 0,
        "mqtt_connected": None,
        "frigate_online": None,
        "last_event_s_ago": None,
    }
    with contextlib.suppress(Exception):
        conn = db.open_sidecar(settings.sidecar.db_path)
        try:
            from marcellus.push import store

            push["device_count"] = len(store.list_devices(conn))
        finally:
            conn.close()
    subscriber = getattr(app_state, "push_subscriber", None)
    if subscriber is not None:
        push["mqtt_connected"] = not subscriber.is_stale()
        push["frigate_online"] = subscriber.frigate_online
        push["last_event_s_ago"] = round(time.time() - subscriber.last_seen, 1)
    return push


def _counts(request: Request, settings: Any) -> dict[str, Any]:
    """Additive status-page counters (Wave 6B-2): one query/attribute each,
    degrading to zeros rather than a 500 on an empty or predating-column DB.
    """
    counts: dict[str, Any] = {
        "uptime_s": round(time.time() - STATS.started_at, 1),
        "face_enrich": {},
        "face_clusters": {"named": 0, "unnamed": 0},
        "face_captures_pending": 0,
        "push_24h": {},
        "live_activities_open": 0,
        "auth": {
            "cache": len(_cache(request.app)),
            "login_rate_limit_rejections": getattr(
                request.app.state, "login_rate_limit_rejections", 0
            ),
        },
        "triage_labels": {"count": 0, "last_labeled_at": None},
    }
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        with contextlib.suppress(Exception):
            enrich = {
                str(r["status"]): int(r["n"])
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM face_enrichments "
                    "WHERE excluded_at IS NULL GROUP BY status"
                )
            }
            excluded = conn.execute(
                "SELECT COUNT(*) AS n FROM face_enrichments WHERE excluded_at IS NOT NULL"
            ).fetchone()
            enrich["excluded"] = int(excluded["n"]) if excluded else 0
            counts["face_enrich"] = enrich
        with contextlib.suppress(Exception):
            row = conn.execute(
                "SELECT SUM(name IS NOT NULL) AS named, SUM(name IS NULL) AS unnamed "
                "FROM face_clusters"
            ).fetchone()
            if row is not None:
                counts["face_clusters"] = {
                    "named": int(row["named"] or 0),
                    "unnamed": int(row["unnamed"] or 0),
                }
        with contextlib.suppress(Exception):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM face_captures WHERE review = 'pending'"
            ).fetchone()
            counts["face_captures_pending"] = int(row["n"]) if row else 0
        with contextlib.suppress(Exception):
            # push_sends has a timestamp (sent_at) but no outcome column, so
            # "by outcome" isn't derivable from this table -- report the
            # last-24h total here and fall back to STATS's own counters for
            # an outcome breakdown.
            since = time.time() - 86400
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM push_sends WHERE sent_at >= ?", (since,)
            ).fetchone()
            snapshot = STATS.snapshot()
            counts["push_24h"] = {
                "sent_total": int(row["n"]) if row else 0,
                "counters": snapshot.get("counters", {}),
            }
        with contextlib.suppress(Exception):
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM push_activities WHERE ended_at IS NULL"
            ).fetchone()
            counts["live_activities_open"] = int(row["n"]) if row else 0
        with contextlib.suppress(Exception):
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(labeled_at) AS last FROM triage_labels"
            ).fetchone()
            if row is not None:
                counts["triage_labels"] = {
                    "count": int(row["n"] or 0),
                    "last_labeled_at": row["last"],
                }
    finally:
        conn.close()
    return counts


def _camera_names(settings: Any) -> list[str]:
    with contextlib.suppress(Exception):
        from marcellus.zones import load_camera_zones

        return sorted(load_camera_zones(settings.frigate.config_path).keys())
    return []


async def _gather_status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    now = time.time()
    frigate_task = asyncio.create_task(_probe_frigate(settings.frigate.base_url))
    scrub = await asyncio.to_thread(
        _scrub_status, settings, now, getattr(request.app.state, "scrub_last_cycle", None)
    )
    push = await asyncio.to_thread(_push_status, request.app.state, settings)
    hardware = await asyncio.to_thread(_frigate_hardware, settings)
    hardware["host"] = await asyncio.to_thread(_host_stats, settings)
    counts = await asyncio.to_thread(_counts, request, settings)
    return {
        "version": __version__,
        "time": now,
        "cameras": _camera_names(settings),
        "frigate": await frigate_task,
        "proxy_enabled": settings.proxy.enabled,
        "scrub": scrub,
        "push": push,
        # Wave 2A: relay retry/breaker + MQTT queue counters, same snapshot
        # GET /v1/stats returns -- one source of truth for the status page.
        "push_stats": STATS.snapshot(),
        "hardware": hardware,
        "counts": counts,
        "sizes": {
            "sidecar_db": _file_size(settings.sidecar.db_path),
            "frigate_db": _file_size(settings.frigate.db_path),
            "scrub_cache": scrub.get("cache_bytes"),
            "scrub_cache_capped": scrub.get("cache_bytes_capped", False),
        },
    }


@router.get("/status.json")
async def status_json(request: Request) -> dict[str, Any]:
    return await _gather_status(request)


@router.get("/v1/stats")
async def v1_stats(request: Request) -> dict[str, Any]:
    """Push-pipeline counters/gauges (wave 2A): `STATS.snapshot()` plus a
    couple of derived conveniences a client shouldn't have to compute itself
    from the raw gauge names. Registered on the same (owned-route) router as
    every other sidecar page, so it picks up the shared Frigate-session gate
    (`FrigateAuthMiddleware`) automatically -- no separate auth wiring."""
    settings = request.app.state.settings
    snapshot = STATS.snapshot()
    gauges = snapshot.get("gauges", {})
    return {
        **snapshot,
        "relay": {"breaker_open": bool(gauges.get("relay.breaker.state", 0))},
        "queue": {
            "depth": gauges.get("mqtt.queue.depth", 0),
            "max": settings.push.mqtt_queue_max,
        },
    }


@router.get("/cameras", response_class=HTMLResponse)
def cameras_page(request: Request) -> Any:
    """Live camera grid: snapshot tiles with scrub live-edge lag badges."""
    settings = request.app.state.settings
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "cameras.html",
        {"cameras": _camera_names(settings), "scrub_enabled": settings.scrub.enabled},
    )


@router.get("/live/{camera}", response_class=HTMLResponse)
def live_view(request: Request, camera: str) -> Any:
    """Single-camera live view: Frigate's MJPEG feed through the proxy."""
    settings = request.app.state.settings
    cameras = _camera_names(settings)
    if camera not in cameras:
        raise HTTPException(
            status_code=404, detail=error_detail("unknown_camera", "unknown camera")
        )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "live.html", {"camera": camera, "cameras": cameras}
    )


@router.get("/", response_class=HTMLResponse)
async def status_page(request: Request) -> Any:
    templates = request.app.state.templates
    status = await _gather_status(request)
    return templates.TemplateResponse(
        request,
        "status.html",
        {"status": status, "counts": {}},
    )
