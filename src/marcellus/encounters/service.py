"""`EncounterService`: the live MQTT hook, the reconciler ("belt"), and
`/healthz` status (docs/encounters.md "service.py").
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from marcellus import db
from marcellus.analysis.clock_offset import event_clock_offset_s
from marcellus.config import Settings
from marcellus.encounters import store
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import Atom, LinkerConfig, apply, decide, normalise_labels
from marcellus.push.models import ReviewEvent

logger = logging.getLogger(__name__)

#: Bound on the live-hook queue (see `EncounterService.observe_review`). Sized
#: generously above any plausible MQTT review burst -- the reconciler is the
#: safety net if it's ever exceeded, so dropping past this point is fine.
_QUEUE_MAXSIZE = 1000


@dataclass(frozen=True)
class ReconcileStats:
    rows: int
    new: int
    updated: int
    sealed: int


def _linker_config(settings: Settings) -> LinkerConfig:
    enc = settings.encounters
    return LinkerConfig(
        gap_s=dict(enc.gap_s),
        max_duration_s=enc.max_duration_s,
        recent_cameras=enc.recent_cameras,
        min_copresence_s=enc.min_copresence_s,
    )


def _review_data(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(x) for x in value)


class EncounterService:
    """Owns encounter linking: `observe_review` is the live per-message hook
    (wired as `PushEngine`'s `on_review`), `reconcile` is the periodic
    backfill/repair sweep run via `asyncio.to_thread`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        adjacency: Adjacency,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.adjacency = adjacency
        self._now = now
        self._cfg = _linker_config(settings)
        self._last_reconcile: ReconcileStats | None = None
        self._last_reconcile_at: float | None = None
        self._last_error: str | None = None
        # Live-hook decoupling (see `observe_review`/`run_worker`): sqlite
        # writes never happen on the caller's thread/loop, only inside the
        # worker (via `asyncio.to_thread`) or `reconcile`.
        self._queue: asyncio.Queue[ReviewEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._queue_drop_logged = False

    def _conn(self) -> sqlite3.Connection:
        return db.open_sidecar(self.settings.sidecar.db_path)

    def _pin_split(
        self, conn: sqlite3.Connection, atom_id: str
    ) -> tuple[str | None, frozenset[str]]:
        decisions = store.decisions_for(conn, atom_id)
        pinned_to = next((d["encounter_id"] for d in decisions if d["action"] == "pin"), None)
        split_from = frozenset(d["encounter_id"] for d in decisions if d["action"] == "split")
        return pinned_to, split_from

    def observe_review(self, ev: ReviewEvent) -> None:
        """LIVE hook off `PushEngine.handle_event` -- never raises, never
        blocks. Only enqueues; `run_worker` (or, in tests, `process_pending`)
        does the actual sqlite linking work off the event loop.

        `PushEngine.handle_event` calls this synchronously, unawaited, right
        on the asyncio event loop that also serves MQTT and push delivery --
        opening a sqlite connection here would risk a `busy_timeout` wait
        (up to 3s) stalling that whole loop whenever the reconciler happens
        to be mid-write against the same WAL file.
        """
        try:
            self._queue.put_nowait(ev)
        except asyncio.QueueFull:
            if not self._queue_drop_logged:
                logger.warning(
                    "encounters: live-hook queue full (%d) -- dropping review "
                    "events until it drains; the reconciler will catch up",
                    self._queue.maxsize,
                )
                self._queue_drop_logged = True
        except Exception:  # noqa: BLE001 -- an encounters failure must never affect push
            logger.exception("encounters: observe_review failed to enqueue review %s", ev.review_id)

    def _link_review(self, ev: ReviewEvent) -> None:
        """The actual (sync, sqlite-touching) linking work for one live
        review message -- run via `asyncio.to_thread` from `run_worker`/
        `process_pending`, never directly on the event loop."""
        try:
            now = self._now()
            if ev.msg_type == "end":
                # Prefer Frigate's own end_time; fall back to wall clock only
                # if Frigate somehow sent none.
                end_time = ev.end_time if ev.end_time is not None else now
            else:
                end_time = None
            labels, qualifiers = normalise_labels(ev.labels)
            sub_labels = tuple(ev.sub_labels) + tuple(
                q for q in qualifiers if q not in ev.sub_labels
            )
            atom = Atom(
                atom_id=ev.review_id,
                camera=ev.camera,
                start_time=ev.start_time,
                end_time=end_time,
                labels=labels,
                zones=ev.zones,
                event_ids=ev.track_ids,
                sub_labels=sub_labels,
                severity=ev.severity,
            )
            conn = self._conn()
            try:
                pinned_to, split_from = self._pin_split(conn, atom.atom_id)
                open_encounters = store.load_open(conn, now)
                decision = decide(
                    atom,
                    open_encounters,
                    self.adjacency,
                    self._cfg,
                    now=now,
                    pinned_to=pinned_to,
                    split_from=split_from,
                )
                store.upsert_atom(conn, atom, decision, now)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 -- an encounters failure must never affect push
            logger.exception("encounters: linking failed for review %s", ev.review_id)

    async def run_worker(self) -> None:
        """Consume `observe_review`'s queue forever, one review at a time (in
        arrival order), doing the sqlite work via `asyncio.to_thread` so the
        event loop is never blocked on it. Started as its own task from the
        server lifespan; cancelled on shutdown."""
        while True:
            ev = await self._queue.get()
            try:
                await asyncio.to_thread(self._link_review, ev)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("encounters: link worker failed for review %s", ev.review_id)

    async def process_pending(self) -> None:
        """Drain whatever's currently queued, synchronously from the caller's
        point of view (each item still runs via `asyncio.to_thread`). Test
        helper -- `run_worker` is what actually services the queue in the
        running app."""
        while not self._queue.empty():
            ev = self._queue.get_nowait()
            await asyncio.to_thread(self._link_review, ev)

    def _atom_from_review_row(self, row: sqlite3.Row) -> Atom:
        camera = str(row["camera"])
        offset_s = event_clock_offset_s(self.settings, camera)
        data = _review_data(row["data"])
        start_time = float(row["start_time"]) + offset_s
        end_time = float(row["end_time"]) + offset_s if row["end_time"] is not None else None
        raw_sub_labels = _strings(data.get("sub_labels"))
        labels, qualifiers = normalise_labels(_strings(data.get("objects")))
        sub_labels = raw_sub_labels + tuple(q for q in qualifiers if q not in raw_sub_labels)
        return Atom(
            atom_id=str(row["id"]),
            camera=camera,
            start_time=start_time,
            end_time=end_time,
            labels=labels,
            zones=_strings(data.get("zones")),
            event_ids=_strings(data.get("detections")),
            sub_labels=sub_labels,
            severity=str(row["severity"]),
        )

    def reconcile(self) -> ReconcileStats:
        """BACKFILL/repair sweep over `reviewsegment` -- the "belt" catching
        anything the MQTT path missed or saw only partially. Sync; run via
        `asyncio.to_thread` from the server's loop."""
        # Rebuild every reconcile (cheap) so gap_s/max_duration_s/
        # recent_cameras/min_copresence_s pick up a tuning override live,
        # without needing a restart to recreate the service.
        self._cfg = _linker_config(self.settings)
        now = self._now()
        frigate_conn = db.open_frigate_ro(self.settings.frigate.db_path)
        sidecar_conn = self._conn()
        try:
            watermark = store.get_watermark(sidecar_conn)
            if watermark is None:
                since = now - self.settings.encounters.backfill_lookback_s
            else:
                since = watermark - self._cfg.max_duration_s

            try:
                rows = frigate_conn.execute(
                    "SELECT id, camera, start_time, end_time, severity, data "
                    "FROM reviewsegment WHERE start_time >= ? ORDER BY start_time",
                    (since,),
                ).fetchall()
            except sqlite3.Error:
                rows = []

            atoms = sorted(
                (self._atom_from_review_row(row) for row in rows), key=lambda a: a.start_time
            )

            open_encounters = store.load_open(sidecar_conn, now)
            by_id = {e.encounter_id: e for e in open_encounters}
            new_count = 0
            updated_count = 0
            for atom in atoms:
                existing = sidecar_conn.execute(
                    "SELECT 1 FROM encounter_members WHERE atom_id = ?", (atom.atom_id,)
                ).fetchone()
                pinned_to, split_from = self._pin_split(sidecar_conn, atom.atom_id)
                decision = decide(
                    atom,
                    list(by_id.values()),
                    self.adjacency,
                    self._cfg,
                    now=now,
                    pinned_to=pinned_to,
                    split_from=split_from,
                )
                encounter_id = store.upsert_atom(sidecar_conn, atom, decision, now)
                if encounter_id in by_id:
                    apply(by_id[encounter_id], atom, now)
                else:
                    refreshed = store.load_one(sidecar_conn, encounter_id, now)
                    if refreshed is not None:
                        by_id[encounter_id] = refreshed
                if existing is None:
                    new_count += 1
                else:
                    updated_count += 1

            sealed = store.seal_stale(sidecar_conn, now, self._cfg)
            if atoms:
                store.set_watermark(sidecar_conn, max(a.start_time for a in atoms))

            stats = ReconcileStats(
                rows=len(rows), new=new_count, updated=updated_count, sealed=sealed
            )
            self._last_reconcile = stats
            self._last_reconcile_at = now
            self._last_error = None
            return stats
        except Exception as exc:
            self._last_error = str(exc)
            raise
        finally:
            frigate_conn.close()
            sidecar_conn.close()

    def status(self) -> dict[str, object]:
        """Status summary for `/healthz`."""
        state = "error" if self._last_error is not None else "ok"
        out: dict[str, object] = {"state": state}
        if self._last_reconcile is not None:
            out["last_reconcile"] = {
                "rows": self._last_reconcile.rows,
                "new": self._last_reconcile.new,
                "updated": self._last_reconcile.updated,
                "sealed": self._last_reconcile.sealed,
                "age_s": round(self._now() - (self._last_reconcile_at or self._now()), 1),
            }
        if self._last_error is not None:
            out["error"] = self._last_error
        return out
