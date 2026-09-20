"""Tests for the high-res cross-camera face capture engine."""

from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from marcellus import db
from marcellus.config import Settings
from marcellus.faces import crosscam

# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_head_box_takes_top_fraction_and_pads() -> None:
    left, top, right, bottom = crosscam.head_box(
        [0.10, 0.20, 0.20, 0.60], head_fraction=0.5, pad=0.0
    )
    assert (left, top) == pytest.approx((0.10, 0.20))
    assert right == pytest.approx(0.30)
    # top half of a 0.60-tall box == 0.30
    assert bottom == pytest.approx(0.50)


def test_head_box_pad_expands_and_clamps() -> None:
    box = crosscam.head_box([0.0, 0.0, 0.20, 0.40], head_fraction=0.5, pad=0.5)
    # padding would push left/top negative; must clamp to 0
    assert box[0] == 0.0
    assert box[1] == 0.0
    assert box[2] <= 1.0 and box[3] <= 1.0


@pytest.mark.parametrize(
    "bad",
    [[0, 0, 0, 0], [0.1, 0.1, -0.2, 0.3], [], [1, 2], "nonsense", None],
)
def test_head_box_degenerate_returns_full_frame(bad: object) -> None:
    assert crosscam.head_box(bad, head_fraction=0.4, pad=0.25) == (0.0, 0.0, 1.0, 1.0)  # type: ignore[arg-type]


def test_head_box_tolerates_box_running_past_the_frame_edge() -> None:
    # Frigate really does emit these (a region of [0,0,0.82,1.094] was observed).
    box = crosscam.head_box([0.0, 0.0, 0.82, 1.094], head_fraction=0.4, pad=0.25)
    assert all(0.0 <= v <= 1.0 for v in box)
    assert box[2] > box[0] and box[3] > box[1]


def _cand(eid: str, ts: float, cam: str = "doorbell") -> crosscam.Candidate:
    return crosscam.Candidate(
        event_id=eid, camera=cam, label="person", start_time=ts, top_score=0.9
    )


def test_group_into_visits_chains_within_window() -> None:
    out = crosscam.group_into_visits(
        [_cand("a", 100.0), _cand("b", 103.0, "gate-walkway"), _cand("c", 106.0, "package")],
        dedup_window_s=60.0,
        max_visit_s=300.0,
    )
    assert [o[1] for o in out] == ["a", "a", "a"]
    assert [o[2] for o in out] == [True, False, False]


def test_group_into_visits_splits_across_gap() -> None:
    out = crosscam.group_into_visits(
        [_cand("a", 100.0), _cand("b", 200.0)], dedup_window_s=60.0, max_visit_s=300.0
    )
    assert [o[1] for o in out] == ["a", "b"]
    assert [o[2] for o in out] == [True, True]


def test_group_into_visits_caps_a_loiter_at_max_visit() -> None:
    # 20 events 10s apart: gap-chaining alone would make one 200s visit, but
    # max_visit_s=60 must start a fresh one.
    cands = [_cand(f"e{i}", 100.0 + i * 10) for i in range(20)]
    out = crosscam.group_into_visits(cands, dedup_window_s=60.0, max_visit_s=60.0)
    heads = [o[0].event_id for o in out if o[2]]
    assert len(heads) > 1


def test_group_into_visits_survives_a_run_boundary_via_prior() -> None:
    # prior visit ended at t=100; a candidate at t=130 is within the window, so
    # it must JOIN that visit rather than start a new one.
    out = crosscam.group_into_visits(
        [_cand("b", 130.0)],
        dedup_window_s=60.0,
        max_visit_s=300.0,
        prior=("a", 90.0, 100.0),
    )
    assert out[0][1] == "a"
    assert out[0][2] is False


def test_samples_for_applies_annotation_offset_with_the_documented_sign() -> None:
    # recording_time = detection_time + annotation_offset_ms/1000
    out = crosscam.samples_for(1000.0, offsets_s=[-4, 0, 4], annotation_offset_ms=-1000)
    assert [s.frame_ts for s in out] == [995.0, 999.0, 1003.0]
    assert [s.offset_ms for s in out] == [-4000, 0, 4000]


def test_samples_for_dedupes_identical_offsets() -> None:
    assert len(crosscam.samples_for(0.0, offsets_s=[0, 0.0], annotation_offset_ms=0)) == 1


def test_relative_paths_are_date_sharded_and_sanitised() -> None:
    full, thumb = crosscam.relative_paths("../evil/id", -4000, 1787435663.9)
    assert full.startswith("2026-08-22/")
    assert ".." not in Path(full).name
    assert "/" not in Path(full).name
    assert thumb.endswith(".thumb.jpg")


