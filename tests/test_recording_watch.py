"""The camera-stopped-recording watcher (`marcellus.recording_watch`).

Layers, pure first: `evaluate()` transitions and hysteresis, `watched_cameras`,
then the worker against a real sidecar DB and a recording `LogTransport`:
one card per incident, quiet growth, resolve only on full recovery, restart
adoption, push off, query failure, and /healthz.
"""

from __future__ import annotations

import asyncio
import datetime
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FrigateSection,
    PushSection,
    RecordingWatchSection,
    Settings,
    SidecarSection,
)
from marcellus.push import card_store, policy_settings
from marcellus.push import store as push_store
from marcellus.push.transport import LogTransport
from marcellus.recording_watch import (
    CARD_KEY,
    RECOVERED,
    STALLED,
    WITHDRAWN,
    RecordingWatcher,
    WatchState,
    evaluate,
    resolved_copy,
    stalled_copy,
    watched_cameras,
)
from marcellus.server import create_app

CFG = RecordingWatchSection()  # stall 300, recover 60
NOW = 1_800_000_000.0


def run(state: WatchState, newest: dict[str, float | None], cams: list[str], now: float):
    return evaluate(state, newest, cams, now, CFG)


# --- evaluate() --------------------------------------------------------------


def test_healthy_cameras_do_not_stall() -> None:
    state, tr = run(WatchState(), {"a": NOW - 8, "b": NOW - 14}, ["a", "b"], NOW)
    assert tr == [] and state.stalled == {}


def test_stalls_after_threshold_with_since_from_newest_segment() -> None:
    state, tr = run(WatchState(), {"a": NOW - 301}, ["a"], NOW)
    assert [(t.camera, t.kind, t.since) for t in tr] == [("a", STALLED, NOW - 301)]
    assert state.stalled == {"a": NOW - 301}
    # exactly at the threshold is not yet stalled
    _, tr = run(WatchState(), {"a": NOW - 300}, ["a"], NOW)
    assert tr == []


def test_no_rows_while_enrolled_is_stalled_from_now() -> None:
    state, tr = run(WatchState(), {"a": None}, ["a"], NOW)
    assert tr[0].kind == STALLED and state.stalled == {"a": NOW}
    state, tr = run(WatchState(), {}, ["a"], NOW)  # absent key == no rows
    assert state.stalled == {"a": NOW}


def test_hysteresis_no_flapping() -> None:
    state, _ = run(WatchState(), {"a": NOW - 400}, ["a"], NOW)
    since = state.stalled["a"]
    # A segment 120 s old: fresher than stall_after_s, not fresher than recover_within_s.
    for n in range(5):
        state, tr = run(state, {"a": NOW + n * 30 - 120}, ["a"], NOW + n * 30)
        assert tr == [] and state.stalled == {"a": since}
    state, tr = run(state, {"a": NOW + 200 - 10}, ["a"], NOW + 200)
    assert [(t.kind, t.since) for t in tr] == [(RECOVERED, since)]
    assert state.stalled == {}
    # And once recovered, hovering at 100 s old does not stall it again.
    state, tr = run(state, {"a": NOW + 300 - 100}, ["a"], NOW + 300)
    assert tr == []


def test_stale_data_never_recovers_a_camera_without_rows() -> None:
    state, _ = run(WatchState(), {"a": None}, ["a"], NOW)
    state, tr = run(state, {"a": None}, ["a"], NOW + 30)
    assert tr == [] and "a" in state.stalled


def test_unwatched_camera_is_ignored_and_withdrawn_when_stalled() -> None:
    state, tr = run(WatchState(), {"a": NOW - 9, "off": NOW - 9999}, ["a"], NOW)
    assert tr == [] and state.stalled == {}  # `off` isn't watched: never evaluated
    state, _ = run(WatchState(), {"a": NOW - 999}, ["a"], NOW)
    state, tr = run(state, {"a": NOW - 999}, [], NOW + 30)  # recording switched off
    assert [(t.camera, t.kind) for t in tr] == [("a", WITHDRAWN)]
    assert state.stalled == {}


