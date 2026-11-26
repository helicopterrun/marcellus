"""`/v1/highlights` reads: the page query and keeping it off the event loop.

The query is checked against the one it replaced on a generated database built
from Frigate's own schema (its indexes included), with enough rows, cameras and
`start_time` ties that a plan or tie-break difference would show.
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from marcellus import db
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.routes import scrub as scrub_routes
from marcellus.server import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "frigate_schema"

#: The query `/v1/highlights` ran before `db.newest_events`, verbatim.
_OLD_SQL = (
    "SELECT * FROM event WHERE camera = ? AND start_time < ? "
    "ORDER BY start_time DESC LIMIT ?"
)

_CAMERAS = {"camera_a": 24_000, "camera_b": 5_000, "camera_c": 1_500, "camera_d": 40}
_LABELS = ("person", "car", "dog", "cat", "package")
_BASE_T = 1_780_000_000.0


def _populate(conn: sqlite3.Connection, seed: int = 7) -> list[float]:
    """Insert ~30k events across four cameras (inserted in shuffled order, so
    rowid order is not time order) with ~30% sharing a start time with the
    previous one. Returns every start time."""
    rng = random.Random(seed)
    rows = []
    n = 0
    for camera, count in _CAMERAS.items():
        t = _BASE_T
        for _ in range(count):
            if rng.random() > 0.3:
                t = round(t + rng.choice([1.0, 2.5, 30.0, 61.0, rng.uniform(1, 900)]), 3)
            n += 1
            data = {"top_score": round(rng.uniform(0.4, 0.99), 3), "type": "object"}
            end = None if rng.random() < 0.01 else t + rng.uniform(1, 120)
            rows.append(
                (f"ev{n:06d}", rng.choice(_LABELS), camera, t, end, "[]", json.dumps(data))
            )
    rng.shuffle(rows)
    conn.executemany(
        "INSERT INTO event (id, label, camera, start_time, end_time, zones, has_clip, "
        "has_snapshot, retain_indefinitely, data) VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?)",
        rows,
    )
    conn.commit()
    return [r[3] for r in rows]


@pytest.fixture(scope="module")
def frigate_like_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, list[float]]]:
    path = tmp_path_factory.mktemp("highlights") / "frigate.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "v0.18.2.sql").read_text())
    starts = _populate(conn)
    conn.close()
    yield path, starts


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _cases(starts: list[float], count: int, seed: int) -> list[tuple[str, float, int, int]]:
    rng = random.Random(seed)
    cameras = [*_CAMERAS, "camera_none"]
    out = []
    for i in range(count):
        before = rng.choice(starts) + rng.choice([0.0, 0.0, 0.0005, 1.0])
        if i % 25 == 0:
            before = rng.choice([_BASE_T - 1, _BASE_T + 1e9])  # before everything / after
        budget = rng.choice([db.NEWEST_EVENTS_SCAN_BUDGET, 3, 40, 400])
        out.append((rng.choice(cameras), before, rng.choice([1, 10, 100, 120]), budget))
    return out


def test_frigate_like_db_is_big_and_tied(frigate_like_db: tuple[Path, list[float]]) -> None:
    path, starts = frigate_like_db
    assert len(starts) >= 30_000
    assert len(set(starts)) < 0.8 * len(starts)  # plenty of ties


def test_newest_events_match_the_old_query_row_for_row(
    frigate_like_db: tuple[Path, list[float]],
) -> None:
    path, starts = frigate_like_db
    conn = _ro(path)
    try:
        for camera, before, limit, budget in _cases(starts, 150, seed=1):
            old = [tuple(r) for r in conn.execute(_OLD_SQL, (camera, before, limit))]
            new = [
                tuple(r)[1:]  # drop the leading `_event_rowid` column
                for r in db.newest_events(conn, camera, before, limit, budget=budget)
            ]
            assert new == old, (camera, before, limit, budget)
    finally:
        conn.close()


def test_newest_events_without_frigate_indexes_match_too(tmp_path: Path) -> None:
    """A bare `event` table (no start-time index) takes the per-camera query."""
    path = tmp_path / "bare.db"
    conn = sqlite3.connect(path)
    ddl = (FIXTURES / "v0.18.2.sql").read_text()
    table = next(s for s in ddl.split(";") if 'CREATE TABLE "event"' in s)
    conn.execute(table)
    starts = _populate(conn, seed=11)
    conn.close()
    conn = _ro(path)
    try:
        assert not db._scan_follows_time_index(conn)
        for camera, before, limit, budget in _cases(starts, 30, seed=2):
            old = [r["id"] for r in conn.execute(_OLD_SQL, (camera, before, limit))]
            new = [r["id"] for r in db.newest_events(conn, camera, before, limit, budget=budget)]
            assert new == old
    finally:
        conn.close()


def test_page_scan_reads_the_start_time_index_without_sorting(
    frigate_like_db: tuple[Path, list[float]],
) -> None:
    path, _ = frigate_like_db
    conn = _ro(path)
    try:
        plan = " ".join(
            str(r[3])
            for r in conn.execute("EXPLAIN QUERY PLAN " + db._NEWEST_SCAN_SQL, (_BASE_T,))
        )
        assert "event_start_time_end_time" in plan
        assert "TEMP B-TREE" not in plan
        assert db._scan_follows_time_index(conn)
    finally:
        conn.close()


def test_newest_events_leaves_no_transaction_open(
    frigate_like_db: tuple[Path, list[float]],
) -> None:
    path, _ = frigate_like_db
    conn = _ro(path)
    try:
        db.newest_events(conn, "camera_a", _BASE_T + 1e9, 10)
        assert not conn.in_transaction
    finally:
        conn.close()


# -- the route ----------------------------------------------------------------


@pytest.fixture
def app(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Any:
    conn = sqlite3.connect(frigate_db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS recordings (camera TEXT, start_time REAL)")
    conn.execute("INSERT INTO recordings VALUES ('camera_a', 0)")
    conn.commit()
    conn.close()
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    return create_app(settings)


def _seed(path: Path, specs: list[tuple[float, float | None]]) -> None:
    conn = sqlite3.connect(path)
    for i, (start, end) in enumerate(specs):
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, zones, data) "
            "VALUES (?, 'camera_a', 'person', ?, ?, '[]', ?)",
            (f"hq{i}", start, end, json.dumps({"top_score": 0.8})),
        )
    conn.commit()
    conn.close()


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_highlights_read_the_db_on_a_worker_thread(
    app: Any, frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(frigate_db_path, [(time.time() - 100, time.time() - 90)])
    seen: list[bool] = []
    real = db.newest_events

    def _spy(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            seen.append(False)
        except RuntimeError:
            seen.append(True)  # no running loop: a worker thread
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "newest_events", _spy)
    async with _client(app) as c:
        r = await c.get("/v1/highlights/camera_a", params={"before": time.time()})
    assert r.status_code == 200
    assert len(r.json()["highlights"]) == 1
    assert seen == [True]


async def test_highlights_never_hold_the_loop_or_more_than_two_threads(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow DB reads (here 0.3 s each, eight at once) neither stall a trivial
    request nor run more than `_HIGHLIGHTS_CONCURRENCY` at a time."""
    running = 0
    peak = 0
    lock = threading.Lock()

    def _slow(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.3)
        with lock:
            running -= 1
        return []

    monkeypatch.setattr(scrub_routes, "_highlight_items", _slow)
    async with _client(app) as c:
        pages = [
            asyncio.ensure_future(
                c.get("/v1/highlights/camera_a", params={"before": time.time() - i})
            )
            for i in range(8)
        ]
        await asyncio.sleep(0.05)
        probe_started = time.monotonic()
        probe = await c.get("/version")
        probe_s = time.monotonic() - probe_started
        results = await asyncio.gather(*pages)
    assert probe.status_code == 200
    assert probe_s < 0.2, f"/version waited {probe_s:.2f}s behind highlights"
    assert all(r.status_code == 200 for r in results)
    assert peak == scrub_routes._HIGHLIGHTS_CONCURRENCY