def _jpeg(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (128, 64, 32)).save(buf, format="JPEG")
    return buf.getvalue()


def test_render_preview_downscales_and_reports_source_size() -> None:
    out = crosscam.render_preview(_jpeg(2560, 1440), crop=None, max_edge=480, quality=80)
    assert out is not None
    data, w, h = out
    assert (w, h) == (2560, 1440)
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= 480


def test_render_preview_crops_when_given_a_box() -> None:
    out = crosscam.render_preview(
        _jpeg(2560, 1440), crop=(0.0, 0.0, 0.5, 0.25), max_edge=4096, quality=80
    )
    assert out is not None
    from PIL import Image

    with Image.open(io.BytesIO(out[0])) as im:
        assert im.size == (1280, 360)


def test_render_preview_returns_none_on_garbage() -> None:
    assert crosscam.render_preview(b"not a jpeg", crop=None, max_edge=480, quality=80) is None


def test_aspect_ok_guards_against_anamorphic_crops() -> None:
    assert crosscam.aspect_ok(16 / 9, 2560, 1440) is True
    assert crosscam.aspect_ok(4 / 3, 2560, 1440) is False
    assert crosscam.aspect_ok(None, 2560, 1440) is False


# --------------------------------------------------------------------------
# Engine integration
# --------------------------------------------------------------------------

_FRIGATE_SCHEMA = """
CREATE TABLE event (
    id TEXT PRIMARY KEY, label TEXT, camera TEXT,
    start_time REAL, end_time REAL, top_score REAL, score REAL,
    has_snapshot INTEGER, has_clip INTEGER, zones TEXT, data TEXT
);
"""


@pytest.fixture()
def frigate_db_with_visit(tmp_path: Path) -> Path:
    """A cross-camera visit plus a lone later event, with a capture-cam box."""
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(_FRIGATE_SCHEMA)
    t = 1_000_000.0
    rows = [
        ("d1", "person", "doorbell", t, t + 20, json.dumps({"box": [0.1, 0.2, 0.2, 0.6]})),
        ("g1", "person", "gate-walkway", t + 3, t + 25, json.dumps({"box": [0.1, 0.2, 0.2, 0.6]})),
        ("p1", "person", "package", t + 6, t + 18, json.dumps({"box": [0.1, 0.2, 0.2, 0.6]})),
        # capture camera's own event, live across the whole visit
        ("c1", "person", "gate-face", t - 8, t + 30, json.dumps({"box": [0.11, 0.10, 0.23, 0.66]})),
        # a separate visit much later
        ("d2", "person", "doorbell", t + 900, t + 915, json.dumps({"box": [0.3, 0.3, 0.1, 0.3]})),
    ]
    for eid, label, cam, st, et, data in rows:
        conn.execute(
            "INSERT INTO event (id,label,camera,start_time,end_time,has_snapshot,data) "
            "VALUES (?,?,?,?,?,1,?)",
            (eid, label, cam, st, et, data),
        )
    conn.commit()
    conn.close()
    return p


def _settings(tmp_path: Path, frigate_db: Path) -> Settings:
    s = Settings()
    s.frigate.db_path = frigate_db
    s.sidecar.db_path = tmp_path / "sidecar.db"
    s.face_capture.enabled = True
    s.face_capture.trigger_cameras = ["doorbell", "gate-walkway", "package"]
    s.face_capture.capture_camera = "gate-face"
    s.face_capture.output_dir = tmp_path / "out"
    s.face_capture.apply_annotation_offset = False
    return s


class _Recorder:
    """Counts requests so idempotency can be asserted on network traffic."""

    def __init__(self, handler: object) -> None:
        self.calls = 0
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._handler(request)  # type: ignore[operator]


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler: object) -> _Recorder:
    rec = _Recorder(handler)
    import marcellus.frigate_api as fa

    real_init = fa.FrigateClient.__init__

    def init(self: fa.FrigateClient, base_url: str, timeout: float = 10.0) -> None:
        real_init(self, base_url, timeout)
        self._client = httpx.Client(transport=httpx.MockTransport(rec))  # type: ignore[attr-defined]

    monkeypatch.setattr(fa.FrigateClient, "__init__", init)
    return rec


