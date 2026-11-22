"""`/v1/motion`'s absolute series (`rate`, `recorded`, `rate_reference`).

Pure apportioning and the reference percentile first, then the read against
both Frigate schema fixtures (indexes included, so the plan is the real one),
then the per-camera cache, then the route: shape, fallbacks and the
capabilities flag.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.routes import scrub as scrub_routes
from marcellus.scrub import motion_rate as mr
from marcellus.server import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "frigate_schema"
SCHEMAS = ("v0.17.2", "v0.18.2")
CAMERA = "cam_a"
T0 = 1_780_000_200.0  # a multiple of 300


# -- apportioning ------------------------------------------------------------


def test_segment_straddling_buckets_is_split_by_overlap() -> None:
    # 20 s of recording, 100 motion, 5 s in bucket 0 and 15 s in bucket 1.
    rate, recorded = mr.apportion([(T0 + 5, T0 + 25, 100.0)], T0, T0 + 20, 10)
    assert rate == [5.0, 5.0]  # 25/5 and 50/10 -- the density, not the share
    assert recorded == [0.5, 1.0]


def test_rate_is_motion_per_recorded_second_not_per_bucket_second() -> None:
    # Two segments in one 60 s bucket: 10 s with 30 motion, 10 s with 0.
    segs = [(T0, T0 + 10, 30.0), (T0 + 30, T0 + 40, 0.0)]
    rate, recorded = mr.apportion(segs, T0, T0 + 60, 60)
    assert rate == [1.5]
    assert recorded == [pytest.approx(0.333)]


def test_gap_and_quiet_buckets_read_differently() -> None:
    # Bucket 0 recorded with no motion, bucket 1 not recorded at all.
    rate, recorded = mr.apportion([(T0, T0 + 10, 0.0)], T0, T0 + 20, 10)
    assert rate == [0.0, 0.0]
    assert recorded == [1.0, 0.0]


def test_no_segments_is_all_zero_on_the_values_grid() -> None:
    rate, recorded = mr.apportion([], T0, T0 + 95, 10)
    assert len(rate) == len(recorded) == 10  # same length as aggregate_motion
    assert set(rate) == set(recorded) == {0.0}


def test_segment_from_before_the_window_counts_only_its_inside_part() -> None:
    # 20 s segment, half before the window: its density still applies.
    rate, recorded = mr.apportion([(T0 - 10, T0 + 10, 40.0)], T0, T0 + 10, 10)
    assert rate == [2.0]
    assert recorded == [1.0]


def test_overlapping_segments_count_once_for_coverage() -> None:
    segs = [(T0, T0 + 10, 10.0), (T0 + 5, T0 + 15, 30.0)]
    rate, recorded = mr.apportion(segs, T0, T0 + 20, 10)
    # Bucket 0: 10 s x 1/s + 5 s x 3/s over 15 recorded seconds.
    assert rate == [pytest.approx(25 / 15, abs=1e-4), 3.0]
    assert recorded == [1.0, 0.5]


def test_contained_overlap_does_not_add_coverage() -> None:
    segs = [(T0, T0 + 20, 0.0), (T0 + 2, T0 + 8, 0.0)]
    _, recorded = mr.apportion(segs, T0, T0 + 20, 10)
    assert recorded == [1.0, 1.0]


def test_last_partial_bucket_is_relative_to_its_in_window_width() -> None:
    rate, recorded = mr.apportion([(T0, T0 + 15, 15.0)], T0, T0 + 15, 10)
    assert recorded == [1.0, 1.0]  # bucket 1 is 5 s wide inside the window
    assert rate == [1.0, 1.0]


def test_apportion_rejects_non_positive_scale() -> None:
    with pytest.raises(ValueError):
        mr.apportion([], T0, T0 + 10, 0)


# -- reference ---------------------------------------------------------------


def test_percentile_interpolates_linearly() -> None:
    assert mr.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5) == 3.0
    assert mr.percentile(list(map(float, range(21))), 0.95) == 19.0
    assert mr.percentile([0.0, 10.0], 0.95) == pytest.approx(9.5)
    assert mr.percentile([7.0], 0.95) == 7.0


def test_reference_needs_twelve_half_recorded_buckets() -> None:
    rate = [float(i) for i in range(20)]
    recorded = [1.0] * 11 + [0.49] * 9
    assert mr.reference_from_series(rate, recorded) is None
    recorded[11] = 0.5  # the threshold itself qualifies
    ref = mr.reference_from_series(rate, recorded)
    assert ref == pytest.approx(mr.percentile([float(i) for i in range(12)], 0.95))


def test_reference_ignores_barely_recorded_buckets() -> None:
    # A spike in a 10%-recorded bucket must not set the scale.
    rate = [1.0] * 30 + [500.0]
    recorded = [1.0] * 30 + [0.1]
    assert mr.reference_from_series(rate, recorded) == 1.0


def test_reference_window_is_seven_days_on_the_five_minute_grid() -> None:
    start, end = mr.reference_window(T0 + 299.9)
    assert end == T0
    assert end - start == 7 * 86400


# -- reading frigate.db, both schemas ----------------------------------------


def _schema_db(path: Path, version: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / f"{version}.sql").read_text())
    return conn


def _insert(
    conn: sqlite3.Connection,
    rows: list[tuple[float, float, float | None]],
    camera: str = CAMERA,
) -> None:
    """(start, duration, motion) rows the way Frigate writes them
    (`end_time = start_time + duration`)."""
    base = conn.execute("SELECT count(*) FROM recordings").fetchone()[0]
    conn.executemany(
        "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, objects, "
        "motion, segment_size, dBFS, regions) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 1.0, NULL, 0)",
        [
            (f"r{base + i}", camera, f"/rec/{camera}/{base + i}.mp4", s, s + d, d, m)
            for i, (s, d, m) in enumerate(rows)
        ],
    )
    conn.commit()


@pytest.mark.parametrize("version", SCHEMAS)
def test_read_uses_the_covering_summary_index(version: str, tmp_path: Path) -> None:
    conn = _schema_db(tmp_path / "f.db", version)
    plan = " ".join(
        str(r[3])
        for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT start_time, duration, motion FROM recordings "
            "WHERE camera = ? AND start_time >= ? AND start_time < ? ORDER BY start_time",
            (CAMERA, 0, 1),
        )
    )
    assert "COVERING INDEX recordings_api_recordings_summary" in plan, plan
    assert "TEMP B-TREE" not in plan, plan


@pytest.mark.parametrize("version", SCHEMAS)
def test_read_rows_null_motion_and_empty_segments(version: str, tmp_path: Path) -> None:
    path = tmp_path / "f.db"
    conn = _schema_db(path, version)
    _insert(
        conn,
        [
            (T0 - 4000, 10.0, 99.0),  # older than the lookback: not read
            (T0 - 30, 10.0, 99.0),  # ends before the window
            (T0 - 5, 10.0, 20.0),  # reaches in
            (T0 + 5, 10.0, None),  # NULL motion: recorded, no motion
            (T0 + 15, 0.0, 500.0),  # zero duration: skipped
            (T0 + 16, -1.0, 500.0),  # negative duration: skipped
            (T0 + 20, 10.0, -3.0),  # negative motion: zero
        ],
    )
    _insert(conn, [(T0, 10.0, 7.0)], camera="cam_b")
    conn.close()
    ro = db.open_frigate_ro(path)
    try:
        segs = mr.read_segments(ro, CAMERA, T0, T0 + 30)
    finally:
        ro.close()
    assert segs == [
        (T0 - 5, T0 + 5, 20.0),
        (T0 + 5, T0 + 15, 0.0),
        (T0 + 20, T0 + 30, 0.0),
    ]
    rate, recorded = mr.apportion(segs, T0, T0 + 30, 10)
    assert rate == [1.0, 0.0, 0.0]
    assert recorded == [1.0, 0.5, 1.0]


def test_read_skips_null_duration(tmp_path: Path) -> None:
    """`duration` is NOT NULL in Frigate's schema; a looser table still reads."""
    conn = sqlite3.connect(tmp_path / "loose.db")
    conn.execute("CREATE TABLE recordings (camera TEXT, start_time REAL, duration REAL, motion)")
    conn.executemany(
        "INSERT INTO recordings VALUES (?, ?, ?, ?)",
        [(CAMERA, T0, None, 5), (CAMERA, T0 + 10, 10.0, 5), (CAMERA, None, 10.0, 5)],
    )
    assert mr.read_segments(conn, CAMERA, T0, T0 + 30) == [(T0 + 10, T0 + 20, 5.0)]


