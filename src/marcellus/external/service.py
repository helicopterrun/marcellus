"""Long-lived service for external event sources.

Owns per-source poll loops (the record), the MQTT hub (the trigger), the
debounced refresh, the retention prune, and an in-memory health model.
Adapters are pure; all journal I/O goes through `db.with_sidecar`.
`health()` / `health_checks()` / `capabilities()` are pure in-memory so
`/healthz` never does I/O on their account.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from starlette.responses import Response

from marcellus import db
from marcellus.config import Settings
from marcellus.external import store
from marcellus.external.media import SourceHttp
from marcellus.external.mqtt_hub import ExternalMqttHub
from marcellus.external.registry import build_sources
from marcellus.external.types import (
    AlertSignal,
    Availability,
    ExternalEventSource,
    Sighting,
    SightingHint,
    Signal,
    StateReport,
)
from marcellus.push.mqtt import compute_backoff

logger = logging.getLogger(__name__)

#: How far back an unsettled row is re-checked individually.
OPEN_RECHECK_WINDOW_S = 6 * 3600.0
DEBOUNCE_S = 2.0
FOLLOWUP_S = 90.0
PRUNE_INTERVAL_S = 3600.0
_DEFAULT_POLL_LIMIT = 50


@dataclasses.dataclass
class _SourceRuntime:
    last_poll_at: float | None = None
    last_poll_ok_at: float | None = None
    last_poll_error: str | None = None
    consecutive_failures: int = 0
    online: bool | None = None
    online_at: float | None = None
    state_payload: dict[str, Any] | None = None
    state_at: float | None = None
    alerts: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    last_event_start: float | None = None
    persisted_state: dict[str, Any] | None = None


class ExternalSourceService:
    def __init__(
        self,
        settings: Settings,
        *,
        sources: Sequence[ExternalEventSource] | None = None,
        http: SourceHttp | None = None,
        hub_factory: Callable[..., ExternalMqttHub] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.section = settings.external_sources
        self.db_path: str = str(settings.sidecar.db_path)
        self._clock = clock
        self.sources: list[ExternalEventSource] = (
            list(sources) if sources is not None else build_sources(self.section, clock=clock)
        )
        self._by_id = {s.id: s for s in self.sources}
        self._cfg_limits = {c.id: c.poll_limit for c in self.section.sources}
        self._configured = {c.id: c for c in self.section.sources}
        self.http = http or SourceHttp(
            self.sources,
            timeout_s={c.id: c.request_timeout_s for c in self.section.sources},
        )
        self._rt: dict[str, _SourceRuntime] = {s.id: _SourceRuntime() for s in self.sources}
        self.started_at: float = clock()
        self.debounce_s = DEBOUNCE_S
        self.followup_s = FOLLOWUP_S
        self._tasks: list[asyncio.Task[Any]] = []
        self._debounce: dict[str, asyncio.Task[None]] = {}
        self._followup: dict[str, asyncio.Task[None]] = {}

        self.hub: ExternalMqttHub | None = None
        mqtt_sources = [s for s in self.sources if s.mqtt_topics()]
        if self.section.mqtt_enabled and mqtt_sources:
            host, port, user, pw = self.section.broker(settings.push)
            factory = hub_factory or ExternalMqttHub
            self.hub = factory(
                mqtt_sources,
                host=host,
                port=port,
                username=user,
                password=pw,
                client_id=self.section.mqtt_client_id,
                on_signal=self.on_signal,
            )

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.started_at = self._clock()
        await self.load_persisted_state()
        for src in self.sources:
            self._tasks.append(asyncio.create_task(self._poll_loop(src), name=f"ext-poll-{src.id}"))
        if self.hub is not None:
            self._tasks.append(asyncio.create_task(self._run_hub(self.hub), name="ext-mqtt"))
        self._tasks.append(asyncio.create_task(self._prune_loop(), name="ext-prune"))

    async def aclose(self) -> None:
        tasks = [*self._tasks, *self._debounce.values(), *self._followup.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        self._debounce.clear()
        self._followup.clear()
        await self.http.aclose()

    async def _run_hub(self, hub: ExternalMqttHub) -> None:
        # `hub.run()` reconnects on its own; this only catches a crash in the
        # hub itself, which must not end MQTT ingest for the process lifetime
        # (polling would still work, but every sighting would lag a poll).
        attempt = 0
        while True:
            try:
                await hub.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = compute_backoff(attempt, 5.0, 300.0)
                logger.exception("external sources: mqtt hub crashed, restarting in %.0fs", delay)
                await asyncio.sleep(delay)

    # --------------------------------------------------------- persisted state

    async def load_persisted_state(self) -> None:
        ids = list(self._rt)

        def _load(conn: Any) -> dict[str, dict[str, tuple[Any, float]]]:
            return {sid: store.get_state(conn, sid) for sid in ids}

        try:
            loaded = await db.with_sidecar(self.db_path, _load)
        except Exception:
            logger.exception("external sources: could not load persisted state")
            return
        for sid, rows in loaded.items():
            rt = self._rt[sid]
            for key, (value, _at) in rows.items():
                if key.startswith("alert:") and isinstance(value, dict) and value.get("active"):
                    rt.alerts[key[6:]] = value
                elif key == "state" and isinstance(value, dict):
                    rt.persisted_state = value

    # ----------------------------------------------------------------- polling

    def _poll_limit(self, src: ExternalEventSource) -> int:
        attr = getattr(src, "poll_limit", None)
        if isinstance(attr, int) and attr > 0:
            return attr
        return self._cfg_limits.get(src.id, _DEFAULT_POLL_LIMIT)

    async def _poll_loop(self, src: ExternalEventSource) -> None:
        first = True
        while True:
            await self.poll_once(src.id, backfill=first)
            first = False
            await asyncio.sleep(self.section.poll_interval_s)

    async def poll_once(self, source_id: str, *, backfill: bool = False) -> None:
        """One poll iteration. Never raises except CancelledError."""
        src = self._by_id[source_id]
        rt = self._rt[source_id]
        limit = self._poll_limit(src)
        if backfill:
            limit = max(self.section.backfill_limit, limit)
        try:
            fetched = await src.fetch_recent(self.http.client(source_id), limit)
            await self._store(fetched, "poll")
            await self._recheck_open(src, {s.native_key for s in fetched})
            newest = await db.with_sidecar(self.db_path, lambda c: store.newest_start(c, source_id))
            rt.last_event_start = newest
            rt.last_poll_ok_at = self._clock()
            rt.last_poll_error = None
            rt.consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            self._poll_failed(src, rt, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            logger.exception("external %s: poll iteration failed", source_id)
            self._poll_failed(src, rt, f"{type(exc).__name__}: {exc}")
        finally:
            rt.last_poll_at = self._clock()

    def _poll_failed(self, src: ExternalEventSource, rt: _SourceRuntime, msg: str) -> None:
        rt.consecutive_failures += 1
        rt.last_poll_error = msg
        logger.warning(
            "external %s: poll failed (%d in a row): %s", src.id, rt.consecutive_failures, msg
        )

    async def _store(self, sightings: Sequence[Sighting], origin: Any) -> None:
        if not sightings:
            return
        now = self._clock()

        def _fn(conn: Any) -> None:
            for s in sightings:
                store.upsert_sighting(conn, s, origin, now)

        await db.with_sidecar(self.db_path, _fn)

    async def _recheck_open(self, src: ExternalEventSource, fetched_keys: set[str]) -> None:
        now = self._clock()
        since = now - OPEN_RECHECK_WINDOW_S
        open_rows = await db.with_sidecar(
            self.db_path, lambda c: store.open_sightings(c, src.id, since=since)
        )
        client = self.http.client(src.id)
        for key, native_id in open_rows:
            if key in fetched_keys or not native_id:
                continue
            try:
                got = await src.fetch_one(client, native_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("external %s: recheck of %s failed: %s", src.id, key, exc)
                continue
            if got is not None and got.native_key == key:
                await self._store([got], "poll")
            else:
                await self._settle(src.id, key, now)

    async def _settle(self, source_id: str, key: str, now: float) -> None:
        def _fn(conn: Any) -> None:
            cur = store.get_sighting(conn, source_id, key)
            if cur is None or cur.final:
                return
            status = "failed" if cur.status == "pending" else cur.status
            settled = dataclasses.replace(cur, final=True, status=status)
            store.upsert_sighting(conn, settled, "poll", now)

        await db.with_sidecar(self.db_path, _fn)

    # ------------------------------------------------------------------ prune

    async def _prune_loop(self) -> None:
        while True:
            await self.prune_once()
            await asyncio.sleep(PRUNE_INTERVAL_S)

    async def prune_once(self) -> int:
        cutoff = self._clock() - self.section.retention_days * 86400
        try:
            n = await db.with_sidecar(self.db_path, lambda c: store.prune(c, cutoff))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external sources: prune failed")
            return 0
        if n:
            logger.info("external sources: pruned %d sighting(s)", n)
        return n

    # --------------------------------------------------------------- refresh

    def request_refresh(self, source_id: str) -> None:
        """Coalesce refresh requests within `debounce_s`; one follow-up later."""
        if source_id not in self._by_id:
            return
        if source_id in self._debounce:
            return
        task = asyncio.create_task(self._debounced(source_id), name=f"ext-refresh-{source_id}")
        self._debounce[source_id] = task
        task.add_done_callback(lambda t: self._drop(self._debounce, source_id, t))

    @staticmethod
    def _drop(d: dict[str, asyncio.Task[None]], key: str, task: asyncio.Task[None]) -> None:
        if d.get(key) is task:
            del d[key]

    async def _debounced(self, source_id: str) -> None:
        await asyncio.sleep(self.debounce_s)
        # Leave the debounce slot before fetching so a signal arriving
        # mid-fetch schedules a fresh refresh rather than being swallowed.
        self._debounce.pop(source_id, None)
        if source_id not in self._followup:
            fu = asyncio.create_task(self._followup_refresh(source_id), name=f"ext-fu-{source_id}")
            self._followup[source_id] = fu
            fu.add_done_callback(lambda t: self._drop(self._followup, source_id, t))
        await self.refresh_now(source_id)

    async def _followup_refresh(self, source_id: str) -> None:
        await asyncio.sleep(self.followup_s)
        await self.refresh_now(source_id)

    async def refresh_now(self, source_id: str) -> None:
        src = self._by_id[source_id]
        try:
            fetched = await src.fetch_recent(
                self.http.client(source_id), min(20, self._poll_limit(src))
            )
            await self._store(fetched, "poll")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("external %s: refresh failed: %s", source_id, exc)

    # ---------------------------------------------------------------- signals

    async def on_signal(self, signal: Signal) -> None:
        rt = self._rt.get(signal.source_id)
        if rt is None:
            return
        now = self._clock()
        sid = signal.source_id
        if isinstance(signal, Availability):
            was = rt.online
            rt.online, rt.online_at = signal.online, now
            await self._persist(
                lambda c: store.set_state(c, sid, "availability", {"online": signal.online}, now)
            )
            if was is False and signal.online:
                self.request_refresh(sid)
        elif isinstance(signal, StateReport):
            rt.state_payload, rt.state_at = dict(signal.payload), now
            await self._persist(
                lambda c: store.set_state(c, sid, "state", dict(signal.payload), now)
            )
        elif isinstance(signal, AlertSignal):
            key = f"alert:{signal.code}"
            if signal.active:
                value = {"code": signal.code, "message": signal.message, "active": True}
                rt.alerts[signal.code] = value
                await self._persist(lambda c: store.set_state(c, sid, key, value, now))
            else:
                rt.alerts.pop(signal.code, None)
                await self._persist(lambda c: store.delete_state(c, sid, key))
        elif isinstance(signal, SightingHint):
            if signal.sighting is not None:
                await self._store([signal.sighting], "mqtt")
            self.request_refresh(sid)

    async def _persist(self, fn: Callable[[Any], Any]) -> None:
        try:
            await db.with_sidecar(self.db_path, fn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("external sources: persisting state failed")

    # ----------------------------------------------------------------- health

    def _state(self, source_id: str) -> str:
        cfg = self._configured.get(source_id)
        if source_id not in self._by_id:
            return "disabled"
        if cfg is not None and not cfg.enabled:
            return "disabled"
        rt = self._rt[source_id]
        interval = self.section.poll_interval_s
        now = self._clock()
        in_grace = now - self.started_at < 2 * interval
        polled = rt.last_poll_ok_at is not None
        if not polled and in_grace:
            return "starting"
        if rt.online is False:
            return "offline"
        if rt.consecutive_failures >= 2 or not polled:
            return "unreachable"
        src = self._by_id[source_id]
        if self.section.mqtt_enabled and src.mqtt_topics():
            if rt.state_at is None:
                if not in_grace:
                    return "stale"
            elif now - rt.state_at > self.section.state_stale_s:
                return "stale"
        if rt.alerts:
            return "alert"
        return "ok"

    def health(self, source_id: str) -> dict[str, Any]:
        state = self._state(source_id)
        rt = self._rt.get(source_id)
        if rt is None:
            return {"state": state}
        now = self._clock()
        return {
            "state": state,
            "last_poll_at": rt.last_poll_at,
            "last_poll_error": rt.last_poll_error,
            "consecutive_failures": rt.consecutive_failures,
            "online": rt.online,
            "last_event_age_s": None
            if rt.last_event_start is None
            else max(0.0, now - rt.last_event_start),
            "state_age_s": None if rt.state_at is None else max(0.0, now - rt.state_at),
            "alerts": sorted(rt.alerts),
        }

    def health_checks(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        ids = list(self._configured) + [i for i in self._by_id if i not in self._configured]
        for sid in ids:
            h = self.health(sid)
            out[f"source:{sid}"] = h["state"]
            out[f"source:{sid}:last_event_age_s"] = h.get("last_event_age_s")
            out[f"source:{sid}:state_age_s"] = h.get("state_age_s")
            out[f"source:{sid}:alerts"] = h.get("alerts", [])
        return out

    def capabilities(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for cfg in self.section.sources:
            src = self._by_id.get(cfg.id)
            if src is None or not cfg.enabled:
                rows.append(
                    {
                        "id": cfg.id,
                        "kind": cfg.kind,
                        "name": cfg.name,
                        "enabled": False,
                        "healthy": False,
                        "subjects": [],
                        "media": [],
                    }
                )
                continue
            rows.append(self._cap_row(src))
        for src in self.sources:
            if src.id not in self._configured:
                rows.append(self._cap_row(src))
        return {"enabled": True, "sources": rows}

    def _cap_row(self, src: ExternalEventSource) -> dict[str, Any]:
        return {
            "id": src.id,
            "kind": src.kind,
            "name": src.name,
            "enabled": True,
            "healthy": self._state(src.id) in ("ok", "alert", "stale"),
            "subjects": list(src.subjects),
            "media": list(src.media),
        }

    # ------------------------------------------------------- route interface

    def source_name(self, source_id: str) -> str | None:
        src = self._by_id.get(source_id)
        return None if src is None else src.name

    def source_names(self) -> dict[str, str]:
        return {s.id: s.name for s in self.sources}

    async def relay_media(self, source_id: str, upstream_path: str, headers: Any) -> Response:
        return await self.http.relay(source_id, upstream_path, headers)

    def mqtt_status(self) -> dict[str, Any]:
        if self.hub is None:
            return {"state": "disabled"}
        return self.hub.status()
