"""Recording segment-clock correction (`analysis/segment_clock.py`,
`analysis/clock_offset.ClockShift`).

Synthetic runs model what Frigate does: the file for segment n is opened at
wall time `true_start + jitter` and named after that second, its footage
lasts `duration`, and the next segment's footage starts exactly where this
one's ended. The true sub-second error is `true_start - name`.
"""

from __future__ import annotations

import bisect
import json
import math
import random
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import auth, db
from marcellus.analysis import clock_offset, segment_clock
from marcellus.analysis.segment_clock import FractionEstimator, SegmentClock, estimate
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.server import create_app

T0 = 1_800_000_000.0


def synth(
    n: int,
    *,
    t0: float = T0,
    f0: float = 0.3,
    span: float = 10.004,
    span_noise: float = 0.03,
    jitter: float = 0.05,
    seed: int = 1,
) -> tuple[list[tuple[float, float]], list[float]]:
    """`(start, duration)` rows as Frigate stores them, and the true `f`."""
    rng = random.Random(seed)
    true_start = t0 + f0
    rows: list[tuple[float, float]] = []
    truth: list[float] = []
    for _ in range(n):
        name = math.floor(true_start + rng.uniform(-jitter, jitter))
        dur = span + rng.uniform(-span_noise, span_noise)
        rows.append((float(name), dur))
        truth.append(true_start - name)
        true_start += dur
    return rows, truth


# -- the estimator -------------------------------------------------------------