@pytest.mark.parametrize("version", SCHEMAS)
def test_compute_reference_over_seven_days(version: str, tmp_path: Path) -> None:
    path = tmp_path / "f.db"
    conn = _schema_db(path, version)
    now = T0 + 120
    # One hour of 10 s segments; motion 10 per segment (1/s), one busy
    # 5-minute stretch at 50 per segment (5/s).
    rows: list[tuple[float, float, float | None]] = []
    for i in range(360):
        start = T0 - 3600 + 10 * i
        rows.append((start, 10.0, 50.0 if i < 30 else 10.0))
    _insert(conn, rows)
    conn.close()
    ro = db.open_frigate_ro(path)
    try:
        ref = mr.compute_reference(ro, CAMERA, now)
        assert mr.compute_reference(ro, "cam_none", now) is None
    finally:
        ro.close()
    # 12 qualifying buckets: one at 5.0, eleven at 1.0 -> p95 interpolates.
    assert ref == pytest.approx(1.0 + 0.45 * 4.0)


def test_interrupt_after_aborts_a_long_statement(tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    mr.interrupt_after(conn, 0.0)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c"
        ).fetchone()


# -- the per-camera cache ----------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _counting(value: float | None, gate: asyncio.Event | None = None) -> tuple[
    list[int], Callable[[], Any]
]:
    calls = [0]

    async def compute() -> float | None:
        calls[0] += 1
        if gate is not None:
            await gate.wait()
        return value

    return calls, compute