def test_camera_joining_after_startup_gets_a_grace_period() -> None:
    state, _ = run(WatchState(), {"a": NOW - 5}, ["a"], NOW)
    # recording just re-enabled for b; its newest segment is hours old
    state, tr = run(state, {"a": NOW - 5, "b": NOW - 9000}, ["a", "b"], NOW + 30)
    assert tr == []
    state, tr = run(state, {"a": NOW + 195, "b": NOW - 9000}, ["a", "b"], NOW + 200)
    assert tr == []
    state, tr = run(state, {"a": NOW + 335, "b": NOW - 9000}, ["a", "b"], NOW + 340)
    assert [(t.camera, t.kind) for t in tr] == [("b", STALLED)]


def test_cameras_at_first_evaluation_are_not_graced() -> None:
    _, tr = run(WatchState(), {"a": NOW - 9000}, ["a"], NOW)
    assert [t.kind for t in tr] == [STALLED]


# --- watched_cameras() ----------------------------------------------------------


def test_watched_requires_enabled_and_record() -> None:
    cfg = {
        "record": {"enabled": True, "continuous": {"days": 7}},
        "cameras": {
            "a": {},
            "b": {"enabled": False},
            "c": {"record": {"enabled": False}},
            "d": None,
        },
    }
    assert watched_cameras(cfg, None) == ["a", "d"]
    # Frigate's own default for record.enabled is off.
    assert watched_cameras({"cameras": {"a": {}}}, None) == []


@pytest.mark.parametrize(
    "record,watched",
    [
        ({"enabled": True, "continuous": {"days": 7}}, True),
        ({"enabled": True, "continuous": {"days": 0}, "motion": {"days": 30}}, False),
        ({"enabled": True, "motion": {"days": 30}}, False),
        ({"enabled": True, "retain": {"days": 3, "mode": "all"}}, True),
        ({"enabled": True, "retain": {"days": 3}}, True),
        ({"enabled": True, "retain": {"days": 3, "mode": "motion"}}, False),
        ({"enabled": True, "retain": {"days": 0}}, False),
    ],
)
def test_only_continuous_recording_is_watched(record, watched) -> None:
    """A motion-only camera has long, normal gaps; its age means nothing."""
    expected = ["a"] if watched else []
    assert watched_cameras({"record": record, "cameras": {"a": {}}}, None) == expected


def test_runtime_disable_wins_over_file() -> None:
    cfg = {"record": {"enabled": True, "continuous": {"days": 7}}, "cameras": {"a": {}, "b": {}}}
    runtime = {"a": (True, False), "b": (True, True)}
    assert watched_cameras(cfg, runtime) == ["b"]
    # runtime-enabled when the file said off: runtime is the truth
    assert watched_cameras({"cameras": {"a": {}}}, {"a": (True, True)}) == ["a"]
    # a camera runtime doesn't mention falls back to the file
    assert watched_cameras(cfg, {"b": (False, True)}) == ["a"]
    assert watched_cameras({}, None) == []


# --- copy ----------------------------------------------------------------------


def test_copy_plain_words() -> None:
    title, body = stalled_copy({"gate-face": NOW - 3600}, 10, NOW)
    assert title == "gate-face isn't recording" and "1 h ago" in body
    title, body = stalled_copy({"a": NOW - 600, "b": NOW - 60}, 10, NOW)
    assert title == "Cameras not recording" and "a, b" in body and "Every camera" not in body
    _, body = stalled_copy({"a": NOW - 600, "b": NOW - 60}, 2, NOW)
    assert "Every camera is affected" in body
    _, body = stalled_copy({c: NOW - 600 for c in "abcde"}, 10, NOW)
    assert "and 2 more" in body
    title, body = resolved_copy(["a", "b"], 18 * 3600 + 300)
    assert title == "Recording resumed" and "18 h 5 min" in body


def test_times_are_in_the_devices_zone_never_the_hosts() -> None:
    from zoneinfo import ZoneInfo

    stopped = datetime.datetime(2026, 10, 30, 19, 43, tzinfo=ZoneInfo("America/Los_Angeles"))
    ts, later = stopped.timestamp(), stopped.timestamp() + 3600
    _, body = stalled_copy({"a": ts}, 10, later, ZoneInfo("America/Los_Angeles"))
    assert "7:43 PM" in body
    _, body = stalled_copy({"a": ts}, 10, later, ZoneInfo("Asia/Tokyo"))
    assert "11:43 AM" in body
    # No device names a zone: how long ago, not a clock that may be wrong.
    _, body = stalled_copy({"a": ts}, 10, later, None)
    assert "ago" in body and "PM" not in body and "AM" not in body


