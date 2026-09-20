"""Ties the decision engine, situation evaluation, handle store, and transport
together.

`PushEngine.handle_review_message` is the single entry point the MQTT
subscriber (and the offline-recovery backfill) calls per review message; it
is plain `async def` and takes its dependencies explicitly so it's testable
without a running app or a real MQTT connection.

The situations push pipeline (v1 camera+label dispatch and situation-only
evaluation) was retired in Phase 5 §1: the card/attention-ladder pipeline in
`delivery_wire.py` is the only alert path now. Device registrations carrying
`situations` are still accepted and stored (older app builds keep working),
and the situation *test* endpoint (`POST /v1/push/test/{situation_id}`,
`send_situation_test` below) still fires the legacy wire shape on demand,
but no review or object message ever emits a situation push.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from marcellus.config import PushSection
from marcellus.push import activity as activity_payload
from marcellus.push import card_store, delivery_wire, store
from marcellus.push.decision import (
    parse_object_message,
    parse_review_message,
)
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.payload import build_payload
from marcellus.push.situations import (
    STAGE_ENDING,
    Match,
    Situation,
    TrackStore,
)
from marcellus.push.stats import STATS
from marcellus.push.thumbnails import fetch_thumbnail
from marcellus.push.transport import PushTransport, TransportResult

logger = logging.getLogger(__name__)


@dataclass
class PushEngine:
    db_path: str
    transport: PushTransport
    server_id: str
    #: Situation handles hold a pre-warmed thumbnail and outlive the v1 ones:
    #: plan §8 keeps them for 24h so a notification the user comes back to
    #: hours later can still redeem its image.
    situation_handle_ttl_s: float = 86400.0
    #: Where the pre-warm fetches from. Empty disables thumbnail warm-up
    #: entirely -- pushes still fire, image-less.
    frigate_base_url: str = ""
    #: GC keeps send records for twice this window (see `_maybe_gc`);
    #: `delivery_wire`'s own budgets do the actual rate limiting now.
    rate_limit_window_s: float = 3600.0
    thumbnail_max_edge: int = 320
    thumbnail_quality: int = 60
    thumbnail_timeout_s: float = 5.0
    gc_interval_s: float = 300.0

    #: Per-`(camera, track_id)` dwell state -- the only way to derive loiter
    #: from a topic that doesn't carry it. Wiped on every MQTT reconnect.
    tracks: TrackStore = field(default_factory=TrackStore)
    #: Where dwell comes from. "events" reads live occupancy off
    #: `frigate/events`; "reviews" is the handoff's literal prescription --
    #: dwell advanced only by `frigate/reviews` `type: update` messages --
    #: kept switchable, but see `push.situations` for why it does not fire on
    #: a real deployment.
    dwell_source: str = "events"

    # -- Phase 2: Live Activities --
    #: Quiet period after which a Present situation is considered resolved.
    activity_resolution_s: float = 30.0
    #: An open card idle this long is closed silently -- the resolve that
    #: never arrived, e.g. a dropped Frigate end or a failed write; sized so
    #: a real loiter's updates keep it alive.
    card_resolution_s: float = 600.0
    #: How long the activity lingers on screen after the end push.
    activity_dismissal_tail_s: float = 30.0
    #: How long an ended activity's row survives before reaping, measured past
    #: its dismissal so a late token upload still finds something.
    activity_reap_after_s: float = 300.0
    #: How long a *closed* card row survives before reaping. Long on purpose:
    #: closed cards are only read for late lookups and debugging, but the
    #: table otherwise grows forever (one row per event ever routed).
    card_reap_after_s: float = 30 * 86400.0

    # -- Attention ladder: delivery pipeline (Elsinore Phase 2) --
    # The whole `PushSection`, not individual fields: `delivery_wire`'s
    # functions take the config object directly (`delivery_enabled`,
    # `delivery_zone_place_map`, ...), and threading each field through the
    # engine's constructor separately would just be another way to get out
    # of sync with `config.py`. `None` (the default, e.g. in tests that build
    # a `PushEngine` directly) behaves exactly like `delivery_enabled=False`.
    push_config: PushSection | None = None

    #: Encounters live hook (encounters/service.py's `EncounterService.
    #: observe_review`, wired in server.py's lifespan). Called first, inside
    #: its own try/except, on every review message -- including "end" --
    #: before this method's own `msg_type == "end"` short-circuit runs, so a
    #: review's finalization still reaches encounters even though push itself
    #: has never done anything with it. An encounters failure must never
    #: affect push, hence the isolated try/except rather than just letting it
    #: propagate.
    on_review: Callable[[ReviewEvent], None] | None = None

    _http: httpx.AsyncClient | None = None
    _last_gc: float = 0.0
    #: Latest `sub_label` seen per `(camera, track_id)` off `frigate/events`.
    #: Feeds the `sub_label_unknown` escalation trigger; Phase 5 owns the
    #: allow/deny lists that will use the same input.
    _sub_labels: dict[tuple[str, str], str] = field(default_factory=dict)
    # Last-seen current_zones per track — the zone-transition hook's change
    # detector (see handle_object_payload).
    _last_zones: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    #: Serializes the delivery pipeline across concurrent per-frame
    #: coroutines. Every `frigate/events`/`frigate/reviews` message runs as
    #: its own coroutine with its own SQLite connection; without this lock,
    #: N handlers race into `open_activity` at once, each seeing
    #: `device_row is None` before the first one's `open_activity` commits,
    #: and each holds its write across `await transport.*` -- the next
    #: handler's write then blocks on the busy_timeout ("database is
    #: locked") instead of erroring immediately. Result (prod journal
    #: 2026-09-02): ~25 push-to-start sends for one story instead of one,
    #: and the card's own resolve dying in the pile-up, leaking it open.
    #: NOT held inside `_end_activity` -- it's called from within
    #: `sweep_activities` (only across that method's read and close phases;
    #: see `_ending` below) and from `delivery_wire` with `engine=self`, so
    #: acquiring there would deadlock. `_end_activity`'s own `await
    #: transport.*` -- the actual network send -- always runs unlocked,
    #: whether called from the sweep or from `delivery_wire`.
    _pipeline_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    #: Device apns_tokens whose Live Activity `sweep_activities` is currently
    #: ending (between releasing the lock after its read phase and
    #: reacquiring it after `_end_activity`'s network send completes). No DB
    #: column marks this -- `push_activities` has no "ending" stage that
    #: means what this needs -- so it's process-local: `handle_event`/
    #: `handle_object_payload` filter these tokens out of the device list
    #: before calling into `delivery_wire`, so a frame arriving mid-sweep
    #: neither re-touches the row `_end_activity` is about to close nor
    #: starts a second Live Activity for a device whose old one hasn't
    #: actually closed yet (`store.find_activity` still returns the
    #: not-yet-closed row for the whole duration of `_end_activity`'s send).
    _ending: set[str] = field(default_factory=set)

    def _conn(self) -> sqlite3.Connection:
        from marcellus import db

        return db.open_sidecar(self.db_path)

    def _client(self) -> httpx.AsyncClient:
        """One pooled client for snapshot pre-warm, kept for the process's
        life so a match doesn't pay connection setup to Frigate on the
        interrupt path."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=self.thumbnail_timeout_s,
                limits=httpx.Limits(
                    max_connections=20, max_keepalive_connections=10, keepalive_expiry=15.0
                ),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    def reset_tracks(self) -> None:
        """Wipe per-track state -- called on every MQTT reconnect, since a
        Frigate restart reissues track ids and stale entries would then
        describe a different object entirely (handoff item 8)."""
        if len(self.tracks):
            logger.info("push: clearing %d track(s) after mqtt reconnect", len(self.tracks))
        self.tracks.clear()
        self._sub_labels.clear()
        self._last_zones.clear()

    def _live_devices(self, devices: list[Device]) -> list[Device]:
        """`devices`, minus any whose Live Activity `sweep_activities` is
        currently ending (see `_ending`'s docstring on `_pipeline_lock`).
        Excludes the device from card delivery too, not just the LA side --
        `delivery_wire`'s card and LA logic share one device list per call
        and neither module gets logic changes here, so the only lever
        available at this layer is which devices reach it at all. The window
        is one `_end_activity` network send, so the gap is brief."""
        if not self._ending:
            return devices
        return [d for d in devices if d.apns_token not in self._ending]

    async def handle_object_payload(self, payload: dict[str, Any]) -> int:
        """Process one `frigate/events` message — track observation and
        delivery resolution only (Phase 5: situations pipeline retired)."""
        if self.dwell_source != "events":
            return 0
        obj = parse_object_message(payload)
        if obj is None:
            return 0
        key = (obj.camera, obj.track_id)

        if obj.msg_type == "end":
            if self.push_config is not None and self.push_config.delivery_enabled:
                # See `_pipeline_lock`: concurrent per-frame handlers each
                # hold their own SQLite conn across `await transport.*`
                # here, which without serialization convoys into
                # `database is locked` and duplicate push-to-starts.
                async with self._pipeline_lock:
                    conn = self._conn()
                    try:
                        devices = self._live_devices(store.list_devices(conn))
                        await delivery_wire.handle_delivery_resolve(
                            obj.camera, obj.track_id, conn=conn, devices=devices,
                            transport=self.transport, config=self.push_config,
                        )
                    finally:
                        conn.close()
            self.tracks.forget(obj.camera, obj.track_id)
            self._sub_labels.pop(key, None)
            self._last_zones.pop(key, None)
            return 0

        now = time.time()
        self.tracks.observe_object(
            obj.camera, obj.track_id, obj.current_zones, now=now,
            path_data=obj.path_data, velocity_angle=obj.velocity_angle,
            average_estimated_speed=obj.average_estimated_speed,
            stationary=obj.stationary, label=obj.label,
        )
        # Zone-transition escalation (delivery_wire.handle_zone_transition):
        # reviews go quiet on stationary objects, so a loiter drifting into
        # hotter ground re-routes from the event stream instead. Fires only
        # when the zone set actually changes; the handler no-ops unless the
        # new ground routes above the card's current level.
        zones_now = tuple(obj.current_zones or ())
        if zones_now and zones_now != self._last_zones.get(key):
            self._last_zones[key] = zones_now
            if self.push_config is not None and self.push_config.delivery_enabled:
                async with self._pipeline_lock:
                    conn = self._conn()
                    try:
                        devices = self._live_devices(store.list_devices(conn))
                        await delivery_wire.handle_zone_transition(
                            obj.camera, obj.track_id, zones_now, label=obj.label,
                            conn=conn, devices=devices, transport=self.transport,
                            config=self.push_config, engine=self, now=now,
                        )
                    finally:
                        conn.close()
        if obj.sub_label:
            old_sub = self._sub_labels.get(key)
            self._sub_labels[key] = obj.sub_label
            if (
                obj.sub_label != old_sub
                and self.push_config is not None
                and self.push_config.delivery_enabled
            ):
                async with self._pipeline_lock:
                    conn = self._conn()
                    try:
                        devices = self._live_devices(store.list_devices(conn))
                        await delivery_wire.handle_recognition_event(
                            obj.camera, obj.track_id, obj.sub_label,
                            conn=conn, devices=devices, transport=self.transport,
                            config=self.push_config, label=obj.label, now=now,
                        )
                    finally:
                        conn.close()
        self._maybe_gc(now)
        return 0

    async def handle_review_payload(self, payload: dict[str, Any]) -> int:
        """Parse + dispatch one `frigate/reviews` message. Returns the number
        of devices notified (0 if nothing matched or the message wasn't
        actionable)."""
        event = parse_review_message(payload)
        if event is None:
            return 0
        return await self.handle_event(event)

    async def handle_event(self, event: ReviewEvent) -> int:
        if self.on_review is not None:
            try:
                self.on_review(event)
            except Exception:
                logger.exception("push: on_review hook failed for review %s", event.review_id)
        if event.msg_type == "end":
            # Review finalization -- never itself a push trigger (unchanged
            # from when parse_review_message dropped "end" outright; it's
            # parsed through now only so on_review above gets to see it).
            return 0

        now = time.time()
        conn = self._conn()
        try:
            devices = self._live_devices(store.list_devices(conn))
        finally:
            conn.close()

        sent = 0
        # Phase 5 §1: the card pipeline is now the only alert path for all
        # devices. The situations pipeline is retired (its dispatch code is
        # deleted) — device registrations with `situations` are still
        # accepted and stored (older app builds keep working), but no
        # situation alert or situation LA push is emitted.
        if self.push_config is not None and self.push_config.delivery_enabled:
            async with self._pipeline_lock:
                conn = self._conn()
                try:
                    sent = await delivery_wire.handle_delivery_event(
                        event, conn=conn, devices=devices, transport=self.transport,
                        config=self.push_config, engine=self, now=now,
                    )
                finally:
                    conn.close()

        self._maybe_gc(now)
        return sent

    async def _end_activity(
        self,
        device: Device,
        situation: Situation,
        camera: str,
        track_id: str,
        *,
        reason: str,
        now: float,
        row: Any = None,
    ) -> int:
        """Resolution: end the activity with a tail so it fades rather than
        vanishing."""
        if row is None:
            conn = self._conn()
            try:
                row = store.find_activity(conn, apns_token=device.apns_token)
            finally:
                conn.close()
        self.tracks.set_stage(camera, track_id, device.apns_token, situation.id, STAGE_ENDING)
        if row is None:
            return 0

        # An early-fire activity that never earned its alert promotion gets a
        # shorter tail: it was a guess, and it should leave quickly once the
        # guess doesn't pan out (handoff item 8).
        unpromoted = bool(row["from_detection"]) and not bool(row["promoted"])
        tail = (
            activity_payload.UNPROMOTED_DISMISSAL_TAIL_S if unpromoted
            else self.activity_dismissal_tail_s
        )
        # A story that ended while its subject was walking AWAY reads as
        # already-over — let the LA leave the lock screen faster.
        from marcellus.push import delivery_wire as _dw
        if _dw.last_heading(camera, track_id) == "leaving":
            tail = min(tail, 10.0)

        sent = 0
        if row["token"]:
            match = Match(
                situation=situation, track_id=track_id,
                dwell_s=float(row["dwell_seconds"] or 0), label="", zone="",
            )
            payload = activity_payload.build_end(
                match, thumbnail_revision=int(row["thumbnail_revision"]),
                tail_s=tail, now=now,
            )
            # Rows recreated by the app's token re-sync carry no collapse_id,
            # and the relay rejects an empty one (422) — which stranded the
            # phone-side LA alive, provoking another token re-sync and an
            # infinite end-retry loop (observed 2026-08-14). The situation id
            # (card_key in the card era) is the collapse id everywhere else.
            result = await self.transport.send_live_activity(
                device, token=row["token"], payload=payload,
                collapse_id=row["collapse_id"] or situation.id, event="end",
            )
            if result.ok:
                sent = 1
            else:
                logger.warning(
                    "push: LA end failed for activity %s: %s",
                    row["activity_id"], result.error,
                )

        conn = self._conn()
        try:
            store.close_activity(conn, row["activity_id"], now=now)
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "push: LA end situation=%s track=%s activity=%s reason=%s tail=%.0fs%s",
            situation.id, track_id, row["activity_id"], reason, tail,
            " (unpromoted early-fire)" if unpromoted else "",
        )
        return sent

    async def sweep_activities(self, *, now: float | None = None) -> int:
        """End activities whose situation has gone quiet (handoff item 7).

        Resolution is the one transition no incoming message announces --
        nothing arrives to say "the object stopped being reported" -- so it
        needs a sweep. Deliberately *not* a keep-alive: this only ever ends
        activities, never refreshes them, so the non-negotiable that stage
        transitions are the only thing minting an LA push still holds.

        Only the read phase (this method) and the final bookkeeping phase
        hold `_pipeline_lock`; the `_end_activity` sends themselves (each one
        a network round trip) run outside it, so a concurrent per-frame
        handler for an unrelated camera/device is never blocked behind a
        sweep send -- see `_pipeline_lock`'s docstring. Each ending device's
        apns_token sits in `self._ending` for the same window, so a frame for
        that *same* device is excluded from delivery in the meantime (see
        `_live_devices`) rather than racing `_end_activity`'s close.
        """
        now = time.time() if now is None else now
        to_end: list[tuple[Device, Situation, sqlite3.Row]] = []
        async with self._pipeline_lock:
            conn = self._conn()
            try:
                stale = store.stale_activities(
                    conn, quiet_for=self.activity_resolution_s, now=now
                )
                devices = {d.apns_token: d for d in store.list_devices(conn)}
            finally:
                conn.close()

            conn = self._conn()
            try:
                n_stale_cards = card_store.close_stale_cards(
                    conn, idle_for=self.card_resolution_s, now=now
                )
            finally:
                conn.close()
            if n_stale_cards:
                logger.info(
                    "push: closed %d stale card(s) idle > %.0fs",
                    n_stale_cards, self.card_resolution_s,
                )

            for row in stale:
                device = devices.get(row["apns_token"])
                if device is None:
                    conn = self._conn()
                    try:
                        store.delete_activity(conn, row["activity_id"])
                        conn.commit()
                    finally:
                        conn.close()
                    continue
                situation = next(
                    (s for s in device.situations if s.id == row["situation_id"]), None
                )
                if situation is None:
                    situation = Situation(id=row["situation_id"], name=row["situation_id"])
                self._ending.add(device.apns_token)
                to_end.append((device, situation, row))

        # Outside the lock: the network sends. `_end_activity` closes each
        # row itself (its own connection, its own commit) once its send
        # attempt -- successful or not -- is done, matching the behaviour at
        # the delivery_wire end site (a failed send still closes the row).
        sent = 0
        try:
            for device, situation, row in to_end:
                try:
                    sent += await self._end_activity(
                        device, situation, row["camera"], row["track_id"],
                        reason="quiet", now=now, row=row,
                    )
                    STATS.incr("pipeline.sweep.ended")
                except Exception:
                    logger.exception(
                        "push: sweep failed to end activity %s", row["activity_id"]
                    )
        finally:
            # A cancelled sweep (shutdown, or the loop being torn down) must
            # not leave tokens parked in `_ending` -- that would silently
            # exclude those devices from every later delivery. Plain set
            # discard, no lock needed: `_ending` is only ever read/written
            # from the event loop.
            for device, _situation, _row in to_end:
                self._ending.discard(device.apns_token)
        return sent

    async def prewarm_thumbnail(self, handle: str, *, camera: str, event_id: str) -> bool:
        """Fetch, shrink, and park the snapshot under `handle` (plan §4 lever 1).

        Returns whether an image landed. False is a normal outcome, not an
        error: Frigate may be down, the event may be too fresh to have a
        snapshot, the fetch may time out. The push has already gone out.
        """
        if not self.frigate_base_url:
            return False
        jpeg = await fetch_thumbnail(
            self._client(),
            frigate_base_url=self.frigate_base_url,
            camera=camera,
            event_id=event_id,
            max_edge=self.thumbnail_max_edge,
            quality=self.thumbnail_quality,
            timeout=self.thumbnail_timeout_s,
        )
        if not jpeg:
            logger.info(
                "push: no thumbnail for handle %s (camera=%s event=%s) -- the push still "
                "fires, it just lands without an image",
                handle, camera, event_id,
            )
            return False
        conn = self._conn()
        try:
            store.store_thumbnail(conn, handle, jpeg)
            conn.commit()
        finally:
            conn.close()
        return True

    # -- shared bookkeeping --------------------------------------------------

    def _prune(self, tokens: list[str]) -> None:
        """Drop permanently-dead device rows (410/400, spec §5)."""
        conn = self._conn()
        try:
            for token in tokens:
                store.delete_device(conn, token)
            conn.commit()
        finally:
            conn.close()

    def _maybe_gc(self, now: float) -> None:
        """Periodic housekeeping, on the same thread that just did the work.

        Everything here is a cheap indexed DELETE and one dict sweep; running
        it inline every few minutes avoids a background task whose only job
        would be to wake up and find nothing to do.
        """
        if now - self._last_gc < self.gc_interval_s:
            return
        self._last_gc = now
        reaped = self.tracks.reap(now=now)
        for key in [k for k in self._sub_labels if k not in self.tracks]:
            del self._sub_labels[key]
        for key in [k for k in self._last_zones if k not in self.tracks]:
            del self._last_zones[key]
        conn = self._conn()
        try:
            handles = store.prune_expired_handles(conn, now=now)
            snoozes = store.prune_expired_snoozes(conn, now=now)
            # Kept a window past the rate limiter's own so a restart mid-window
            # can still see the sends that came before it.
            sends = store.prune_old_sends(conn, older_than=self.rate_limit_window_s * 2, now=now)
            # Ended activities outlive their dismissal window by a margin, so a
            # token upload that arrives just after the end still lands on a row
            # rather than resurrecting one.
            activities = store.reap_activities(
                conn, older_than=self.activity_reap_after_s, now=now
            )
            cards = card_store.reap_cards(conn, older_than=self.card_reap_after_s, now=now)
            conn.commit()
        finally:
            conn.close()
        if handles or reaped or snoozes or sends or activities or cards:
            logger.debug(
                "push: gc dropped %d handle(s), %d track(s), %d snooze(s), %d send record(s), "
                "%d activity row(s), %d card row(s)",
                handles, reaped, snoozes, sends, activities, cards,
            )

    # -- test pushes ---------------------------------------------------------

    async def send_test(self, device: Device) -> TransportResult:
        """One test push to `device`, bypassing its subscription filters.

        Filters are deliberately not consulted: this verifies the APNs pipe, not
        the subscription (spec §1), so a device subscribed to one camera still
        gets its own test. Environment routing is *not* bypassed.

        The 410/400 feedback cleanup is the same one a real send applies -- a
        dead token discovered by pressing the test button is exactly as dead as
        one discovered by a real alert, and leaving the row behind would mean
        the next real alert rediscovers it.
        """
        result = await self.transport.send_test(device)
        if result.unregistered:
            logger.info(
                "push: pruning device %s after test send (%s)",
                device.device_id, result.error,
            )
            self._prune([device.apns_token])
        elif not result.ok:
            logger.warning(
                "push: test send failed for device %s: %s", device.device_id, result.error
            )
        return result

    async def send_situation_test(
        self, device: Device, situation: Situation, *, camera: str = ""
    ) -> TransportResult:
        """Fire one situation-shaped push at `device` through the real pipeline.

        Deliberately the *whole* path -- handle minting, thumbnail pre-warm,
        payload shape, collapse id, sound -- because what the app's Settings
        button is verifying is that a real situation push would arrive looking
        the way it should, not merely that APNs is reachable (spec §1's plain
        test push already answers that).

        Snooze and the rate limit are bypassed, and the send is not recorded
        against the hourly ceiling: pressing "test" is the user asking for
        this one, and it must not spend the budget a real alert might need
        ten minutes later.
        """
        camera = camera or (situation.cameras[0] if situation.cameras else "")
        camera = camera or (device.cameras[0] if device.cameras else "")
        track_id = f"test-{int(time.time())}"

        conn = self._conn()
        try:
            handle = store.mint_handle(
                conn,
                camera=camera,
                event_id="",
                review_id=f"test-{situation.id}",
                ttl_s=self.situation_handle_ttl_s,
                situation_id=situation.id,
                track_id=track_id,
            )
            conn.commit()
        finally:
            conn.close()

        match = Match(
            situation=situation,
            track_id=track_id,
            dwell_s=situation.loiter_seconds,
            label=situation.labels[0] if situation.labels else "person",
            zone=situation.zones[0] if situation.zones else "",
        )
        warm = asyncio.create_task(
            # No event id: a test has no tracked object, so the pre-warm falls
            # through to the camera's latest frame -- which is also the most
            # honest preview of what this situation would look like.
            self.prewarm_thumbnail(handle, camera=camera, event_id="")
        )
        try:
            payload = build_payload(match, handle=handle, server_id=self.server_id)
            result = await self.transport.send_situation(
                device, payload=payload, collapse_id=match.collapse_id
            )
        finally:
            with __import__("contextlib").suppress(Exception):
                await warm

        if result.unregistered:
            logger.info(
                "push: pruning device %s after situation test send (%s)",
                device.device_id, result.error,
            )
            self._prune([device.apns_token])
        elif not result.ok:
            logger.warning(
                "push: situation test send failed for device %s: %s",
                device.device_id, result.error,
            )
        return result