async def test_unknown_camera_is_still_a_404(app: Any) -> None:
    async with _client(app) as c:
        r = await c.get("/v1/highlights/camera_zz", params={"before": time.time()})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "camera_unknown"


async def test_old_settled_pages_are_cached_briefly(
    app: Any, frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    _seed(frigate_db_path, [(now - 9000, now - 8990), (now - 8000, now - 7990)])
    calls = 0
    real = scrub_routes._highlight_items

    def _count(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(scrub_routes, "_highlight_items", _count)
    old_before = now - 7200
    async with _client(app) as c:
        first = await c.get("/v1/highlights/camera_a", params={"before": old_before})
        second = await c.get("/v1/highlights/camera_a", params={"before": old_before})
        clustered = await c.get(
            "/v1/highlights/camera_a", params={"before": old_before, "cluster_s": 5000}
        )
        assert calls == 1, "the second read of an old, settled page comes from the cache"
        assert first.json() == second.json()
        assert first.headers.get("cache-control") is None
        # Clustering works on a copy: the cached page is unchanged.
        assert clustered.json()["highlights"][0]["events"] == 2
        third = await c.get("/v1/highlights/camera_a", params={"before": old_before})
        assert third.json() == first.json()

        # Past the TTL it is read again.
        real_time = time.time
        monkeypatch.setattr(
            scrub_routes.time,
            "time",
            lambda: real_time() + scrub_routes._HIGHLIGHTS_CACHE_TTL_S + 1,
        )
        await c.get("/v1/highlights/camera_a", params={"before": old_before})
        assert calls == 2


async def test_recent_or_open_pages_are_never_cached(
    app: Any, frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    # An event that started long ago and is still running.
    _seed(frigate_db_path, [(now - 9000, None)])
    calls = 0
    real = scrub_routes._highlight_items

    def _count(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(scrub_routes, "_highlight_items", _count)
    async with _client(app) as c:
        for _ in range(2):
            await c.get("/v1/highlights/camera_a", params={"before": now})  # recent
        for _ in range(2):
            await c.get("/v1/highlights/camera_a", params={"before": now - 7200})  # open event
    assert calls == 4


def test_page_cache_is_bounded() -> None:
    cache = scrub_routes._PageCache(size=3, ttl_s=10.0)
    for i in range(5):
        cache.put(i, [{"i": i}], now=0.0)
    assert cache.get(0, 1.0) is None and cache.get(1, 1.0) is None
    assert cache.get(4, 1.0) == [{"i": 4}]
    assert cache.get(4, 11.0) is None


# -- the items, scores included ------------------------------------------------

_ODD_DATA = [
    '{"top_score": 0.8500000000000001, "score": 0.5}',
    '{"top_score": 0.1, "score": 0.30000000000000004}',
    '{"score": 0.77}',
    '{"top_score": null, "score": 0.6}',
    '{"top_score": "0.9", "score": 0.4}',
    '{"top_score": true}',
    '{"top_score": 1, "score": 0}',
    '{"top_score": 1e-7}',
    '{"top_score": {"v": 1}, "score": [1]}',
    '{"top_score": 0.95, "path_data": [[[0.1, 0.2], 1780000000.5]]}',
    '{"type": "object"}',
    "{}",
    "not json",
    "",
    None,
    # Python's json.dumps writes these; SQLite's json_valid rejects them.
    '{"top_score": NaN, "score": 0.5}',
    '{"top_score": Infinity}',
    '{"top_score": -Infinity, "score": 0.25}',
    '{"score": NaN}',
    '{"top_score": 0.5, "max_severity": "alert"}',
    '{"score": 0.4, "max_severity": "detection"}',
    '{"top_score": 0.6, "max_severity": "other"}',
    '{"top_score": 0.6, "max_severity": null}',
    '{"top_score": NaN, "max_severity": "alert"}',
    # Duplicate keys: SQLite reads the first, Python keeps the last.
    '{"top_score": 0.1, "top_score": 0.9}',
    '{"score": 0.2, "top_score": null, "score": 0.7}',
]


def _old_items(settings: Settings, camera: str, before: float, limit: int) -> list[dict[str, Any]]:
    """The pre-change `/v1/highlights` read, verbatim but for the frame."""
    conn = db.open_frigate_ro(settings.frigate.db_path)
    try:
        shift = scrub_routes._event_clock_shift(settings, camera)
        margin = shift.max_dev_s
        rows = conn.execute(
            _OLD_SQL, (camera, before - shift.constant_s + margin, limit + (20 if margin else 0))
        ).fetchall()
        items = []
        for r in rows:
            h_start = shift.shift(r["start_time"])
            if margin and h_start >= before:
                continue
            item: dict[str, Any] = {
                "id": r["id"],
                "start": h_start,
                "end": shift.shift(r["end_time"]) if r["end_time"] is not None else None,
                "reason": r["label"],
                "score": db.event_top_score(r),
            }
            try:
                blob = json.loads(r["data"]) if r["data"] else None
            except json.JSONDecodeError:
                blob = None
            sev = blob.get("max_severity") if isinstance(blob, dict) else None
            if sev in ("alert", "detection"):
                item["severity"] = sev
            items.append(item)
            if len(items) >= limit:
                break
    finally:
        conn.close()
    return items


@pytest.mark.parametrize("sql_scores", [True, False], ids=["sql-scores", "python-scores"])
@pytest.mark.parametrize("correction", [True, False])
def test_items_match_the_old_read_score_for_score(
    tmp_path: Path, correction: bool, sql_scores: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scrub_routes, "_sql_scores", sql_scores)
    path = tmp_path / "frigate.db"
    conn = sqlite3.connect(path)
    # `data` nullable here, so a NULL blob can be tried too.
    conn.executescript(
        (FIXTURES / "v0.18.2.sql").read_text().replace('"data" JSON NOT NULL', '"data" JSON')
    )
    rng = random.Random(4)
    rows = []
    for i in range(600):
        start = _BASE_T + rng.choice([0, 1, 2, 3]) * 10 + i // 4
        rows.append(
            (
                f"odd{i:04d}", rng.choice(_LABELS), rng.choice(["camera_a", "camera_b"]),
                start, start + 5, "[]", rng.choice(_ODD_DATA),
                rng.choice([None, 0.42]), rng.choice([None, 0.33]),
            )
        )
    rng.shuffle(rows)
    conn.executemany(
        "INSERT INTO event (id, label, camera, start_time, end_time, zones, has_clip, "
        "has_snapshot, retain_indefinitely, data, top_score, score) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "marcellus.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=False, cache_dir=tmp_path / "scrub", segment_clock_correction=correction
        ),
    )
    if sql_scores:
        # Clean rows really are read in SQL, and only the others carry `data`.
        ro = _ro(path)
        try:
            cut = db.newest_events(
                ro,
                "camera_a",
                _BASE_T + 1e9,
                120,
                columns=scrub_routes._HIGHLIGHT_COLUMNS,
                prepare=scrub_routes._HIGHLIGHT_CLEAN,
            )
            clean = [r for r in cut if r["_clean"]]
            unclean = [r for r in cut if not r["_clean"]]
            assert clean and unclean
            assert all(r["data"] is None for r in clean)
            for r in clean:  # each a value's own JSON text
                scrub_routes._json_value(r["_data_top_score"])
            assert any(r["data"] and "NaN" in r["data"] for r in unclean)
            assert any(r["data"] and r["data"].count('"top_score"') == 2 for r in unclean)
        finally:
            ro.close()
    for before in (_BASE_T + 20, _BASE_T + 100, _BASE_T + 1e9):
        for camera in ("camera_a", "camera_b"):
            for limit in (1, 10, 100):
                new = scrub_routes._highlight_items(settings, camera, before, limit)
                # Compared as JSON text: NaN never equals itself.
                assert json.dumps(new) == json.dumps(_old_items(settings, camera, before, limit))


@pytest.mark.parametrize("sql_scores", [True, False], ids=["sql-scores", "python-scores"])
@pytest.mark.parametrize("blob", ['"just a string"', "42", "[0.5]"])
def test_a_non_object_blob_fails_as_it_always_did(
    tmp_path: Path, blob: str, sql_scores: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old read raised on JSON that isn't an object (`.get` on it); so
    does the new one, on either path -- nothing quietly changes."""
    monkeypatch.setattr(scrub_routes, "_sql_scores", sql_scores)
    path = tmp_path / "frigate.db"
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "v0.18.2.sql").read_text())
    conn.execute(
        "INSERT INTO event (id, label, camera, start_time, end_time, zones, has_clip, "
        "has_snapshot, retain_indefinitely, data) "
        "VALUES ('x', 'person', 'camera_a', 1, 2, '[]', 1, 1, 0, ?)",
        (blob,),
    )
    conn.commit()
    conn.close()
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "marcellus.db", bind_port=5001),
        scrub=ScrubSection(enabled=False, cache_dir=tmp_path / "scrub"),
    )
    with pytest.raises(AttributeError):
        _old_items(settings, "camera_a", 10, 10)
    with pytest.raises(AttributeError):
        scrub_routes._highlight_items(settings, "camera_a", 10, 10)


# -- which SQL path, decided once ---------------------------------------------


def test_sql_scores_are_probed_once_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(scrub_routes, "_sql_scores", None)
    with caplog.at_level("INFO", logger="marcellus.routes.scrub"):
        first = scrub_routes.sql_scores_supported()
        second = scrub_routes.sql_scores_supported()
    assert first is second
    lines = [r.getMessage() for r in caplog.records if "highlights: SQLite" in r.getMessage()]
    assert len(lines) == 1
    assert sqlite3.sqlite_version in lines[0]
    if sqlite3.sqlite_version_info >= (3, 38, 0):
        assert first is True and "in SQL" in lines[0]


async def test_a_locked_database_surfaces_and_is_not_retried(
    app: Any, frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"database is locked" propagates exactly as it did before (no second
    read through another path)."""
    calls = 0

    def _locked(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "newest_events", _locked)
    async with _client(app) as c:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await c.get("/v1/highlights/camera_a", params={"before": time.time()})
    assert calls == 1
    assert scrub_routes._highlights_gate(app.state).free == scrub_routes._HIGHLIGHTS_CONCURRENCY


# -- the queue -----------------------------------------------------------------


async def test_a_request_queued_past_the_deadline_gets_503(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scrub_routes, "_HIGHLIGHTS_QUEUE_TIMEOUT_S", 0.1)

    def _slow(*args: Any) -> list[dict[str, Any]]:
        time.sleep(0.4)
        return []

    monkeypatch.setattr(scrub_routes, "_highlight_items", _slow)
    async with _client(app) as c:
        results = await asyncio.gather(
            *(
                c.get("/v1/highlights/camera_a", params={"before": time.time() - i})
                for i in range(3)
            )
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 200, 503]
        busy = next(r for r in results if r.status_code == 503)
        assert busy.headers["retry-after"] == str(scrub_routes._HIGHLIGHTS_RETRY_AFTER_S)
        assert busy.json()["detail"]["error"] == "upstream_busy"
        # Nothing leaked: both slots are free and the next request is served.
        assert scrub_routes._highlights_gate(app.state).free == 2
        again = await c.get("/v1/highlights/camera_a", params={"before": time.time()})
        assert again.status_code == 200


async def test_a_caller_gone_while_queued_costs_no_read(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    async def _gone(self: Any) -> bool:
        return True

    called = False

    def _items(*args: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(Request, "is_disconnected", _gone)
    monkeypatch.setattr(scrub_routes, "_highlight_items", _items)
    async with _client(app) as c:
        r = await c.get("/v1/highlights/camera_a", params={"before": time.time()})
    assert r.status_code == 499
    assert not called
    assert scrub_routes._highlights_gate(app.state).free == 2


async def test_a_cancelled_request_holds_its_slot_until_the_read_ends(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def _blocked(*args: Any) -> list[dict[str, Any]]:
        release.wait(5)
        return []

    monkeypatch.setattr(scrub_routes, "_highlight_items", _blocked)
    async with _client(app) as c:
        task = asyncio.ensure_future(
            c.get("/v1/highlights/camera_a", params={"before": time.time()})
        )
        gate = None
        for _ in range(100):
            await asyncio.sleep(0.01)
            gate = getattr(app.state, "highlights_gate", None)
            if gate is not None and gate.free == 1:
                break
        assert gate is not None and gate.free == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.free == 1  # the read is still running on its thread
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if gate.free == 2:
                break
        assert gate.free == 2


async def test_gate_hands_permits_over_in_order_and_never_drifts() -> None:
    gate = scrub_routes._Gate(1)
    assert await gate.acquire(0.01)
    assert not await gate.acquire(0.01)  # timed out
    assert gate.free == 0

    waiter = asyncio.ensure_future(gate.acquire(1.0))
    await asyncio.sleep(0)
    gate.release()  # handed straight to the waiter
    assert await waiter
    assert gate.free == 0

    # A waiter cancelled just after being handed a permit passes it on.
    late = asyncio.ensure_future(gate.acquire(1.0))
    await asyncio.sleep(0)
    gate.release()
    late.cancel()
    with pytest.raises(asyncio.CancelledError):
        await late
    assert gate.free == 1
    assert await gate.acquire(0.01)
    gate.release()
    assert gate.free == 1