async def test_cache_serves_a_fresh_value_without_recomputing() -> None:
    clock = _Clock()
    cache = mr.RateReferenceCache(clock=clock)
    calls, compute = _counting(4.2)
    assert await cache.get(CAMERA, compute, deadline_s=1.0) == 4.2
    clock.t += mr.REFERENCE_TTL_S - 1
    assert await cache.get(CAMERA, compute, deadline_s=1.0) == 4.2
    assert calls == [1]
    clock.t += 2  # past the TTL
    assert await cache.get(CAMERA, compute, deadline_s=1.0) == 4.2
    assert calls == [2]


async def test_cache_remembers_too_little_recorded() -> None:
    cache = mr.RateReferenceCache(clock=_Clock())
    calls, compute = _counting(None)
    assert await cache.get(CAMERA, compute, deadline_s=1.0) is None
    assert await cache.get(CAMERA, compute, deadline_s=1.0) is None
    assert calls == [1]


async def test_cache_is_single_flight_per_camera() -> None:
    cache = mr.RateReferenceCache(clock=_Clock())
    release = asyncio.Event()
    calls, compute = _counting(3.0, release)
    waiters = [
        asyncio.ensure_future(cache.get(CAMERA, compute, deadline_s=5.0)) for _ in range(3)
    ]
    await asyncio.sleep(0)
    assert cache.inflight_count() == 1
    release.set()
    assert await asyncio.gather(*waiters) == [3.0, 3.0, 3.0]
    assert calls == [1]
    assert cache.inflight_count() == 0


