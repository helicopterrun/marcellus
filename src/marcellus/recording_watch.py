"""Camera-stopped-recording watcher.

On 2026-10-30 five cameras silently lost recording for up to 18 hours.
gate-face was the worst kind: Frigate kept *detecting* (126 events) while its
record stream delivered no video, so `/api/stats` looked healthy
(camera_fps, process_fps fine). The one reliable signal is the age of each
camera's newest row in Frigate's `recordings` table, which is 5 to 15 s when
healthy (10 s segments, written in order, a few seconds of lag).

Layers, from pure to impure:

1. `evaluate()` -- no I/O. Turns "newest segment end per camera" into stalled /
   recovered transitions, with hysteresis (`stall_after_s` to stall,
   `recover_within_s` to recover; between the two nothing changes, so a camera
   hovering near a threshold cannot flap pages).
2. `watched_cameras()` -- which cameras are expected to record: enabled in
   Frigate's config with record on, *and* not switched off at runtime (Frigate's
   `/api/config` reports the live `enabled` / `record.enabled`, so a person who
   deliberately turned recording off is never paged).
3. `RecordingWatcher` -- the worker: one tick = read config, query the DB on a
   thread (per-camera `ORDER BY start_time DESC LIMIT 1`, under 1 ms on
   production), evaluate, publish the snapshot `/healthz` reads, and keep ONE
   system card per incident in step with it.

Frigate itself being down, or its DB unreadable, is `watchdog.py`'s and
`/healthz`'s job: a failing query is logged and retried next tick, and never
changes state or pages. If every watched camera stalls together it is still
reported -- once, as the same single card.

The card is `recording:system:stalled`, a system card (`ladder_policy.
SYSTEM_CARD_LEVEL`: an active push with sound, outside the ladder). One card
per incident across all cameras: more cameras stalling, or some recovering,
is an ENRICH (same level, new facts: silent in place); all recovered is a
RESOLVE. It keeps its Notification Center row (`kept_row`), is touched every
tick so `close_stale_cards` never closes a long incident, and survives a
sidecar restart: an open card is adopted, not re-sent.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import httpx

from marcellus import db
from marcellus.frigate_config import _record_section, load_frigate_config

if TYPE_CHECKING:
    from marcellus.config import RecordingWatchSection, Settings
    from marcellus.push.cards import Card

logger = logging.getLogger(__name__)

#: `{camera}:system:{reason}` -- the camera component is a pseudo-camera
#: (the card spans cameras); the app only reads it as "first component".
CARD_CAMERA = "recording"
CARD_REASON = "stalled"
CARD_KEY = f"{CARD_CAMERA}:system:{CARD_REASON}"

#: Delay before the first check after start-up: lets the lifespan finish
#: and Frigate's DB settle, but is short enough that /healthz fills in fast.
INITIAL_DELAY_S = 5.0

#: Cameras named in a card body before "and N more".
_NAMES_SHOWN = 3

STALLED = "stalled"
RECOVERED = "recovered"
#: The camera left the watched set while stalled (recording switched off in
#: Frigate, camera removed): not a recovery, but it ends the camera's stall.
WITHDRAWN = "withdrawn"


class Transition(NamedTuple):
    camera: str
    kind: str  # STALLED | RECOVERED | WITHDRAWN
    #: When recording actually stopped (the newest segment's end), or the time
    #: of detection for a camera with no rows at all.
    since: float
    at: float


@dataclass(frozen=True)
class WatchState:
    """What `evaluate` carries between ticks. Treat as immutable."""

    #: camera -> when recording stopped.
    stalled: Mapping[str, float] = field(default_factory=dict)
    #: Cameras seen in the previous evaluation.
    known: frozenset[str] = frozenset()
    #: camera -> not before this time may it stall (a camera that joined the
    #: watched set after start-up, e.g. recording just re-enabled, has an old
    #: newest segment and needs `stall_after_s` to produce a fresh one).
    grace_until: Mapping[str, float] = field(default_factory=dict)
    primed: bool = False


def evaluate(
    state: WatchState,
    newest_by_camera: Mapping[str, float | None],
    cameras: Iterable[str],
    now: float,
    cfg: RecordingWatchSection,
) -> tuple[WatchState, list[Transition]]:
    """Pure core. `newest_by_camera[cam]` is the end time of the camera's
    newest recording segment (None / absent: no rows). `cameras` is the
    watched set; everything else is ignored and forgotten."""
    watched = sorted(set(cameras))
    stalled: dict[str, float] = {}
    grace = {c: t for c, t in state.grace_until.items() if c in watched and t > now}
    transitions: list[Transition] = []

    for cam in watched:
        newest = newest_by_camera.get(cam)
        age = float("inf") if newest is None else max(0.0, now - newest)
        if cam in state.stalled:
            if newest is not None and age <= cfg.recover_within_s:
                transitions.append(Transition(cam, RECOVERED, state.stalled[cam], now))
            else:
                stalled[cam] = state.stalled[cam]
            continue
        if state.primed and cam not in state.known:
            grace.setdefault(cam, now + cfg.stall_after_s)
        if age > cfg.stall_after_s and cam not in grace:
            since = now if newest is None else newest
            stalled[cam] = since
            transitions.append(Transition(cam, STALLED, since, now))

    for cam, since in state.stalled.items():
        if cam not in watched:
            transitions.append(Transition(cam, WITHDRAWN, since, now))

    return (
        WatchState(
            stalled=stalled, known=frozenset(watched), grace_until=grace, primed=True
        ),
        transitions,
    )


def records_continuously(record: Mapping[str, Any]) -> bool:
    """Whether segments are kept whatever is happening. A motion-only setup has
    long, normal gaps, so segment age says nothing about it."""
    continuous = record.get("continuous")
    if isinstance(continuous, Mapping) and (continuous.get("days") or 0) > 0:
        return True
    retain = record.get("retain")  # the pre-0.17 schema; `mode` defaults to all
    return (
        isinstance(retain, Mapping)
        and (retain.get("days") or 0) > 0
        and retain.get("mode", "all") == "all"
    )


def watched_cameras(
    frigate_cfg: Mapping[str, Any],
    runtime: Mapping[str, tuple[bool, bool]] | None,
) -> list[str]:
    """Cameras expected to be recording right now.

    From Frigate's config file: `enabled` (default true) and the merged
    `record.enabled` (global then per camera; Frigate's own default is off),
    recording continuously (`records_continuously`).
    `runtime` is Frigate's live state, `{camera: (enabled, record_enabled)}`
    from `/api/config`; it wins for any camera it names, because that is where
    a person's deliberate runtime toggle shows up. With neither source, there
    is nothing to watch.
    """
    from_file: dict[str, bool] = {}
    cams = frigate_cfg.get("cameras")
    if isinstance(cams, dict):
        for name, body in cams.items():
            body = body if isinstance(body, dict) else {}
            record = _record_section(dict(frigate_cfg), name)
            from_file[str(name)] = (
                body.get("enabled", True) is not False
                and bool(record.get("enabled", False))
                and records_continuously(record)
            )
    out: list[str] = []
    for name in sorted(set(from_file) | set(runtime or {})):
        if runtime is not None and name in runtime:
            ok = runtime[name][0] and runtime[name][1]
        else:
            ok = from_file.get(name, False)
        if ok:
            out.append(name)
    return out


# --- card copy ---------------------------------------------------------------


def _clock(ts: float, now: float, zone: ZoneInfo | None) -> str:
    """A wall-clock time in `zone`, with the date when it isn't today. Without a
    zone, how long ago: the host's clock is often UTC, which is not the reader's."""
    if zone is None:
        return f"{_duration(now - ts)} ago"
    t = datetime.datetime.fromtimestamp(ts, zone)
    stamp = t.strftime("%I:%M %p").lstrip("0")
    if t.date() != datetime.datetime.fromtimestamp(now, zone).date():
        return f"{t.strftime('%b')} {t.day}, {stamp}"
    return stamp


def _duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "under two minutes" if seconds < 60 else "a minute"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{minutes} minutes"
    hours, rem = divmod(minutes, 60)
    if hours >= 48:
        return f"{hours // 24} days"
    return f"{hours} h {rem} min" if rem else f"{hours} h"


def _names(cameras: list[str]) -> str:
    shown = cameras[:_NAMES_SHOWN]
    rest = len(cameras) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {rest} more" if rest > 0 else text


def stalled_copy(
    stalled: Mapping[str, float], watched_count: int, now: float, zone: ZoneInfo | None = None
) -> tuple[str, str]:
    """(title, body) for the card while `stalled` is non-empty."""
    ordered = sorted(stalled.items(), key=lambda kv: (kv[1], kv[0]))
    cams = [c for c, _ in ordered]
    first = ordered[0][1]
    if len(cams) == 1:
        title = f"{cams[0]} isn't recording"
        since = _clock(first, now, zone)
        body = f"No video saved since {since}." if zone else f"Last video saved {since}."
    else:
        title = "Cameras not recording"
        body = f"{_names(cams)} stopped saving video, starting {_clock(first, now, zone)}."
        if len(cams) == watched_count:
            body += " Every camera is affected, so check Frigate and the network."
    return title, body


def resolved_copy(cameras: list[str], lasted_s: float) -> tuple[str, str]:
    """(title, body) for the final update when no camera is stalled any more."""
    if not cameras:
        # No camera was seen recovering (the incident predates a restart, or
        # the cameras left the watched set because recording was switched off).
        return (
            "Recording resumed",
            f"No camera is stalled any more. It lasted {_duration(lasted_s)}.",
        )
    verb = "is" if len(cameras) == 1 else "are"
    return (
        "Recording resumed",
        f"{_names(cameras)} {verb} recording again after {_duration(lasted_s)}.",
    )


# --- the worker --------------------------------------------------------------


def _query_newest(db_path: Path, cameras: list[str]) -> dict[str, float | None]:
    """Per-camera newest segment end, from a read-only Frigate connection."""
    conn = db.open_frigate_ro(db_path)
    try:
        return {cam: db.newest_recording_end(conn, cam) for cam in cameras}
    finally:
        conn.close()


class RecordingWatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        engine_getter: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
        newest_fn: Callable[[Path, list[str]], dict[str, float | None]] = _query_newest,
        runtime_fn: Callable[[], Awaitable[dict[str, tuple[bool, bool]] | None]] | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = settings.recording_watch
        self._engine_getter = engine_getter or (lambda: None)
        self._clock = clock
        self._newest_fn = newest_fn
        self._runtime_fn = runtime_fn or self._fetch_runtime
        self.state = WatchState()
        self.last_check: float | None = None
        self.last_error: str | None = None
        self.watched: list[str] = []
        #: Cameras the open card currently names; None = no card sent by this
        #: process (or adopted from before a restart, text unknown).
        self._announced: frozenset[str] | None = None
        self._incident_cameras: set[str] = set()
        self._incident_recovered: set[str] = set()

    # -- health --------------------------------------------------------------

    def health(self, now: float | None = None) -> dict[str, Any]:
        """The `/healthz` `checks["recording"]` value (informational only)."""
        now = self._clock() if now is None else now
        body: dict[str, Any] = {
            "status": STALLED if self.state.stalled else "ok",
            "stalled": [
                {"camera": c, "since": round(s, 1)}
                for c, s in sorted(self.state.stalled.items(), key=lambda kv: (kv[1], kv[0]))
            ],
            "last_check_age_s": (
                None if self.last_check is None else round(max(0.0, now - self.last_check), 1)
            ),
        }
        if self.last_error:
            body["error"] = self.last_error
        return body

    # -- inputs --------------------------------------------------------------

    async def _fetch_runtime(self) -> dict[str, tuple[bool, bool]] | None:
        """Frigate's live `{camera: (enabled, record.enabled)}`, or None when it
        can't be read (then the config file alone decides)."""
        url = self.settings.frigate.base_url.rstrip("/") + "/api/config"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                cams = resp.json().get("cameras")
        except Exception as exc:  # noqa: BLE001 -- runtime state is best-effort
            logger.debug("recording_watch: runtime config unavailable: %s", exc)
            return None
        if not isinstance(cams, dict):
            return None
        out: dict[str, tuple[bool, bool]] = {}
        for name, body in cams.items():
            if not isinstance(body, dict):
                continue
            record = body.get("record")
            out[str(name)] = (
                body.get("enabled", True) is not False,
                isinstance(record, dict)
                and bool(record.get("enabled", False))
                and records_continuously(record),
            )
        return out

    async def _watched(self) -> list[str]:
        file_cfg = await asyncio.to_thread(
            load_frigate_config, self.settings.frigate.config_path
        )
        runtime = await self._runtime_fn()
        return watched_cameras(file_cfg, runtime)

    # -- one tick ------------------------------------------------------------

    async def tick(self) -> list[Transition]:
        """One check. Never raises; a failed check changes nothing."""
        now = self._clock()
        try:
            cameras = await self._watched()
            newest = await asyncio.to_thread(
                self._newest_fn, self.settings.frigate.db_path, cameras
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- retry next tick
            text = f"{type(exc).__name__}: {exc}"
            if text != self.last_error:
                logger.warning("recording_watch: check failed (will retry): %s", text)
            self.last_error = text
            return []
        self.last_error = None
        self.last_check = now
        self.watched = cameras
        self.state, transitions = evaluate(self.state, newest, cameras, now, self.cfg)
        for tr in transitions:
            if tr.kind == STALLED:
                logger.warning(
                    "recording_watch: %s stalled (newest segment ended %s)",
                    tr.camera, _clock(tr.since, now, None),
                )
            else:
                logger.info("recording_watch: %s %s", tr.camera, tr.kind)
        self._incident_recovered.update(t.camera for t in transitions if t.kind == RECOVERED)
        try:
            await self._reconcile_card(now)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recording_watch: card update failed (will retry)")
        return transitions

    # -- the card ------------------------------------------------------------

    def _push_ready(self) -> Any | None:
        """The push engine when this watcher may send a card, else None."""
        push = self.settings.push
        if not (self.cfg.notify and push.enabled and push.delivery_enabled):
            return None
        return self._engine_getter()

    async def _reconcile_card(self, now: float) -> None:
        engine = self._push_ready()
        if engine is None:
            return
        from marcellus.push import card_store
        from marcellus.push import store as push_store

        stalled = dict(self.state.stalled)
        conn = db.open_sidecar(engine.db_path)
        try:
            existing = card_store.get_card(conn, CARD_KEY)
            is_open = existing is not None and not existing.closed and not existing.resolved
            if stalled:
                self._incident_cameras.update(stalled)
                current = frozenset(stalled)
                if not is_open:
                    await self._send(conn, engine, push_store, None, stalled, now)
                    self._announced = current
                elif self._announced is None:
                    # An incident already open before this process: adopt it
                    # rather than sound again. Later changes update it.
                    self._announced = current
                    card_store.touch_card(conn, CARD_KEY, now=now)
                elif current != self._announced:
                    await self._send(conn, engine, push_store, existing, stalled, now)
                    self._announced = current
                else:
                    card_store.touch_card(conn, CARD_KEY, now=now)
            else:
                if is_open and existing is not None:
                    await self._send_resolve(conn, engine, push_store, existing, now)
                self._announced = None
                self._incident_cameras = set()
                self._incident_recovered = set()
        finally:
            conn.close()

    def _sound_allowed(self, zone: ZoneInfo | None, now: float) -> bool:
        """No sound inside quiet hours, or with sounds muted."""
        from marcellus.push import policy_settings

        policy = policy_settings.get_active()
        if policy.get("mute_sounds"):
            return False
        active, _mode = policy_settings.quiet_hours_at(policy, now, zone)
        return not active

    @staticmethod
    def _devices(conn: Any, push_store: Any) -> list[Any]:
        # A system card is about the system, not a camera or a label: a
        # device's camera / label filters must not silence it.
        return [replace(d, cameras=(), labels=()) for d in push_store.list_devices(conn)]

    def _normalized(self, existing: Card | None) -> Card | None:
        """The stored card with its level restored to the system level (a
        persisted `notify` reads back as `silent`, which would change device
        eligibility and mutation classification)."""
        from marcellus.push.ladder_policy import SYSTEM_CARD_LEVEL

        if existing is None:
            return None
        return replace(existing, level=SYSTEM_CARD_LEVEL, peak_level=SYSTEM_CARD_LEVEL)

    async def _send(
        self, conn: Any, engine: Any, push_store: Any, existing: Card | None,
        stalled: Mapping[str, float], now: float,
    ) -> None:
        from marcellus.push import policy_settings
        from marcellus.push.cards import CREATE
        from marcellus.push.delivery import advance_card, build_card_payload, send_card_mutation
        from marcellus.push.ladder_policy import SYSTEM_CARD_LEVEL

        card, mutation, sound = advance_card(
            self._normalized(existing), SYSTEM_CARD_LEVEL, card_key=CARD_KEY, now=now
        )
        if mutation == CREATE:
            # A new incident: its clock is when recording stopped, and it is a
            # real, kept row (the system level isn't a pushable ladder level,
            # so `send_card_mutation` would not mark it on its own).
            card = replace(card, state_since_at=min(stalled.values()), kept_row=True)
        else:
            card = replace(card, kept_row=True)
        devices = self._devices(conn, push_store)
        zone = policy_settings.household_zone(devices)
        title, body = stalled_copy(stalled, len(self.watched), now, zone)
        payload = build_card_payload(
            card, mutation, sound=sound and self._sound_allowed(zone, now),
            subject_kind="", place_class="", label="", camera=CARD_CAMERA, zone_name="",
            glyph="video.slash", primary=title, secondary=body, event_ts=now,
        )
        await send_card_mutation(
            conn, engine.transport, devices, card, mutation, payload,
            camera=CARD_CAMERA, now=now,
        )

    async def _send_resolve(
        self, conn: Any, engine: Any, push_store: Any, existing: Card, now: float
    ) -> None:
        from marcellus.push.delivery import advance_card, build_card_payload, send_card_mutation
        from marcellus.push.ladder_policy import SYSTEM_CARD_LEVEL

        card, mutation, _sound = advance_card(
            self._normalized(existing), SYSTEM_CARD_LEVEL, card_key=CARD_KEY, now=now,
            resolved=True,
        )
        card = replace(card, kept_row=True)
        title, body = resolved_copy(
            sorted(self._incident_recovered), now - existing.state_since_at
        )
        payload = build_card_payload(
            card, mutation, sound=False,
            subject_kind="", place_class="", label="", camera=CARD_CAMERA, zone_name="",
            glyph="video", primary=title, secondary=body, event_ts=now,
        )
        await send_card_mutation(
            conn, engine.transport, self._devices(conn, push_store), card, mutation, payload,
            camera=CARD_CAMERA, now=now,
        )


async def recording_watch_loop(app: Any) -> None:
    """Run the watcher until cancelled (started by the app lifespan)."""
    watcher: RecordingWatcher = app.state.recording_watcher
    await asyncio.sleep(INITIAL_DELAY_S)
    while True:
        try:
            await watcher.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recording_watch: tick failed")
        await asyncio.sleep(watcher.cfg.interval_s)
