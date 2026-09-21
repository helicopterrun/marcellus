"""EncounterService: live hook, reconciler, PushEngine wiring
(docs/encounters.md "Tests" section)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection, Settings
from marcellus.encounters import store
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.service import EncounterService
from marcellus.push.decision import parse_review_message
from marcellus.push.engine import PushEngine
from marcellus.push.models import ReviewEvent
from marcellus.push.transport import LogTransport
from tests.test_scrub import REVIEWSEGMENT_SCHEMA

FIXTURES = Path(__file__).parent / "fixtures"


def _settings(tmp_path: Path, frigate_db: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate={"base_url": "http://frigate.test:5000", "config_path": cfg, "db_path": frigate_db},
        sidecar={"db_path": tmp_path / "sidecar.db"},
    )


def _reviewsegment_db(tmp_path: Path, name: str = "frigate.db") -> Path:
    p = tmp_path / name
    conn = sqlite3.connect(p)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    conn.commit()
    conn.close()
    return p


def _insert_review(
    path: Path,
    *,
    rid: str,
    camera: str,
    start: float,
    end: float | None,
    severity: str = "alert",
    objects: tuple[str, ...] = ("person",),
    detections: tuple[str, ...] = (),
    zones: tuple[str, ...] = (),
    sub_labels: tuple[str, ...] = (),
) -> None:
    conn = sqlite3.connect(path)
    data = json.dumps(
        {
            "objects": list(objects),
            "detections": list(detections),
            "zones": list(zones),
            "sub_labels": list(sub_labels),
        }
    )
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (rid, camera, start, end, severity, data),
    )
    conn.commit()
    conn.close()


def test_reconcile_normalises_sub_label_qualified_objects(tmp_path: Path) -> None:
    """Production Frigate promotes a sub_label into `data.objects` alongside
    the base label (e.g. "person-verified" next to "person"). A plain
    `person` atom followed 120s later, on an adjacent camera, by a
    "person-verified" atom should link as one encounter -- and the linked
    member's labels/sub_labels should reflect the normalised split, not the
    raw hyphenated string."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 300,
        end=now - 290,
        objects=("person",),
    )
    _insert_review(
        frigate_db,
        rid="r2",
        camera="shed",
        start=now - 290 + 120,
        end=now - 290 + 130,
        objects=("person", "person-verified"),
    )

    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate={"base_url": "http://frigate.test:5000", "config_path": cfg, "db_path": frigate_db},
        sidecar={"db_path": tmp_path / "sidecar.db"},
        encounters={"gap_s": {"person": 150.0, "default": 60.0}},
    )

    adjacency = Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})}))
    service = EncounterService(settings, adjacency=adjacency, now=lambda: now)
    stats = service.reconcile()
    assert stats.rows == 2
    assert stats.new == 2

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = {
            r["atom_id"]: dict(r)
            for r in conn.execute("SELECT * FROM encounter_members").fetchall()
        }
        assert rows["r1"]["encounter_id"] == rows["r2"]["encounter_id"]
        assert rows["r2"]["link_reason"] in ("identity", "adjacent")
        assert json.loads(rows["r2"]["labels_json"]) == ["person"]
        assert "verified" in json.loads(rows["r2"]["sub_labels_json"])
    finally:
        conn.close()


def test_reconcile_over_reviewsegment(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 100,
        end=now - 90,
        objects=("person",),
        detections=("ev1",),
    )
    # NULL end_time: still-open review.
    _insert_review(
        frigate_db,
        rid="r2",
        camera="alley-wide",
        start=now - 80,
        end=None,
        objects=("person",),
        detections=("ev2",),
    )
    # A camera with a sidecar-side clock offset.
    _insert_review(
        frigate_db,
        rid="r3",
        camera="shed",
        start=now - 60,
        end=now - 50,
        objects=("raccoon",),
        detections=("ev3",),
    )

    settings = _settings(tmp_path, frigate_db)
    sidecar_conn = db.open_sidecar(settings.sidecar.db_path)
    db.set_event_clock_offset(sidecar_conn, "shed", 2000)
    sidecar_conn.commit()
    sidecar_conn.close()

    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)
    stats = service.reconcile()
    assert stats.rows == 3
    assert stats.new == 3

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        all_rows = conn.execute("SELECT * FROM encounter_members")
        rows = {r["atom_id"]: r for r in (dict(x) for x in all_rows)}
        assert set(rows) == {"r1", "r2", "r3"}
        # r2's end_time stays NULL (still open in Frigate).
        assert rows["r2"]["end_time"] is None
        # r3's start_time got the sidecar offset applied (2000ms = 2s).
        assert rows["r3"]["start_time"] == pytest.approx(now - 60 + 2.0)
        watermark = store.get_watermark(conn)
        assert watermark == pytest.approx(now - 60 + 2.0)  # max start_time seen, offset applied
    finally:
        conn.close()


