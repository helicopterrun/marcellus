"""`sweep_activities` sends outside `_pipeline_lock` (Wave 2B §2).

A fake transport whose `send_live_activity` blocks on a controllable
`asyncio.Event` stands in for the network round trip, so the test can prove
the lock is released *during* that send (a concurrent per-frame handler for
an unrelated camera completes without waiting on it) and that both stale
activities still end and close once the send(s) resolve.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import store
from marcellus.push.engine import PushEngine
from marcellus.push.stats import STATS
from marcellus.push.transport import TransportResult


class BlockingLiveActivityTransport:
    """`send_live_activity` records the call, then awaits `gate` before
    returning `ok=True` -- `gate.set()` (called by the test) releases every
    call blocked on it, including ones made after `set()`, so only the
    *first* call in a sequential loop actually pauses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.gate = asyncio.Event()

    async def send(self, *args: object, **kwargs: object) -> TransportResult:
        return TransportResult(ok=True)

    async def send_situation(self, *args: object, **kwargs: object) -> TransportResult:
        return TransportResult(ok=True)

    async def send_live_activity(
        self, device: object, *, token: str, payload: dict, collapse_id: str, event: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        self.calls.append((getattr(device, "apns_token", ""), event))
        await self.gate.wait()
        return TransportResult(ok=True)

    async def send_test(self, *args: object, **kwargs: object) -> TransportResult:
        return TransportResult(ok=True)


def _seed_stale_activity(
    conn: object, *, apns_token: str, camera: str, activity_id: str, stale_at: float
) -> None:
    """A device-scoped Live Activity (per store.py's `DEVICE_SITUATION_ID`
    sentinel) last touched at `stale_at`, with a real per-activity token so
    `_end_activity` actually calls the transport."""
    store.upsert_device(
        conn, apns_token=apns_token, bundle_id="com.example.elsinore",
        environment="sandbox", cameras=[], min_severity="alert",
    )
    store.open_activity(
        conn, activity_id=activity_id, apns_token=apns_token,
        situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID,
        camera=camera, collapse_id=store.DEVICE_SITUATION_ID, handle="", now=stale_at,
    )
    store.attach_activity_token(
        conn, activity_id=activity_id, apns_token=apns_token,
        situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID,
        token=f"la-{activity_id}", now=stale_at,
    )


async def test_sweep_releases_lock_during_send_and_ends_both_activities(
    tmp_path: Path,
) -> None:
    STATS.reset()
    db_path = tmp_path / "sidecar.db"
    now = time.time()
    stale_at = now - 40.0  # activity_resolution_s default is 30.0

    conn = db.open_sidecar(db_path)
    try:
        _seed_stale_activity(
            conn, apns_token="tokA", camera="doorbell", activity_id="a_A", stale_at=stale_at,
        )
        _seed_stale_activity(
            conn, apns_token="tokB", camera="garden", activity_id="a_B", stale_at=stale_at,
        )
    finally:
        conn.close()

    transport = BlockingLiveActivityTransport()
    engine = PushEngine(
        db_path=str(db_path), transport=transport, server_id="s1",
        activity_resolution_s=30.0, push_config=PushSection(delivery_enabled=True),
    )

    sweep_task = asyncio.create_task(engine.sweep_activities(now=now))
    try:
        # Wait for the sweep's first `_end_activity` send to actually be
        # in flight (blocked on `transport.gate`) before asserting anything.
        for _ in range(1000):
            if transport.calls:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("sweep never reached the blocked send")

        # Both stale rows are marked "ending" up front (during the one
        # locked read phase), even though only one send is in flight at a
        # time -- see `_ending`'s docstring on `_pipeline_lock`.
        assert engine._ending == {"tokA", "tokB"}
        assert engine._pipeline_lock.locked() is False

        # A concurrent per-frame handler for an unrelated camera must not
        # block behind the sweep's in-flight network send.
        await asyncio.wait_for(
            engine.handle_object_payload(
                {"after": {"camera": "unrelated-camera", "id": "trackX"}, "type": "end"}
            ),
            timeout=2.0,
        )
    finally:
        transport.gate.set()

    sent = await asyncio.wait_for(sweep_task, timeout=2.0)
    assert sent == 2
    assert STATS.get("pipeline.sweep.ended") == 2
    assert engine._ending == set()

    conn = db.open_sidecar(db_path)
    try:
        row_a = store.get_activity(conn, "a_A")
        row_b = store.get_activity(conn, "a_B")
    finally:
        conn.close()
    assert row_a is not None and row_a["ended_at"] is not None
    assert row_b is not None and row_b["ended_at"] is not None


def test_live_devices_excludes_ending_tokens(tmp_path: Path) -> None:
    """The filter `handle_event`/`handle_object_payload` apply before calling
    into `delivery_wire`: a device mid-sweep-end is excluded entirely, so it
    is treated as having no open activity for the duration."""
    db_path = tmp_path / "sidecar.db"
    conn = db.open_sidecar(db_path)
    try:
        store.upsert_device(
            conn, apns_token="tokA", bundle_id="com.example.elsinore",
            environment="sandbox", cameras=[], min_severity="alert",
        )
        store.upsert_device(
            conn, apns_token="tokB", bundle_id="com.example.elsinore",
            environment="sandbox", cameras=[], min_severity="alert",
        )
        device_a = store.get_device(conn, "tokA")
        device_b = store.get_device(conn, "tokB")
    finally:
        conn.close()
    assert device_a is not None and device_b is not None

    engine = PushEngine(db_path=str(db_path), transport=None, server_id="s1")  # type: ignore[arg-type]

    # Nothing ending: both pass through untouched.
    assert engine._live_devices([device_a, device_b]) == [device_a, device_b]

    engine._ending.add("tokA")
    assert engine._live_devices([device_a, device_b]) == [device_b]

    # Discarded (as the sweep does once its send completes): both pass again
    # -- a fresh frame can start a new activity for what was excluded.
    engine._ending.discard("tokA")
    assert engine._live_devices([device_a, device_b]) == [device_a, device_b]