# --- the worker ------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Harness:
    """A watcher over a real sidecar DB with one registered device and a
    recording transport; `newest` is the fake Frigate recordings table."""

    def __init__(
        self, tmp_path: Path, *, push_enabled: bool = True, notify: bool = True,
        cameras: tuple[str, ...] = ("a", "b", "c"),
    ) -> None:
        self.db_path = tmp_path / "sidecar.db"
        self.clock = Clock()
        self.transport = LogTransport()
        self.newest: dict[str, float | None] = {c: NOW - 5 for c in cameras}
        self.cameras = list(cameras)
        self.fail = False
        self.settings = Settings(
            frigate=FrigateSection(config_path=tmp_path / "none.yml", db_path=tmp_path / "f.db"),
            sidecar=SidecarSection(db_path=self.db_path),
            push=PushSection(enabled=push_enabled),
            recording_watch=RecordingWatchSection(notify=notify),
        )
        conn = db.open_sidecar(self.db_path)
        push_store.upsert_device(
            conn, apns_token="tok1", bundle_id="x.y", environment="sandbox",
            cameras=["street"], labels=["person"], min_severity="alert",
        )
        conn.close()
        engine = SimpleNamespace(db_path=str(self.db_path), transport=self.transport)
        self.watcher = self.make_watcher(engine)

    def make_watcher(self, engine: Any) -> RecordingWatcher:
        async def runtime() -> None:
            return None

        def newest_fn(_path: Path, cams: list[str]) -> dict[str, float | None]:
            if self.fail:
                raise sqlite3.OperationalError("database is locked")
            return {c: self.newest.get(c) for c in cams}

        w = RecordingWatcher(
            self.settings, engine_getter=lambda: engine, clock=self.clock,
            newest_fn=newest_fn, runtime_fn=runtime,
        )

        async def watched() -> list[str]:
            return list(self.cameras)

        w._watched = watched  # type: ignore[method-assign]
        return w

    def tick(self, advance: float = 30.0) -> list[Any]:
        self.clock.now += advance
        for cam in self.newest:
            if cam not in self.stopped:
                self.newest[cam] = self.clock.now - 6
        return asyncio.run(self.watcher.tick())

    stopped: set[str]

    def stop(self, *cams: str) -> None:
        self.stopped = getattr(self, "stopped", set()) | set(cams)

    def start(self, *cams: str) -> None:
        self.stopped = getattr(self, "stopped", set()) - set(cams)

    @property
    def card(self):
        conn = db.open_sidecar(self.db_path)
        try:
            return card_store.get_card(conn, CARD_KEY)
        finally:
            conn.close()

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [s["payload"] for s in self.transport.sent]


@pytest.fixture(autouse=True)
def _loud_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    """No quiet hours unless a test sets them (the default policy's window
    would make sound depend on the wall clock)."""
    monkeypatch.setattr(policy_settings, "get_active", lambda: {})


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.stopped = set()
    return h


def test_gate_face_case_stats_healthy_segments_old_is_stalled(h: Harness) -> None:
    """Frigate says everything is fine (we never even look at /api/stats);
    only the segment age exposes it."""
    h.stop("a")
    h.tick(60)  # first look: all fine, a's newest is 66 s old
    assert h.watcher.health()["status"] == "ok"
    tr = h.tick(400)
    assert [(t.camera, t.kind) for t in tr] == [("a", STALLED)]
    health = h.watcher.health()
    assert health["status"] == "stalled"
    assert [s["camera"] for s in health["stalled"]] == ["a"]
    assert health["last_check_age_s"] == 0.0