async def test_live_then_reconcile_agree_no_duplicates(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    # Live: a "new" then an "end" for the same review. observe_review only
    # enqueues (non-blocking, off the event loop) -- process_pending drains
    # the queue synchronously for the test, same work run_worker would do.
    ev_new = ReviewEvent(
        review_id="r1",
        camera="alley-wide",
        severity="alert",
        labels=("person",),
        msg_type="new",
        track_ids=("ev1",),
        start_time=now - 100,
    )
    service.observe_review(ev_new)
    ev_end = ReviewEvent(
        review_id="r1",
        camera="alley-wide",
        severity="alert",
        labels=("person",),
        msg_type="end",
        track_ids=("ev1",),
        start_time=now - 100,
        end_time=now - 97.0,  # Frigate's real end_time -- preferred over wall clock
    )
    service.observe_review(ev_end)
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute("SELECT * FROM encounter_members WHERE atom_id = 'r1'").fetchone()
        assert row is not None
        assert row["end_time"] == now - 97.0  # Frigate's end_time, not the wall clock

        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()

    # Now the same review shows up in reviewsegment (as Frigate would persist
    # it) -- reconcile must not create a duplicate atom.
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 100,
        end=now - 95,
        objects=("person",),
        detections=("ev1",),
    )
    stats = service.reconcile()
    assert stats.rows == 1
    assert stats.new == 0
    assert stats.updated == 1

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
        row = conn.execute("SELECT end_time FROM encounter_members WHERE atom_id = 'r1'").fetchone()
        assert row["end_time"] == now - 95
    finally:
        conn.close()


async def test_live_founder_rehomed_into_companion_encounter(tmp_path: Path) -> None:
    """A dog atom with no shared label family with the person atom next to it
    founds its own encounter on first sight (no overlap yet). Once a later
    "update" message shows real copresence with the person's still-open
    encounter, the dog's lone-founder encounter must be re-homed into it as
    "companion" -- and the dog's old encounter must be gone."""
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    clock = [10.0]
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: clock[0]
    )

    service.observe_review(
        ReviewEvent(
            review_id="p1",
            camera="alley-wide",
            severity="alert",
            labels=("person",),
            msg_type="new",
            start_time=10.0,
        )
    )
    await service.process_pending()

    clock[0] = 11.0
    service.observe_review(
        ReviewEvent(
            review_id="d1",
            camera="alley-wide",
            severity="detection",
            labels=("dog",),
            msg_type="new",
            start_time=11.0,
        )
    )
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        person_row = conn.execute(
            "SELECT encounter_id FROM encounter_members WHERE atom_id = 'p1'"
        ).fetchone()
        dog_row = conn.execute(
            "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = 'd1'"
        ).fetchone()
        assert dog_row["link_reason"] == "new"
        assert dog_row["encounter_id"] != person_row["encounter_id"]
        dog_old_encounter_id = dog_row["encounter_id"]
        person_encounter_id = person_row["encounter_id"]
    finally:
        conn.close()

    clock[0] = 16.0
    service.observe_review(
        ReviewEvent(
            review_id="d1",
            camera="alley-wide",
            severity="detection",
            labels=("dog",),
            msg_type="update",
            start_time=11.0,
        )
    )
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        dog_row = conn.execute(
            "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = 'd1'"
        ).fetchone()
        assert dog_row["encounter_id"] == person_encounter_id
        assert dog_row["link_reason"] == "companion"

        assert store.get(conn, dog_old_encounter_id) is None

        enc_row = store.get(conn, person_encounter_id)
        assert enc_row is not None
        assert enc_row["atom_count"] == 2
        assert "dog" in json.loads(enc_row["labels_json"])
    finally:
        conn.close()