async def test_cache_deadline_miss_omits_then_lands_for_the_next_call() -> None:
    cache = mr.RateReferenceCache(clock=_Clock())
    release = asyncio.Event()
    calls, compute = _counting(2.5, release)
    assert await cache.get(CAMERA, compute, deadline_s=0.0) is None  # not ready
    assert cache.inflight_count() == 1  # not cancelled
    release.set()
    while cache.inflight_count():
        await asyncio.sleep(0)
    assert await cache.get(CAMERA, compute, deadline_s=0.0) == 2.5
    assert calls == [1]


async def test_cache_serves_the_stale_value_while_refreshing() -> None:
    clock = _Clock()
    cache = mr.RateReferenceCache(clock=clock)
    _, first = _counting(1.0)
    assert await cache.get(CAMERA, first, deadline_s=1.0) == 1.0
    clock.t += mr.REFERENCE_TTL_S + 1
    release = asyncio.Event()
    _, slow = _counting(2.0, release)
    assert await cache.get(CAMERA, slow, deadline_s=0.0) == 1.0  # stale, still useful
    release.set()
    while cache.inflight_count():
        await asyncio.sleep(0)
    assert await cache.get(CAMERA, slow, deadline_s=0.0) == 2.0
    clock.t += mr.REFERENCE_STALE_TTL_S + 1
    never = asyncio.Event()
    _, stuck = _counting(3.0, never)
    assert await cache.get(CAMERA, stuck, deadline_s=0.0) is None  # too old to serve
    never.set()


async def test_cache_backs_off_after_a_failure() -> None:
    clock = _Clock()
    cache = mr.RateReferenceCache(clock=clock)
    calls = [0]

    async def broken() -> float | None:
        calls[0] += 1
        raise sqlite3.OperationalError("database is locked")

    assert await cache.get(CAMERA, broken, deadline_s=1.0) is None
    assert await cache.get(CAMERA, broken, deadline_s=1.0) is None
    assert calls == [1]  # no retry inside the back-off
    clock.t += mr.REFERENCE_RETRY_S + 1
    _, fixed = _counting(6.0)
    assert await cache.get(CAMERA, fixed, deadline_s=1.0) == 6.0


# -- the route ---------------------------------------------------------------


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    path = tmp_path / "frigate.db"
    conn = _schema_db(path, "v0.18.2")
    # 10 s segments over the 2 h before T0, 1 motion per second; the last
    # 40 s of the window have no recordings.
    _insert(conn, [(T0 - 7200 + 10 * i, 10.0, 10.0) for i in range(716)])
    conn.close()
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text(f"cameras:\n  {CAMERA}: {{}}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=path
        ),
        sidecar=SidecarSection(
            db_path=tmp_path / "marcellus.db", bind_port=5001, require_frigate_auth=False
        ),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )

    async def _fixed_motion(
        request: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [100.0] + [0.0] * (mr.bucket_count(start, end, scale) - 1), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fixed_motion)
    monkeypatch.setattr(scrub_routes.time, "time", lambda: T0)
    app = create_app(settings)
    return TestClient(app), app