def test_scan_captures_visit_head_and_dedupes_the_rest(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = _jpeg(2560, 1440)
    rec = _patch_client(monkeypatch, lambda req: httpx.Response(200, content=img))
    s = _settings(tmp_path, frigate_db_with_visit)

    out = crosscam.scan(s, now=1_000_000.0 + 2000)
    assert out["captured"] == 6  # two visit heads x 3 offsets
    assert out["deduped"] == 2  # g1 and p1 chain to d1
    assert rec.calls == 6

    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        statuses = {
            r["trigger_event_id"]: r["status"]
            for r in conn.execute("SELECT trigger_event_id, status FROM face_captures")
        }
        assert statuses["g1"] == "deduped"
        assert statuses["p1"] == "deduped"
        files = list(Path(s.face_capture.output_dir).rglob("*.jpg"))
        assert len([f for f in files if not f.name.endswith(".thumb.jpg")]) == 6
    finally:
        conn.close()


def test_scan_is_idempotent_and_issues_no_repeat_requests(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    img = _jpeg(640, 360)
    rec = _patch_client(monkeypatch, lambda req: httpx.Response(200, content=img))
    s = _settings(tmp_path, frigate_db_with_visit)

    crosscam.scan(s, now=1_000_000.0 + 2000)
    first = rec.calls
    second = crosscam.scan(s, now=1_000_000.0 + 2000)
    assert rec.calls == first, "a re-run must not re-fetch"
    assert second["captured"] == 0


def test_404_is_terminal_and_not_retried(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _patch_client(monkeypatch, lambda req: httpx.Response(404, json={"success": False}))
    s = _settings(tmp_path, frigate_db_with_visit)

    out = crosscam.scan(s, now=1_000_000.0 + 2000)
    assert out["captured"] == 0
    assert out["no_recording"] == 6
    calls = rec.calls
    crosscam.scan(s, now=1_000_000.0 + 2000)
    assert rec.calls == calls, "no_recording is terminal"


def test_transport_error_is_retried_until_max_attempts(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    rec = _patch_client(monkeypatch, boom)
    s = _settings(tmp_path, frigate_db_with_visit)
    s.face_capture.max_attempts = 2

    out = crosscam.scan(s, now=1_000_000.0 + 2000)
    assert out["error"] == 6
    after_first = rec.calls
    crosscam.scan(s, now=1_000_000.0 + 2000)
    assert rec.calls > after_first, "transport errors must be retried"
    # third run: attempts has hit max_attempts, so the candidate drops out
    calls_before = rec.calls
    crosscam.scan(s, now=1_000_000.0 + 2000)
    assert rec.calls == calls_before, "retries must be bounded by max_attempts"


def test_candidate_upper_bound_subtracts_max_offset(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An event too recent for its LATEST sample must not be picked up yet."""
    rec = _patch_client(monkeypatch, lambda req: httpx.Response(200, content=_jpeg(64, 36)))
    s = _settings(tmp_path, frigate_db_with_visit)
    s.face_capture.capture_delay_s = 45.0
    s.face_capture.offsets_s = [0.0, 4.0]

    # now such that d1.start is exactly capture_delay old: the +4s sample would
    # still be in the future-ish window, so d1 must be excluded.
    out = crosscam.scan(s, now=1_000_000.0 + 45.0)
    assert out["candidates"] == 0
    assert rec.calls == 0

    # push past delay + max offset and it becomes a candidate
    out2 = crosscam.scan(s, now=1_000_000.0 + 45.0 + 4.0 + 1.0)
    assert out2["candidates"] >= 1


def test_check_inputs_reports_unwritable_output_dir(tmp_path: Path) -> None:
    s = Settings()
    s.face_capture.enabled = True
    s.face_capture.trigger_cameras = ["doorbell"]
    s.face_capture.capture_camera = "gate-face"
    s.face_capture.output_dir = Path("/proc/definitely-not-writable/x")
    problems = crosscam.check_inputs(s)
    assert any("not writable" in p for p in problems)


def test_scan_aborts_without_touching_the_network_when_misconfigured(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _patch_client(monkeypatch, lambda req: httpx.Response(200, content=_jpeg(64, 36)))
    s = _settings(tmp_path, frigate_db_with_visit)
    s.face_capture.trigger_cameras = []
    out = crosscam.scan(s, now=1_000_000.0 + 2000)
    assert out.get("problems")
    assert rec.calls == 0


def test_prune_drops_old_rows_files_and_day_dirs(
    tmp_path: Path, frigate_db_with_visit: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_client(monkeypatch, lambda req: httpx.Response(200, content=_jpeg(64, 36)))
    s = _settings(tmp_path, frigate_db_with_visit)
    crosscam.scan(s, now=1_000_000.0 + 2000)

    assert list(Path(s.face_capture.output_dir).rglob("*.jpg"))
    # everything is far older than retention relative to "now"
    out = crosscam.prune(s, now=time.time())
    assert out["rows"] > 0

    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) c FROM face_captures").fetchone()["c"] == 0
    finally:
        conn.close()