def test_reconcile_founder_rehomed_into_companion_encounter(tmp_path: Path) -> None:
    """Same scenario as the live-hook version, but driven entirely through
    `reconcile()` from `reviewsegment` rows with real end_times -- the
    backfill path must reach the same result."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="p1",
        camera="alley-wide",
        start=now,
        end=now + 6,
        objects=("person",),
    )
    _insert_review(
        frigate_db,
        rid="d1",
        camera="alley-wide",
        start=now + 1,
        end=now + 1.5,
        objects=("dog",),
    )

    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now + 6
    )
    stats = service.reconcile()
    assert stats.new == 2

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        person_row = conn.execute(
            "SELECT encounter_id FROM encounter_members WHERE atom_id = 'p1'"
        ).fetchone()
        dog_row = conn.execute(
            "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = 'd1'"
        ).fetchone()
        assert dog_row["link_reason"] == "new"
        assert dog_row["encounter_id"] != person_row["encounter_id"]
        dog_old_encounter_id = dog_row["encounter_id"]
        person_encounter_id = person_row["encounter_id"]
    finally:
        conn.close()

    # Frigate extends the dog's review item -- now it genuinely overlaps the
    # still-open person encounter for 5s (>= the 3s default min_copresence_s).
    conn = sqlite3.connect(frigate_db)
    conn.execute("UPDATE reviewsegment SET end_time = ? WHERE id = 'd1'", (now + 6,))
    conn.commit()
    conn.close()

    stats = service.reconcile()
    assert stats.updated >= 1

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        dog_row = conn.execute(
            "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = 'd1'"
        ).fetchone()
        assert dog_row["encounter_id"] == person_encounter_id
        assert dog_row["link_reason"] == "companion"

        assert store.get(conn, dog_old_encounter_id) is None

        enc_row = store.get(conn, person_encounter_id)
        assert enc_row is not None
        assert enc_row["atom_count"] == 2
        assert "dog" in json.loads(enc_row["labels_json"])
    finally:
        conn.close()


async def test_live_start_time_zero_skipped_without_member_row(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    service.observe_review(
        ReviewEvent(
            review_id="r1",
            camera="alley-wide",
            severity="alert",
            labels=("person",),
            msg_type="new",
            start_time=0.0,
        )
    )
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 0
    finally:
        conn.close()


async def test_live_start_time_zero_keeps_stored_start_time(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    now = time.time()
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    service.observe_review(
        ReviewEvent(
            review_id="r1",
            camera="alley-wide",
            severity="alert",
            labels=("person",),
            msg_type="new",
            start_time=now - 50,
        )
    )
    await service.process_pending()

    # A later "update" arrives with start_time 0.0 (the parser bug) -- the
    # stored start_time must not regress to 0.
    service.observe_review(
        ReviewEvent(
            review_id="r1",
            camera="alley-wide",
            severity="alert",
            labels=("person",),
            msg_type="update",
            start_time=0.0,
        )
    )
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT start_time FROM encounter_members WHERE atom_id = 'r1'"
        ).fetchone()
        assert row["start_time"] == pytest.approx(now - 50)
    finally:
        conn.close()


async def test_observe_review_does_not_touch_sqlite_synchronously(tmp_path: Path) -> None:
    """`observe_review` must be a non-blocking enqueue only -- no sqlite
    connection opened on the caller's thread/loop. Nothing is linked until
    something actually drains the queue (`process_pending` here, `run_worker`
    in the running app)."""
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    ev = ReviewEvent(
        review_id="r1",
        camera="alley-wide",
        severity="alert",
        labels=("person",),
        start_time=time.time(),
    )
    service.observe_review(ev)

    # The sidecar DB file shouldn't even exist yet -- open_sidecar's
    # CREATE-schema-on-first-open hasn't run, because nothing has opened a
    # connection at all.
    assert not settings.sidecar.db_path.exists()

    await service.process_pending()
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        assert n == 1
    finally:
        conn.close()


async def test_observe_review_drops_and_logs_once_when_queue_full(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))
    # Shrink the queue after construction so the test doesn't need 1000 events.
    service._queue = asyncio.Queue(maxsize=2)

    for i in range(5):
        service.observe_review(
            ReviewEvent(review_id=f"r{i}", camera="alley-wide", severity="alert")
        )

    assert service._queue.qsize() == 2  # first two accepted, rest dropped
    assert service._queue_drop_logged is True


async def test_replay_capture_charger_loiter_produces_sane_grouping(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    fixture = FIXTURES / "capture-charger-loiter.jsonl"
    with fixture.open() as fh:
        for line in fh:
            msg = json.loads(line)
            if msg.get("topic") != "frigate/reviews":
                continue
            event = parse_review_message(msg["payload"])
            if event is not None:
                service.observe_review(event)
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        n_members = conn.execute("SELECT COUNT(*) AS n FROM encounter_members").fetchone()["n"]
        n_encounters = conn.execute("SELECT COUNT(*) AS n FROM encounters").fetchone()["n"]
        assert n_members == 3  # 3 distinct review ids in the fixture (2 new + 1 update)
        assert 1 <= n_encounters <= 3
    finally:
        conn.close()


def test_observe_review_exception_does_not_propagate_through_handle_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()))

    def _boom(ev: ReviewEvent) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "observe_review", _boom)

    engine = PushEngine(
        db_path=str(settings.sidecar.db_path),
        transport=LogTransport(),
        server_id="s1",
        on_review=service.observe_review,
    )
    engine.push_config = PushSection(delivery_enabled=True)

    event = ReviewEvent(review_id="r1", camera="doorbell", severity="alert", labels=("person",))
    # Must not raise, even though the hook always blows up.
    sent = asyncio.run(engine.handle_event(event))
    assert isinstance(sent, int)


# --- Reconcile hygiene: one txn, skip-unchanged, per-atom error isolation,
# vanished-segment cleanup, retention (docs/encounters.md "Tests" section).


def _three_atom_reviews(frigate_db: Path, now: float) -> None:
    # Same camera, close together so all three link into one encounter --
    # none stays a lone founder, so `member_unchanged` skip applies to all
    # three on a repeat reconcile (a genuine lone founder is always
    # re-decided, never skipped, since a later message might re-home it).
    for rid, start in (("r1", now - 300), ("r2", now - 280), ("r3", now - 260)):
        _insert_review(
            frigate_db,
            rid=rid,
            camera="alley-wide",
            start=start,
            end=start + 5,
            objects=("person",),
        )


def test_reconcile_skips_unchanged_atoms_on_second_run(tmp_path: Path) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _three_atom_reviews(frigate_db, now)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    first = service.reconcile()
    assert first.rows == 3
    assert first.new == 3

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        encounter_ids_before = {
            r["atom_id"]: r["encounter_id"]
            for r in conn.execute("SELECT atom_id, encounter_id FROM encounter_members")
        }
    finally:
        conn.close()

    second = service.reconcile()
    assert second.skipped == 3
    assert second.new == 0
    assert second.updated == 0

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        encounter_ids_after = {
            r["atom_id"]: r["encounter_id"]
            for r in conn.execute("SELECT atom_id, encounter_id FROM encounter_members")
        }
    finally:
        conn.close()
    assert encounter_ids_after == encounter_ids_before


def test_reconcile_isolates_a_failing_atom(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _three_atom_reviews(frigate_db, now)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    real_upsert = store.upsert_atom

    def _flaky(conn, atom, decision, now_, **kw):  # type: ignore[no-untyped-def]
        if atom.atom_id == "r2":
            raise RuntimeError("boom")
        return real_upsert(conn, atom, decision, now_, **kw)

    monkeypatch.setattr(store, "upsert_atom", _flaky)

    stats = service.reconcile()
    assert stats.errors == 1
    assert stats.new == 2

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = {r["atom_id"] for r in conn.execute("SELECT atom_id FROM encounter_members")}
        assert rows == {"r1", "r3"}
        watermark = store.get_watermark(conn)
        assert watermark == pytest.approx(now - 260)
    finally:
        conn.close()


def test_reconcile_removes_vanished_segment_and_shrinks_encounter(tmp_path: Path) -> None:
    """Both atoms are closed (`end_time` set) well past `_VANISH_GRACE_S`, so
    they're eligible for vanished-segment removal once dropped from
    `reviewsegment`."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="a1",
        camera="alley-wide",
        start=now - 1000,
        end=now - 995,
        objects=("person",),
    )
    _insert_review(
        frigate_db,
        rid="a2",
        camera="alley-wide",
        start=now - 990,
        end=now - 985,
        objects=("person",),
    )
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    first = service.reconcile()
    assert first.new == 2
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        rows = {
            r["atom_id"]: r["encounter_id"] for r in conn.execute("SELECT * FROM encounter_members")
        }
        assert rows["a1"] == rows["a2"]
        enc_id = rows["a1"]
    finally:
        conn.close()

    conn = sqlite3.connect(frigate_db)
    conn.execute("DELETE FROM reviewsegment WHERE id = 'a2'")
    conn.commit()
    conn.close()

    second = service.reconcile()
    assert second.removed == 1
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute("SELECT atom_id FROM encounter_members WHERE atom_id = 'a2'").fetchone()
        assert row is None
        enc_row = store.get(conn, enc_id)
        assert enc_row is not None
        assert enc_row["atom_count"] == 1
    finally:
        conn.close()

    conn = sqlite3.connect(frigate_db)
    conn.execute("DELETE FROM reviewsegment WHERE id = 'a1'")
    conn.commit()
    conn.close()

    third = service.reconcile()
    assert third.removed == 1
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert store.get(conn, enc_id) is None
    finally:
        conn.close()