def test_one_card_per_incident_loud_once_then_quiet_growth(h: Harness) -> None:
    h.tick(30)
    assert h.payloads == []
    h.stop("a")
    h.tick(400)
    assert len(h.payloads) == 1
    first = h.payloads[0]
    assert first["card_key"] == CARD_KEY and first["mutation"] == "create"
    assert first["aps"]["alert"]["title"] == "a isn't recording"
    assert first["aps"].get("sound")
    assert first["aps"]["interruption-level"] == "active"
    assert h.card is not None and h.card.kept_row

    h.tick(30)  # nothing changed: no new push
    assert len(h.payloads) == 1

    h.stop("b", "c")  # the set grows
    h.tick(400)
    assert len(h.payloads) == 2
    grown = h.payloads[1]
    assert grown["card_key"] == CARD_KEY and grown["mutation"] == "enrich"
    assert "sound" not in grown["aps"]
    assert grown["aps"]["alert"]["title"] == "Cameras not recording"
    assert "a, b, c" in grown["aps"]["alert"]["body"]
    # An unchanged set stays quiet, and the card is the same one.
    h.tick(30)
    assert len(h.payloads) == 2
    assert {s["collapse_id"] for s in h.transport.sent} == {CARD_KEY}


def test_resolves_only_on_full_recovery_and_reaches_device(h: Harness) -> None:
    h.tick(30)
    h.stop("a", "b")
    h.tick(400)
    h.start("a")  # partial recovery
    h.tick(30)
    assert h.payloads[-1]["mutation"] == "enrich"
    assert h.payloads[-1]["aps"]["alert"]["title"] != "Cameras not recording"
    assert h.payloads[-1]["aps"]["alert"]["title"] == "b isn't recording"
    assert h.card is not None and not h.card.closed
    n = len(h.payloads)
    h.start("b")  # full recovery
    h.tick(30)
    assert len(h.payloads) == n + 1
    resolved = h.payloads[-1]
    assert resolved["mutation"] == "resolve"
    assert resolved["aps"]["alert"]["title"] == "Recording resumed"
    assert "recording again" in resolved["aps"]["alert"]["body"]
    # A real incident leaves a kept row: not ephemeral, no sound.
    assert resolved["ephemeral"] is False
    assert "sound" not in resolved["aps"]
    assert h.card is not None and h.card.closed
    # The device's camera/label filters did not silence it.
    assert len(h.transport.sent) == n + 1


def test_new_incident_after_resolve_sounds_again(h: Harness) -> None:
    h.tick(30)
    h.stop("a")
    h.tick(400)
    h.start("a")
    h.tick(30)
    h.stop("c")
    h.tick(400)
    assert h.payloads[-1]["mutation"] == "create" and h.payloads[-1]["aps"].get("sound")


def test_all_cameras_stalled_together_is_one_card(h: Harness) -> None:
    h.tick(30)
    h.stop("a", "b", "c")
    h.tick(400)
    assert len(h.payloads) == 1
    assert "Every camera is affected" in h.payloads[0]["aps"]["alert"]["body"]


def test_restart_adopts_open_incident_without_resending(h: Harness) -> None:
    h.stop("a")
    h.tick(400)
    assert len(h.payloads) == 1
    engine = SimpleNamespace(db_path=str(h.db_path), transport=h.transport)
    h.watcher = h.make_watcher(engine)  # a fresh process: no memory of the page
    h.tick(30)
    h.tick(30)
    assert len(h.payloads) == 1  # not re-paged
    h.start("a")
    h.tick(30)
    assert h.payloads[-1]["mutation"] == "resolve"
    assert h.payloads[-1]["aps"]["alert"]["title"] == "Recording resumed"


def test_open_card_is_kept_alive_against_the_stale_sweep(h: Harness) -> None:
    h.stop("a")
    h.tick(400)
    before = h.card.updated_at
    h.tick(30)
    assert h.card.updated_at > before
    conn = db.open_sidecar(h.db_path)
    try:
        # What PushEngine.sweep_activities does with push.card_resolution_s = 600
        assert card_store.close_stale_cards(conn, idle_for=600, now=h.clock.now + 30) == 0
    finally:
        conn.close()


def test_quiet_hours_and_mute_drop_the_sound(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        policy_settings, "get_active",
        lambda: {"quiet_hours": {"start": "00:00", "end": "23:59", "mode": "hold_urgent"}},
    )
    h.tick(30)
    h.stop("a")
    h.tick(400)
    assert len(h.payloads) == 1 and "sound" not in h.payloads[0]["aps"]


