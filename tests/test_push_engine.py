from __future__ import annotations

from pathlib import Path

from marcellus import db
from marcellus.config import PushSection
from marcellus.push import store
from marcellus.push.engine import PushEngine
from marcellus.push.models import ReviewEvent
from marcellus.push.transport import LogTransport


def _make_engine(db_path: Path, transport=None) -> PushEngine:
    engine = PushEngine(
        db_path=str(db_path), transport=transport or LogTransport(), server_id="s1",
    )
    engine.push_config = PushSection(delivery_enabled=True)
    return engine


def _register(db_path: Path, apns_token: str, **kwargs) -> None:
    conn = db.open_sidecar(db_path)
    store.upsert_device(conn, apns_token=apns_token, bundle_id="com.x", environment="sandbox",
                         **kwargs)
    conn.commit()
    conn.close()


def test_handle_event_card_pipeline_creates_card(tmp_path: Path) -> None:
    """With the card pipeline (Phase 5), a person event at a door zone creates
    a card and sends via send_situation."""
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["doorbell"], min_severity="detection")
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(
        review_id="r1", camera="doorbell", severity="alert", labels=("person",),
        zones=("front_door",),
    )
    import asyncio

    sent = asyncio.run(engine.handle_event(event))
    assert sent == 1
    assert len(transport.sent) >= 1
    payload = transport.sent[0]["payload"]
    assert payload["mutation"] == "create"


def test_handle_event_no_match_sends_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", cameras=["garden"])
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert")
    import asyncio

    asyncio.run(engine.handle_event(event))
    # Card is still mutated (card_key created), but no push sent to devices
    # whose camera filter doesn't match.
    assert transport.sent == []


def test_handle_review_payload_end_to_end(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecar.db"
    _register(db_path, "tok1", min_severity="detection")
    transport = LogTransport()
    engine = _make_engine(db_path, transport)

    payload = {
        "type": "new",
        "after": {
            "id": "r1", "camera": "doorbell", "severity": "alert",
            "data": {"objects": ["person"], "detections": ["ev-1"]},
        },
    }
    import asyncio

    sent = asyncio.run(engine.handle_review_payload(payload))
    assert sent == 1


def test_concurrent_delivery_serializes_la_start(tmp_path: Path) -> None:
    """Two concurrent `handle_event` calls for the same device/story (the
    convoy from prod journal 2026-09-02, where every `frigate/events`/
    `frigate/reviews` message is its own coroutine racing another with its
    own SQLite connection) must not both see `device_row is None` and both
    open-and-send an LA start: `_pipeline_lock` should serialize them so the
    second call finds the first's row already committed."""
    import asyncio

    db_path = tmp_path / "sidecar.db"
    _register(
        db_path, "tok1", cameras=["doorbell"], min_severity="detection",
        push_to_start_token="pts-1", la_capable=True,
    )
    inner = LogTransport()
    release = asyncio.Event()

    class GatedTransport:
        """Wraps LogTransport but blocks every LA *start* send on `release`,
        so two concurrent deliveries are forced to overlap unless the
        engine's own lock keeps them from both reaching this point at
        once."""

        def __init__(self) -> None:
            self.sent = inner.sent

        async def send(self, *args, **kwargs):
            return await inner.send(*args, **kwargs)

        async def send_situation(self, *args, **kwargs):
            return await inner.send_situation(*args, **kwargs)

        async def send_test(self, *args, **kwargs):
            return await inner.send_test(*args, **kwargs)

        async def send_live_activity(self, *args, **kwargs):
            if kwargs.get("event") == "start":
                await release.wait()
            return await inner.send_live_activity(*args, **kwargs)

    engine = _make_engine(db_path, GatedTransport())
    event = ReviewEvent(
        review_id="r1", camera="doorbell", severity="alert", labels=("person",),
        zones=("front_door",),
    )

    async def run() -> None:
        t1 = asyncio.create_task(engine.handle_event(event))
        t2 = asyncio.create_task(engine.handle_event(event))
        # Give both tasks a chance to queue up on the lock before letting
        # the gated LA-start send through.
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(t1, t2)

    asyncio.run(run())

    starts = [r for r in inner.sent if r.get("event") == "start"]
    assert len(starts) == 1

    conn = db.open_sidecar(db_path)
    try:
        (row_count,) = conn.execute("SELECT COUNT(*) FROM push_activities").fetchone()
    finally:
        conn.close()
    assert row_count == 1


def test_maybe_gc_reaps_stale_last_zones(tmp_path: Path) -> None:
    """`_last_zones` piggybacks on `tracks.reap()` the same way `_sub_labels`
    already does: an entry whose track has aged out of `self.tracks` is
    dropped too, so the dict doesn't grow forever for tracks that never send
    an explicit `end` message."""
    db_path = tmp_path / "sidecar.db"
    engine = _make_engine(db_path)
    key = ("doorbell", "t1")

    now = 1_000_000.0
    engine.tracks.observe_object("doorbell", "t1", ("yard",), now=now)
    engine._last_zones[key] = ("yard",)
    engine._sub_labels[key] = "known_person"

    # Not yet stale: gc_interval hasn't elapsed and the track isn't old
    # enough to reap.
    engine._maybe_gc(now)
    assert key in engine._last_zones

    # Advance past both the gc interval and the track's own reap horizon.
    later = now + engine.gc_interval_s + engine.tracks.reap_after_s + 1
    engine._maybe_gc(later)
    assert key not in engine._last_zones
    assert key not in engine._sub_labels
    assert key not in engine.tracks


def test_reset_tracks_clears_last_zones(tmp_path: Path) -> None:
    """A Frigate restart reissues track ids (handoff item 8) -- a stale
    `_last_zones` entry surviving `reset_tracks` would describe a different
    object entirely, exactly like the `_sub_labels`/`tracks` case this
    mirrors."""
    db_path = tmp_path / "sidecar.db"
    engine = _make_engine(db_path)
    engine.tracks.observe_object("doorbell", "t1", ("yard",), now=1.0)
    engine._last_zones[("doorbell", "t1")] = ("yard",)
    engine._sub_labels[("doorbell", "t1")] = "known_person"

    engine.reset_tracks()

    assert engine._last_zones == {}
    assert engine._sub_labels == {}
    assert len(engine.tracks) == 0