async def test_reconcile_never_removes_an_open_live_linked_member(tmp_path: Path) -> None:
    """Frigate publishes the MQTT new/update message (which the live hook
    links right away) before it writes the `reviewsegment` row -- that only
    happens once the segment closes. A member the live hook just linked, with
    no matching `reviewsegment` row yet and `end_time IS NULL`, must never be
    treated as vanished, no matter how long `reconcile` waits."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    service.observe_review(
        ReviewEvent(
            review_id="live1",
            camera="alley-wide",
            severity="alert",
            labels=("person",),
            msg_type="new",
            start_time=now - 1000,
        )
    )
    await service.process_pending()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT end_time FROM encounter_members WHERE atom_id = 'live1'"
        ).fetchone()
        assert row is not None
        assert row["end_time"] is None
    finally:
        conn.close()

    # `reviewsegment` never gets a row for it (still open in Frigate) --
    # reconcile must leave it alone regardless of grace elapsed.
    stats = service.reconcile()
    assert stats.removed == 0

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT atom_id FROM encounter_members WHERE atom_id = 'live1'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_reconcile_vanish_grace_window(tmp_path: Path) -> None:
    """A closed member missing from `reviewsegment` survives inside the
    `_VANISH_GRACE_S` window and is removed once past it."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="fresh",
        camera="alley-wide",
        start=now - 2000,
        end=now - 60,  # closed 60s ago -- inside the 300s grace
        objects=("person",),
    )
    _insert_review(
        frigate_db,
        rid="old",
        camera="alley-wide",
        start=now - 2500,
        end=now - 600,  # closed 600s ago -- past the 300s grace
        objects=("person",),
    )
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    first = service.reconcile()
    assert first.new == 2

    conn = sqlite3.connect(frigate_db)
    conn.execute("DELETE FROM reviewsegment WHERE id IN ('fresh', 'old')")
    conn.commit()
    conn.close()

    second = service.reconcile()
    assert second.removed == 1

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert (
            conn.execute("SELECT atom_id FROM encounter_members WHERE atom_id = 'fresh'").fetchone()
            is not None
        )
        assert (
            conn.execute("SELECT atom_id FROM encounter_members WHERE atom_id = 'old'").fetchone()
            is None
        )
    finally:
        conn.close()