def test_push_off_still_updates_health_and_sends_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path, push_enabled=False)
    h.stopped = {"a"}
    h.tick(400)
    assert h.payloads == [] and h.card is None
    assert h.watcher.health()["status"] == "stalled"
    (tmp_path / "n").mkdir()
    notify_off = Harness(tmp_path / "n", push_enabled=True, notify=False)
    notify_off.stopped = {"a"}
    notify_off.tick(400)
    assert notify_off.payloads == []
    assert notify_off.watcher.health()["status"] == "stalled"


def test_query_error_does_not_kill_worker_or_change_state(h: Harness) -> None:
    h.stop("a")
    h.tick(400)
    assert h.watcher.health()["status"] == "stalled"
    h.fail = True
    assert h.tick(30) == []
    health = h.watcher.health()
    assert health["status"] == "stalled" and "database is locked" in health["error"]
    assert health["last_check_age_s"] >= 30
    h.fail = False
    h.tick(30)
    assert "error" not in h.watcher.health()


def test_card_failure_does_not_kill_worker(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("apns exploded")

    h.stop("a")
    monkeypatch.setattr(h.watcher, "_send", boom)
    h.tick(400)  # logged, not raised
    assert h.watcher.health()["status"] == "stalled"


def test_loop_survives_an_exploding_tick(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from marcellus import recording_watch as rw

    h = Harness(tmp_path)
    calls: list[int] = []

    async def tick() -> list[Any]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("x")
        return []

    h.watcher.tick = tick  # type: ignore[method-assign]
    h.watcher.cfg = RecordingWatchSection(interval_s=0.01)
    monkeypatch.setattr(rw, "INITIAL_DELAY_S", 0.0)
    app = SimpleNamespace(state=SimpleNamespace(recording_watcher=h.watcher))

    async def go() -> None:
        task = asyncio.create_task(rw.recording_watch_loop(app))
        await asyncio.sleep(0.2)
        task.cancel()

    asyncio.run(go())
    assert len(calls) >= 2


# --- real DB query -----------------------------------------------------------------


def test_default_query_reads_newest_segment_per_camera(tmp_path: Path) -> None:
    from marcellus.recording_watch import _query_newest

    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, path TEXT, "
        "start_time REAL, end_time REAL, duration REAL)"
    )
    rows = [("1", "a", 100.0, 110.0), ("2", "a", 110.0, 120.0), ("3", "b", 50.0, 60.0)]
    for i, cam, s, e in rows:
        conn.execute("INSERT INTO recordings VALUES (?,?,?,?,?,?)", (i, cam, i, s, e, e - s))
    conn.commit()
    conn.close()
    assert _query_newest(p, ["a", "b", "nope"]) == {"a": 120.0, "b": 60.0, "nope": None}


# --- /healthz ------------------------------------------------------------------------


def _app(tmp_path: Path, **rw: Any) -> TestClient:
    cfg = tmp_path / "frigate.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=tmp_path / "f.db"),
        sidecar=SidecarSection(
            db_path=tmp_path / "s.db", bind_port=5001, require_frigate_auth=False
        ),
        recording_watch=RecordingWatchSection(**rw),
    )
    return TestClient(create_app(settings))


def test_healthz_reports_recording_check_and_never_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _app(tmp_path) as client:
        watcher = client.app.state.recording_watcher  # type: ignore[attr-defined]
        watcher.state = WatchState(stalled={"gate-face": NOW - 3600})
        watcher.last_check = time.time() - 12
        body = client.get("/healthz").json()
        rec = body["checks"]["recording"]
        assert rec["status"] == "stalled"
        assert rec["stalled"] == [{"camera": "gate-face", "since": NOW - 3600}]
        assert 11 <= rec["last_check_age_s"] <= 20
        # Informational: whatever the status code is, the stall isn't why.
        assert body["status"] == "ok"
        status = client.get("/status.json").json()["recording"]
        assert status["enabled"] is True and status["status"] == "stalled"


def test_healthz_omits_recording_when_disabled(tmp_path: Path) -> None:
    with _app(tmp_path, enabled=False) as client:
        assert "recording" not in client.get("/healthz").json()["checks"]
        assert client.get("/status.json").json()["recording"]["enabled"] is False


def test_status_page_names_stalled_cameras(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        client.app.state.recording_watcher.state = WatchState(  # type: ignore[attr-defined]
            stalled={"gate-face": NOW}
        )
        html = client.get("/").text
        assert "1 stalled" in html and "gate-face" in html