def test_estimator_pins_the_true_fraction_on_a_drifting_run() -> None:
    rows, truth = synth(3000)
    res = estimate(rows)
    errors = [abs(r[0] - f) for r, f in zip(res[400:], truth[400:], strict=True) if r is not None]
    assert len(errors) == 2600
    errors.sort()
    assert errors[len(errors) // 2] < 0.04
    assert errors[int(len(errors) * 0.95)] < 0.1


def test_estimator_without_rollovers_stays_at_the_midpoint() -> None:
    """A clock that does not drift never rolls a name over: nothing pins `f`,
    so the estimate is the middle of the unit interval -- a zero correction."""
    rows, _truth = synth(800, span=10.0, span_noise=0.0, jitter=0.0, f0=0.37)
    res = estimate(rows)
    for r in res:
        assert r is not None
        assert r[0] == pytest.approx(0.5, abs=1e-6)
        assert r[1] == pytest.approx(1.0, abs=1e-6)


def test_single_segment_and_run_start_are_unpinned() -> None:
    assert estimate([(T0, 10.0)]) == [(0.5, 1.0)]


def test_gap_breaks_the_run() -> None:
    rows, truth = synth(1500)
    # A 90 s outage, then the camera comes back with a new sub-second phase.
    after, truth_after = synth(1500, t0=rows[-1][0] + 100.0, f0=0.8, seed=2)
    res = estimate(rows + after)
    first_after = res[len(rows)]
    assert first_after == (0.5, 1.0)
    pairs = zip(res[len(rows) + 400 :], truth_after[400:], strict=True)
    tail = [abs(r[0] - f) for r, f in pairs if r]
    assert sorted(tail)[int(len(tail) * 0.95)] < 0.1


def test_restart_jump_backwards_breaks_the_run() -> None:
    rows, _ = synth(50)
    est = FractionEstimator()
    for s, d in rows:
        est.push(s, d)
    # A name 30 s before the previous segment's end: not contiguous.
    assert est.push(rows[-1][0] - 20.0, 10.0) == (0.5, 1.0)


def test_non_whole_second_start_disables_the_estimate() -> None:
    rows, _ = synth(10)
    res = estimate(rows[:5] + [(rows[5][0] + 0.25, rows[5][1])] + rows[6:])
    assert res[5] is None
    assert res[6] == (0.5, 1.0)  # the run restarts after it


def test_bad_duration_disables_the_estimate() -> None:
    assert estimate([(T0, float("nan")), (T0 + 10, 0.0)]) == [None, None]


def test_estimates_are_causal() -> None:
    """Appending segments never changes an earlier estimate: the reel body is
    ETagged on content and must not churn as the live edge advances."""
    rows, _ = synth(2000)
    assert estimate(rows[:1200]) == estimate(rows)[:1200]


# -- the cache -------------------------------------------------------------------


class FakeSource:
    """`rows`/`newest` over a list, with a movable 'now' cutoff and a log of
    every read."""

    def __init__(self, rows: list[tuple[float, float]]) -> None:
        self.all = rows
        self.cutoff = math.inf
        self.reads: list[tuple[float, float | None]] = []

    def visible(self) -> list[tuple[float, float]]:
        return [r for r in self.all if r[0] <= self.cutoff]

    def rows(self, camera: str, since: float, until: float | None) -> list[tuple[float, float]]:
        self.reads.append((since, until))
        return [
            r for r in self.visible() if r[0] >= since and (until is None or r[0] < until)
        ]

    def newest(self, camera: str) -> float | None:
        vis = self.visible()
        return vis[-1][0] if vis else None


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _clock(
    rows: list[tuple[float, float]], **kw: object
) -> tuple[SegmentClock, FakeSource, FakeClock]:
    src = FakeSource(rows)
    clk = FakeClock()
    return SegmentClock(src.rows, src.newest, clock=clk, **kw), src, clk  # type: ignore[arg-type]


def test_cache_lookup_matches_the_pure_estimator() -> None:
    rows, _ = synth(3000)
    expected = estimate(rows)
    sc, _src, _clk = _clock(rows)
    for i in range(0, 3000, 37):
        start, dur = rows[i]
        assert sc.fraction("cam-a", start + dur / 2) == expected[i][0]  # type: ignore[index]


def test_cache_is_bounded() -> None:
    rows, _ = synth(3000)  # ~8.3 h
    sc, _src, _clk = _clock(rows, max_blocks=3)
    for i in range(0, 3000, 50):
        sc.fraction("cam-a", rows[i][0] + 1.0)
        assert len(sc) <= 3


def test_live_edge_grows_incrementally_and_agrees_with_a_cold_load() -> None:
    rows, _ = synth(1500)
    sc, src, clk = _clock(rows)
    src.cutoff = rows[999][0]
    newest_start, newest_dur = rows[999]
    f_newest = sc.fraction("cam-a", newest_start + 1.0)
    assert f_newest is not None
    # Past the newest segment: its f stands in for up to LIVE_EDGE_S...
    assert sc.fraction("cam-a", newest_start + newest_dur + 30.0) == f_newest
    # ...and not beyond.
    assert sc.fraction("cam-a", newest_start + newest_dur + 90.0) is None

    # New segments arrive; within REFRESH_S nothing is re-read.
    src.cutoff = rows[1400][0]
    reads = len(src.reads)
    sc.fraction("cam-a", rows[1200][0] + 1.0)
    assert len(src.reads) == reads
    clk.t += segment_clock.REFRESH_S + 0.1
    got = [sc.fraction("cam-a", rows[i][0] + 1.0) for i in range(1000, 1401)]
    # The refresh read only rows newer than the last one seen.
    tail_reads = [r for r in src.reads[reads:] if r[1] is None]
    assert tail_reads and all(since > newest_start for since, _ in tail_reads)

    cold, _s, _c = _clock(rows)
    assert got == [cold.fraction("cam-a", rows[i][0] + 1.0) for i in range(1000, 1401)]


def test_gap_and_no_rows_give_no_fraction() -> None:
    rows, _ = synth(400)
    later, _ = synth(400, t0=rows[-1][0] + 600.0, seed=3)
    sc, _src, _clk = _clock(rows + later)
    last_start, last_dur = rows[-1]
    assert sc.fraction("cam-a", last_start + last_dur + 1.0) is not None  # rollover hole
    assert sc.fraction("cam-a", last_start + last_dur + 300.0) is None  # outage
    empty, _s, _c = _clock([])
    assert empty.fraction("cam-a", T0) is None


# -- the shift -----------------------------------------------------------------


def test_shift_lands_events_on_the_picture() -> None:
    """Ground truth: an event seen at true time T is stamped `T - latency` on
    the detect clock and appears in the recording at nominal `T - f`."""
    rows, truth = synth(3000)
    sc, _src, _clk = _clock(rows)
    latency = 1.7
    shift = clock_offset.ClockShift(camera="cam-a", constant_s=latency - 0.5, clock=sc)
    plain = clock_offset.ClockShift(camera="cam-a", constant_s=latency - 0.5)
    rng = random.Random(9)
    err_new, err_old = [], []
    for _ in range(300):
        i = rng.randrange(400, 2999)
        true_t = rows[i][0] + truth[i] + rng.uniform(1.0, rows[i][1] - 2.5)
        nominal = true_t - truth[i]
        detect_t = true_t - latency
        err_new.append(abs(shift.shift(detect_t) - nominal))
        err_old.append(abs(plain.shift(detect_t) - nominal))
    err_new.sort()
    err_old.sort()
    assert err_new[int(len(err_new) * 0.95)] < 0.1
    assert err_old[int(len(err_old) * 0.95)] > 0.3  # the sawtooth a constant leaves


def test_shift_without_clock_or_rows_is_the_constant() -> None:
    off = clock_offset.ClockShift(camera="cam-a", constant_s=-1.25)
    assert off.max_dev_s == 0.0
    assert off.at(T0) == -1.25
    sc, _s, _c = _clock([])
    on = clock_offset.ClockShift(camera="cam-a", constant_s=-1.25, clock=sc)
    assert on.at(T0) == -1.25
    assert on.shift(T0) == T0 - 1.25


# -- the routes: off = old behaviour, on = corrected, scrub == encounters ------


RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, path TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
    segment_size REAL NOT NULL DEFAULT 0
);
CREATE TABLE event (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, label TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL, score REAL, top_score REAL,
    zones TEXT, data TEXT
);
CREATE TABLE reviewsegment (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, start_time REAL NOT NULL,
    end_time REAL, severity TEXT NOT NULL, thumb_path TEXT, data TEXT
);
"""


@pytest.fixture
def sawtooth_db(tmp_path: Path) -> tuple[Path, list[tuple[float, float]], float]:
    """Two hours of drifting whole-second segments ending ~10 s ago, three
    events and a review segment on top."""
    n = 720
    now = time.time()
    t0 = math.floor(now - 10.0 - n * 10.004)
    rows, _ = synth(n, t0=t0)
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
    for i, (s, d) in enumerate(rows):
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration) "
            "VALUES (?, 'cam-a', ?, ?, ?, ?)",
            (f"r{i}", f"/media/frigate/recordings/{i}.mp4", s, s + d, d),
        )
    path = [[[0.1 + 0.01 * k, 0.5], rows[650][0] + 0.3 * k] for k in range(40)]
    for i, k in enumerate((500, 600, 650)):
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, "
            "zones, data) VALUES (?, 'cam-a', 'person', ?, ?, 0.9, 0.9, '[]', ?)",
            (f"e{i}", rows[k][0] + 3.3, rows[k][0] + 15.1, json.dumps({"path_data": path})),
        )
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES ('rv1', 'cam-a', ?, ?, 'alert', ?)",
        (rows[600][0] + 2.2, rows[600][0] + 30.0, json.dumps({"objects": ["person"]})),
    )
    conn.commit()
    conn.close()
    return p, rows, now


def _settings(
    tmp_path: Path, frigate_db: Path, *, correction: bool, auth_off: bool = False
) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras:\n  cam-a:\n    detect:\n      annotation_offset: -1500\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db
        ),
        sidecar=SidecarSection(
            db_path=tmp_path / "marcellus.db",
            bind_port=5001,
            require_frigate_auth=not auth_off,
        ),
        scrub=ScrubSection(
            enabled=False,
            cache_dir=tmp_path / "scrub",
            segment_clock_correction=correction,
        ),
    )


def _reel(
    tmp_path: Path,
    frigate_db: Path,
    now: float,
    monkeypatch: pytest.MonkeyPatch,
    *,
    correction: bool,
) -> dict:
    from marcellus.routes import scrub as scrub_routes

    async def _noop(app: object, cookie: str, *, ttl_s: float | None = None) -> None:
        return None

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(auth, "validate_frigate_session", _noop)
    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)
    clock_offset.invalidate()
    segment_clock.reset()
    client = TestClient(create_app(_settings(tmp_path, frigate_db, correction=correction)))
    r = client.get(
        "/v1/reel/cam-a",
        params={"start": now - 3600, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    return r.json()


def test_reel_with_correction_off_is_the_constant_shift(
    sawtooth_db: tuple[Path, list[tuple[float, float]], float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frigate_db, _rows, now = sawtooth_db
    body = _reel(tmp_path, frigate_db, now, monkeypatch, correction=False)
    conn = sqlite3.connect(frigate_db)
    raw = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, start_time, end_time FROM event")}
    rv = conn.execute("SELECT start_time, end_time FROM reviewsegment").fetchone()
    conn.close()
    events = {e["id"]: e for e in body["events"]}
    for eid, (s, e) in raw.items():
        assert events[eid]["start"] == s + -1.5  # exact, the old arithmetic
        assert events[eid]["end"] == e + -1.5
    assert body["reviews"][0]["start"] == rv[0] + -1.5
    assert body["reviews"][0]["end"] == rv[1] + -1.5


def test_reel_with_correction_on_moves_times_by_the_segment_term(
    sawtooth_db: tuple[Path, list[tuple[float, float]], float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frigate_db, rows, now = sawtooth_db
    off = _reel(tmp_path, frigate_db, now, monkeypatch, correction=False)
    on = _reel(tmp_path, frigate_db, now, monkeypatch, correction=True)
    expected = estimate(rows)
    by_id_off = {e["id"]: e for e in off["events"]}
    moved = 0
    for i, ev in enumerate(sorted(on["events"], key=lambda e: e["id"])):
        k = (500, 600, 650)[i]
        f = expected[k][0]  # type: ignore[index]
        term = round(0.5 - f, 3)
        assert ev["start"] == pytest.approx(by_id_off[ev["id"]]["start"] + term, abs=1e-6)
        moved += abs(term) > 0.01
        # Path points are shifted by the term of the segment each one is in.
        t_off = by_id_off[ev["id"]]["path"]["drift"][0][0]
        j = bisect.bisect_right([r[0] for r in rows], t_off) - 1
        path_term = round(0.5 - expected[j][0], 3)  # type: ignore[index]
        assert ev["path"]["drift"][0][0] == pytest.approx(t_off + path_term, abs=2e-3)
    assert moved  # the fixture really exercises the correction
    # And the body is stable poll to poll (ETag).
    again = _reel(tmp_path, frigate_db, now, monkeypatch, correction=True)
    assert again["events"] == on["events"] and again["reviews"] == on["reviews"]


def test_scrub_and_encounters_shift_reviews_identically(
    sawtooth_db: tuple[Path, list[tuple[float, float]], float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marcellus.encounters.service import EncounterService

    frigate_db, _rows, now = sawtooth_db
    body = _reel(tmp_path, frigate_db, now, monkeypatch, correction=True)
    settings = _settings(tmp_path, frigate_db, correction=True)
    svc = EncounterService.__new__(EncounterService)
    svc.settings = settings
    conn = db.open_frigate_ro(frigate_db)
    try:
        row = conn.execute("SELECT * FROM reviewsegment WHERE id = 'rv1'").fetchone()
    finally:
        conn.close()
    atom = svc._atom_from_review_row(row)
    assert atom.start_time == body["reviews"][0]["start"]
    assert atom.end_time == body["reviews"][0]["end"]


def test_segment_clock_diagnostics_route(
    sawtooth_db: tuple[Path, list[tuple[float, float]], float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frigate_db, rows, _now = sawtooth_db
    settings = _settings(tmp_path, frigate_db, correction=True, auth_off=True)
    client = TestClient(create_app(settings))
    r = client.get(
        "/analysis/segment-clock", params={"camera": "cam-a", "ts": rows[600][0] + 3.0}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["constant_s"] == -1.5
    assert body["segments"] and all("f" in s and "width" in s for s in body["segments"])


def test_calibrator_frame_adds_the_segment_term(
    sawtooth_db: tuple[Path, list[tuple[float, float]], float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marcellus import frigate_api

    frigate_db, rows, _now = sawtooth_db
    calls: list[float] = []

    def _fake_snapshot(
        self: object, camera: str, ts: float, *, timeout: float = 15.0
    ) -> tuple[bytes | None, int]:
        calls.append(ts)
        return (b"\xff\xd8jpeg", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "recording_snapshot", _fake_snapshot)
    settings = _settings(tmp_path, frigate_db, correction=True, auth_off=True)
    client = TestClient(create_app(settings))
    ts = rows[600][0] + 4.0
    term = clock_offset.segment_term_s(settings, "cam-a", ts)
    assert term != 0.0
    assert client.get(
        "/analysis/annotation-offset/frame/cam-a", params={"ts": ts}
    ).status_code == 200
    assert client.get(
        "/analysis/annotation-offset/frame/cam-a", params={"ts": ts, "segment_clock": 1}
    ).status_code == 200
    assert calls[0] == pytest.approx(ts)
    assert calls[1] == pytest.approx(ts + term)


def test_measuring_tool_requests_corrected_frames() -> None:
    """With a segment term the sweep asks for `t + offset + term(t + offset)`,
    so the offset it reports is the constant the corrected shift wants."""
    pytest.importorskip("cv2")
    import numpy as np

    from marcellus.analysis import annotation_offset as ao

    urls: list[str] = []

    class _Resp:
        status_code = 404
        content = b""

    class _Client:
        def get(self, url: str) -> _Resp:
            urls.append(url)
            return _Resp()

    template = np.zeros((40, 40, 3), dtype=np.uint8)
    monkey = {"calls": 0}

    def _fetch(client: object, url: str, np_mod: object, cv2_mod: object) -> object:
        urls.append(url)
        monkey["calls"] += 1
        return template if monkey["calls"] <= 2 else None

    path = [(0.5, 0.5, T0 + k) for k in range(8)]
    orig = ao._fetch_image
    ao._fetch_image = _fetch  # type: ignore[assignment]
    try:
        import cv2

        ao._measure_event(
            _Client(), cv2, np, "cam-a", "ev", path, None, 2, 500, 500, 0.4,
            segment_term=lambda cam, t: 0.25,
        )
    finally:
        ao._fetch_image = orig  # type: ignore[assignment]
    sweep = [u for u in urls if "/recordings/" in u][1:]
    stamps = sorted(float(u.split("/recordings/")[1].split("/")[0]) for u in sweep)
    assert stamps[0] == pytest.approx(T0 - 0.5 + 0.25, abs=1e-3)