class _ConnProxy:
    """Thin wrapper forwarding everything to a real sqlite3.Connection --
    `sqlite3.Connection` is a C type and can't have its methods patched
    directly (`cannot set 'execute' attribute of immutable type`), so tests
    that need to intercept one connection's calls wrap the connection
    object itself instead of the class."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def test_reconcile_skips_removal_when_frigate_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="a1",
        camera="alley-wide",
        start=now - 100,
        end=now - 95,
        objects=("person",),
    )
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)
    first = service.reconcile()
    assert first.new == 1

    class _BoomProxy(_ConnProxy):
        def execute(self, sql, *a, **kw):  # type: ignore[no-untyped-def]
            if "FROM reviewsegment" in sql:
                raise sqlite3.OperationalError("boom")
            return self._conn.execute(sql, *a, **kw)

    real_open_frigate_ro = db.open_frigate_ro
    monkeypatch.setattr(db, "open_frigate_ro", lambda path: _BoomProxy(real_open_frigate_ro(path)))
    stats = service.reconcile()
    assert stats.removed == 0

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute("SELECT atom_id FROM encounter_members WHERE atom_id = 'a1'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_reconcile_commits_at_most_three_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _three_atom_reviews(frigate_db, now)
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)

    commit_count = 0

    class _CountingProxy(_ConnProxy):
        def commit(self) -> None:
            nonlocal commit_count
            commit_count += 1
            self._conn.commit()

    real_conn = service._conn
    monkeypatch.setattr(service, "_conn", lambda: _CountingProxy(real_conn()))
    service.reconcile()
    assert commit_count <= 3


def test_encounters_prune_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from marcellus.cli import app as cli_app

    frigate_db = _reviewsegment_db(tmp_path)
    settings = _settings(tmp_path, frigate_db)
    monkeypatch.setenv("MARCELLUS_SIDECAR__DB_PATH", str(settings.sidecar.db_path))
    monkeypatch.setenv("MARCELLUS_FRIGATE__DB_PATH", str(frigate_db))
    monkeypatch.setenv("MARCELLUS_FRIGATE__CONFIG_PATH", str(tmp_path / "frigate-config.yml"))
    (tmp_path / "frigate-config.yml").write_text("cameras: {}\n")

    conn = db.open_sidecar(settings.sidecar.db_path)
    conn.close()

    runner = CliRunner()
    result = runner.invoke(cli_app, ["encounters", "prune"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"encounters", "members", "decisions"}


def test_reconcile_picks_up_live_adjacency_override(tmp_path: Path) -> None:
    """encounters.adjacency is a live tuning key (Part B): a reconcile after
    the override changes must use the new edge without recreating the
    service."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db,
        rid="r1",
        camera="alley-wide",
        start=now - 200,
        end=now - 190,
        objects=("person",),
    )

    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now)
    stats = service.reconcile()
    assert stats.new == 1
    assert not service.adjacency.adjacent("alley-wide", "street")

    _insert_review(
        frigate_db,
        rid="r2",
        camera="street",
        start=now - 150,
        end=now - 140,
        objects=("person",),
    )

    # No override yet -- the new atom on "street" isn't adjacent to
    # "alley-wide", so it starts a new encounter.
    stats2 = service.reconcile()
    assert stats2.new == 1

    conn = db.open_sidecar(settings.sidecar.db_path)
    before = conn.execute("SELECT COUNT(*) AS c FROM encounters").fetchone()["c"]
    conn.close()
    assert before == 2

    # Live-override the adjacency graph with a brand-new camera ("yard") not
    # sharing a camera with either open encounter -- it can only link via
    # the "adjacent" continuity rule, exercising the override.
    settings.encounters.adjacency = [["street", "yard"]]
    _insert_review(
        frigate_db,
        rid="r3",
        camera="yard",
        start=now - 90,
        end=now - 80,
        objects=("person",),
    )
    service.reconcile()
    assert service.adjacency.adjacent("street", "yard")

    conn = db.open_sidecar(settings.sidecar.db_path)
    r3_row = conn.execute(
        "SELECT encounter_id, link_reason FROM encounter_members WHERE atom_id = 'r3'"
    ).fetchone()
    r2_row = conn.execute(
        "SELECT encounter_id FROM encounter_members WHERE atom_id = 'r2'"
    ).fetchone()
    conn.close()
    assert r3_row["link_reason"] == "adjacent"
    assert r3_row["encounter_id"] == r2_row["encounter_id"]