def _get(client: TestClient) -> Any:
    r = client.get(
        f"/v1/motion/{CAMERA}", params={"start": T0 - 300, "end": T0, "scale": 60}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_route_adds_the_absolute_series(app_client: tuple[TestClient, Any]) -> None:
    client, _ = app_client
    body = _get(client)
    assert body["values"] == [100.0, 0.0, 0.0, 0.0, 0.0]  # untouched
    assert body["start"] == T0 - 300 and body["interval"] == 60
    assert body["rate"] == [1.0, 1.0, 1.0, 1.0, 1.0]
    assert body["recorded"] == [1.0, 1.0, 1.0, 1.0, pytest.approx(0.333)]
    assert body["rate_reference"] == 1.0
    assert body["rate_unit"] == "motion_per_recorded_second"
    assert "motion_unavailable" not in body


def test_route_without_enough_history_omits_only_the_reference(
    app_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client
    monkeypatch.setattr(mr, "REFERENCE_MIN_BUCKETS", 10_000)
    body = _get(client)
    assert "rate" in body and "recorded" in body and "rate_unit" in body
    assert "rate_reference" not in body


def test_route_falls_back_to_the_old_shape_when_the_db_read_fails(
    app_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client

    def _broken(*args: object) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(scrub_routes, "_read_motion_rate", _broken)
    body = _get(client)
    assert set(body) == {"start", "interval", "values"}


def test_route_falls_back_when_the_frigate_db_is_missing(
    app_client: tuple[TestClient, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = app_client
    _get(client)  # warms the known-camera cache, then the file goes away
    (tmp_path / "frigate.db").unlink()
    app.state.motion_reference_cache = None
    body = _get(client)
    assert set(body) == {"start", "interval", "values"}


async def test_deadline_miss_answers_without_waiting_and_holds_the_slot(
    app_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven on this test's own loop: TestClient's per-request loop waits
    for worker threads at shutdown, which would hide the early answer."""
    _, app = app_client
    started = threading.Event()
    release = threading.Event()
    real = scrub_routes._read_motion_rate

    def _slow(*args: Any) -> Any:
        started.set()
        release.wait(10)
        return real(*args)

    monkeypatch.setattr(scrub_routes, "_read_motion_rate", _slow)
    monkeypatch.setattr(scrub_routes, "_MOTION_RATE_DEADLINE_S", 0.0)
    request: Any = SimpleNamespace(app=app)
    gate = scrub_routes._motion_rate_gate(app.state)
    try:
        assert await scrub_routes._motion_absolute(request, CAMERA, T0 - 300, T0, 60) is None
        assert started.wait(5)
        # The read still runs, and still holds its permit until it is done.
        assert gate.free == scrub_routes._MOTION_RATE_CONCURRENCY - 1
    finally:
        release.set()
    while gate.free < scrub_routes._MOTION_RATE_CONCURRENCY:
        await asyncio.sleep(0)
    cache = scrub_routes._motion_reference_cache(app.state)
    while cache.inflight_count():
        await asyncio.sleep(0)


async def test_gated_read_times_out_while_queued() -> None:
    gate = scrub_routes._Gate(1)
    assert await gate.acquire(0)  # the only permit is taken
    with pytest.raises(asyncio.TimeoutError):
        await scrub_routes._gated_read(gate, lambda: 1, timeout_s=0.0)
    gate.release()
    assert await scrub_routes._gated_read(gate, lambda: 7, timeout_s=1.0) == 7
    while gate.free < 1:
        await asyncio.sleep(0)


def test_route_keeps_the_series_when_frigate_is_down(
    app_client: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = app_client

    async def _down(
        request: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * mr.bucket_count(start, end, scale), True

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _down)
    body = _get(client)
    assert body["motion_unavailable"] is True
    assert body["rate"][0] == 1.0  # read from frigate.db, not Frigate's API


def test_motion_gate_is_not_the_highlights_gate(app_client: tuple[TestClient, Any]) -> None:
    client, app = app_client
    _get(client)
    state = app.state
    gates = {
        id(scrub_routes._highlights_gate(state)),
        id(scrub_routes._motion_rate_gate(state)),
        id(scrub_routes._motion_reference_gate(state)),
    }
    assert len(gates) == 3
    assert scrub_routes._motion_rate_gate(state).free == scrub_routes._MOTION_RATE_CONCURRENCY


def test_capabilities_advertise_the_rate(app_client: tuple[TestClient, Any]) -> None:
    client, _ = app_client
    body = client.get("/v1/capabilities").json()
    assert body["motion"] == {"rate": True}