def test_reconcile_honours_split_across_two_cycles(tmp_path: Path) -> None:
    """After a human splits an atom out of its encounter, a later reconcile
    (even one where the old encounter is still open and would otherwise be
    the obvious re-link target) must not re-join it -- `split_from` (fed by
    `service._pin_split`) excludes the donor, and the `pinned/split`
    membership lock in `store.upsert_atom` refuses any move regardless."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    _insert_review(
        frigate_db, rid="p1", camera="alley-wide", start=now, end=now + 6, objects=("person",)
    )
    _insert_review(
        frigate_db,
        rid="p2",
        camera="alley-wide",
        start=now + 10,
        end=now + 16,
        objects=("person",),
    )

    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(
        settings, adjacency=Adjacency(edges=frozenset()), now=lambda: now + 20
    )
    stats = service.reconcile()
    assert stats.new == 2

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        p1_encounter = store.member_row(conn, "p1")["encounter_id"]
        p2_encounter = store.member_row(conn, "p2")["encounter_id"]
        assert p1_encounter == p2_encounter  # same_camera/gap linked them
        donor_id = store.split_atom(conn, "p2", now + 20)
        assert donor_id != p1_encounter
    finally:
        conn.close()

    # Cycle 1: p2's reviewsegment end_time extends -- would ordinarily still
    # link back to p1's encounter (same camera, in-gap) but must not.
    conn = sqlite3.connect(frigate_db)
    conn.execute("UPDATE reviewsegment SET end_time = ? WHERE id = 'p2'", (now + 17,))
    conn.commit()
    conn.close()
    service.reconcile()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = store.member_row(conn, "p2")
        assert row["encounter_id"] != p1_encounter
        assert row["link_reason"] == "split"
        first_encounter = row["encounter_id"]
    finally:
        conn.close()

    # Cycle 2: still split, still not re-joined.
    conn = sqlite3.connect(frigate_db)
    conn.execute("UPDATE reviewsegment SET end_time = ? WHERE id = 'p2'", (now + 18,))
    conn.commit()
    conn.close()
    service.reconcile()

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = store.member_row(conn, "p2")
        assert row["encounter_id"] == first_encounter
        assert row["encounter_id"] != p1_encounter
        assert row["link_reason"] == "split"
    finally:
        conn.close()


def test_linker_config_loads_transitions_only_when_use_learned_gaps(tmp_path: Path) -> None:
    """M3: `_linker_config` (via `EncounterService.reconcile`) only loads
    `camera_transitions` into `LinkerConfig.transitions` when
    `encounters.use_learned_gaps` is on -- off (the default) must leave it
    None even if rows exist in the table."""
    frigate_db = _reviewsegment_db(tmp_path)
    now = time.time()
    settings = _settings(tmp_path, frigate_db)
    service = EncounterService(
        settings,
        adjacency=Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})})),
        now=lambda: now,
    )

    conn = db.open_sidecar(settings.sidecar.db_path)
    conn.execute(
        "INSERT INTO camera_transitions (cam_a, cam_b, family, samples, p10_s, p50_s, "
        "p90_s, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("alley-wide", "shed", "animal", 20, 10.0, 20.0, 30.0, "learned", now),
    )
    conn.commit()
    conn.close()

    service.reconcile()
    assert service._cfg.transitions is None

    settings.encounters.use_learned_gaps = True
    service.reconcile()
    assert service._cfg.transitions is not None
    assert ("alley-wide", "shed", "animal") in service._cfg.transitions
    stats = service._cfg.transitions[("alley-wide", "shed", "animal")]
    assert stats.source == "learned"
    assert stats.p90 == 30.0
    assert service._cfg.transition_slack == settings.encounters.transition_slack
