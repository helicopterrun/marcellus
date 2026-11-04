"""Tests for the wildlife finder: pure helpers, discovery, scan pipeline."""

from __future__ import annotations

import io
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from marcellus import db
from marcellus.config import (
    FrigateSection,
    Settings,
    SidecarSection,
    WildlifeCamera,
    WildlifeSection,
)
from marcellus.frigate_api import FrigateClient, FrigatePlusError
from marcellus.wildlife import finder, taxonomy
from marcellus.wildlife.engine import Detection, Frame, iou, nms, render_jpeg

SQUIRREL = "86f5;mammalia;rodentia;sciuridae;sciurus;carolinensis;eastern gray squirrel"
DOUGLAS = "aa11;mammalia;rodentia;sciuridae;tamiasciurus;douglasii;douglas's squirrel"
RAT = "0fcb;mammalia;rodentia;muridae;rattus;norvegicus;brown rat"
BLANK = "f185;;;;;;blank"
VEHICLE = "e289;;;;;;vehicle"
HUMAN = "990a;mammalia;primates;hominidae;homo;sapiens;human"
BIRD = "b135;aves;;;;;bird"
CAT = "e4d1;mammalia;carnivora;felidae;felis;catus;domestic cat"
DOG = "3d80;mammalia;carnivora;canidae;canis;familiaris;domestic dog"
COYOTE = "5a0f;mammalia;carnivora;canidae;canis;latrans;coyote"
OPOSSUM = "2a1b;mammalia;didelphimorphia;didelphidae;didelphis;virginiana;virginia opossum"
MAMMAL = "f2d2;mammalia;;;;;mammal"


# --------------------------------------------------------------------------
# taxonomy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "want"),
    [
        (SQUIRREL, "squirrel"),
        (RAT, "rodent"),
        (CAT, "cat"),
        (DOG, "dog"),
        (COYOTE, "coyote"),
        (OPOSSUM, "possum"),
        (BIRD, "bird"),
        (MAMMAL, "animal"),
        ("x;mammalia;carnivora;procyonidae;procyon;lotor;common raccoon", "raccoon"),
        ("x;mammalia;lagomorpha;leporidae;sylvilagus;bachmani;brush rabbit", "rabbit"),
        ("x;mammalia;carnivora;mephitidae;mephitis;mephitis;striped skunk", "skunk"),
        ("x;mammalia;carnivora;canidae;urocyon;cinereoargenteus;gray fox", "fox"),
    ],
)
def test_suggestion_rolls_up_by_family_or_order(label: str, want: str) -> None:
    assert taxonomy.suggestion_for(taxonomy.parse(label)) == want


def test_parse_tolerates_missing_uuid() -> None:
    t = taxonomy.parse("mammalia;rodentia;sciuridae;sciurus;carolinensis;eastern gray squirrel")
    assert (t.family, t.common) == ("sciuridae", "eastern gray squirrel")


def test_decide_sums_species_into_one_confident_suggestion() -> None:
    # No single squirrel species clears 0.5, but the family does.
    v = taxonomy.decide([(SQUIRREL, 0.35), (DOUGLAS, 0.3), (BLANK, 0.2), (RAT, 0.15)],
                        reject_score=0.8)
    assert v.verdict == "animal"
    assert v.suggestion == "squirrel"
    assert v.suggestion_score == pytest.approx(0.65)
    assert v.top_class == "eastern gray squirrel"
    assert v.reject_score == pytest.approx(0.2)


@pytest.mark.parametrize(
    ("top", "verdict"),
    [
        ([(BLANK, 0.9), (CAT, 0.1)], "blank"),
        ([(HUMAN, 0.85), (BLANK, 0.1)], "human"),
        ([(VEHICLE, 0.81)], "vehicle"),
        # Recall first: an unsure classifier keeps the box.
        ([(BLANK, 0.79), (CAT, 0.21)], "animal"),
    ],
)
def test_decide_rejects_only_confident_non_animals(
    top: list[tuple[str, float]], verdict: str
) -> None:
    assert taxonomy.decide(top, reject_score=0.8).verdict == verdict


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "count", "want"),
    [
        (10, 3, [1, 5, 8]),
        (2, 3, [0, 1]),
        (1, 3, [0]),
        (0, 3, []),
        (10, 1, [5]),
        (3, 3, [0, 1, 2]),
    ],
)
def test_pick_indices_spreads_evenly(n: int, count: int, want: list[int]) -> None:
    assert finder.pick_indices(n, count) == want


@pytest.mark.parametrize(
    ("hour", "window", "inside"),
    [
        (12, None, True),
        (7, (7, 19), True),
        (19, (7, 19), False),
        (3, (7, 19), False),
        (23, (20, 6), True),
        (2, (20, 6), True),
        (12, (20, 6), False),
        (5, (0, 0), True),
    ],
)
def test_in_hours_wraps_midnight(hour: int, window: tuple[int, int] | None, inside: bool) -> None:
    assert finder.in_hours(hour, window) is inside


def test_local_hour_uses_the_configured_zone() -> None:
    ts = 1792267471.0  # 2026-10-17 20:04:31 UTC == 13:04 PDT
    assert finder.local_hour(ts, "America/Los_Angeles") == 13
    assert finder.local_hour(ts, "UTC") == 20


def test_chains_respects_gap_and_cap() -> None:
    assert finder.chains(100, 150, 200, gap_s=60, max_visit_s=900)
    assert not finder.chains(100, 150, 211, gap_s=60, max_visit_s=900)
    assert not finder.chains(100, 150, 200, gap_s=60, max_visit_s=90)
    # Retried older segment: a hit just before the visit still chains.
    assert finder.chains(100, 150, 60, gap_s=60, max_visit_s=900)


def test_parse_showinfo_reads_pts_and_size() -> None:
    err = (
        b"[Parsed_showinfo_1 @ 0x55] config in time_base: 1/90000\n"
        b"[Parsed_showinfo_1 @ 0x55] n:   0 pts:      0 pts_time:0       duration:3000 "
        b"fmt:yuv420p sar:1/1 s:1280x720 i:P iskey:1 type:I\n"
        b"[Parsed_showinfo_1 @ 0x55] n:   1 pts:  90000 pts_time:1.0333  duration:3000 "
        b"fmt:yuv420p sar:1/1 s:1280x720 i:P iskey:1 type:I\n"
    )
    assert finder.parse_showinfo(err) == [(0.0, 1280, 720), (1.0333, 1280, 720)]


def test_iou_and_nms() -> None:
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == pytest.approx(1.0)
    assert iou((0, 0, 0.5, 0.5), (0.5, 0.5, 1, 1)) == 0.0
    kept = nms([
        Detection(0.1, 0.1, 0.3, 0.3, 0.5),
        Detection(0.11, 0.1, 0.31, 0.3, 0.9),
        Detection(0.6, 0.6, 0.7, 0.7, 0.3),
    ])
    assert [d.score for d in kept] == [0.9, 0.3]


def _frame(w: int = 64, h: int = 36, pts: float = 0.0) -> Frame:
    return Frame(pts=pts, width=w, height=h, rgb=bytes([90, 120, 60]) * (w * h))


def test_render_jpeg_crops_and_outlines() -> None:
    from PIL import Image

    full = render_jpeg(_frame(), outline=(0.1, 0.1, 0.5, 0.5), max_edge=32)
    assert Image.open(io.BytesIO(full)).size == (32, 18)
    crop = render_jpeg(_frame(), crop=(0.25, 0.25, 0.75, 0.75), max_edge=320)
    assert Image.open(io.BytesIO(crop)).size == (32, 18)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_extract_frames_keyframe_decode(tmp_path: Path) -> None:
    seg = tmp_path / "seg.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=320x180:rate=10:duration=4",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(seg)],
        check=True,
    )
    frames = finder.extract_frames(seg, width=160, count=3)
    assert [round(f.pts) for f in frames] == [0, 2, 3]
    assert all((f.width, f.height, len(f.rgb)) == (160, 90, 160 * 90 * 3) for f in frames)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_extract_frames_raises_on_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(finder.FrameError):
        finder.extract_frames(bad, width=160, count=3)


# --------------------------------------------------------------------------
# DB-backed: discovery, scenery, visits, scan, prune, scoreboard
# --------------------------------------------------------------------------

RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, path TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
    objects INTEGER, motion INTEGER, segment_size REAL NOT NULL, dBFS INTEGER,
    regions INTEGER
);
"""

# 2026-10-17 20:00:00 UTC == 13:00 PDT
T0 = 1792267200.0


class FakeModels:
    """Detector returns `boxes[pts]`; classifier answers from `labels` by
    box position (x1), so a test can say "this spot is a squirrel"."""

    def __init__(
        self,
        boxes: list[Detection],
        labels: dict[float, list[tuple[str, float]]],
    ) -> None:
        self.boxes = boxes
        self.labels = labels
        self.classified = 0

    def detect(self, frame: Frame, *, size: int, min_score: float) -> list[Detection]:
        return [b for b in self.boxes if b.score >= min_score]

    def classify(self, frame: Frame, boxes: list[Detection]) -> list[list[tuple[str, float]]]:
        self.classified += len(boxes)
        return [self.labels[b.x1] for b in boxes]


def _settings(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, **kw: Any
) -> Settings:
    rec_root = tmp_path / "rec"
    wl = {
        "enabled": True,
        "timezone": "America/Los_Angeles",
        "cameras": {"gate-walkway": WildlifeCamera(), "package": WildlifeCamera(hours=(7, 19))},
        "output_dir": tmp_path / "wildlife",
        **kw,
    }
    return Settings(
        frigate=FrigateSection(
            db_path=frigate_db_path, media_path=Path("/media/frigate"), recordings_path=rec_root
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path),
        wildlife=WildlifeSection(**wl),
    )


def _add_recordings(
    frigate_db_path: Path, tmp_path: Path, rows: list[tuple[str, float, int, int]]
) -> None:
    """rows: (camera, start, motion, objects); creates the segment files too."""
    conn = sqlite3.connect(frigate_db_path)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='recordings'").fetchone():
        conn.executescript(RECORDINGS_SCHEMA)
    for cam, start, motion, objects in rows:
        rel = f"recordings/2026-10-17/20/{cam}/{int(start)}.mp4"
        f = tmp_path / "rec" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
        conn.execute(
            "INSERT INTO recordings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"{cam}-{start}", cam, f"/media/frigate/{rel}", start, start + 10, 10.0,
             objects, motion, 1.0, 0, 5),
        )
    conn.commit()
    conn.close()


def _joined(s: Settings) -> sqlite3.Connection:
    return db.open_joined(s.frigate.db_path, s.sidecar.db_path)


def test_find_segments_takes_motion_without_objects_inside_hours(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    night = T0 - 12 * 3600  # 01:00 PDT
    _add_recordings(frigate_db_path, tmp_path, [
        ("gate-walkway", T0, 30, 0),       # due
        ("gate-walkway", T0 + 10, 0, 0),   # no motion
        ("gate-walkway", T0 + 20, 30, 2),  # Frigate tracked something
        ("package", T0 + 30, 30, 0),       # daytime: due
        ("package", night, 30, 0),         # outside package's hours
        ("shed", T0 + 40, 30, 0),          # not configured
    ])
    conn = _joined(s)
    try:
        segs = finder.find_segments(conn, cfg=s.wildlife, now=T0 + 3600)
    finally:
        conn.close()
    assert [(g.camera, g.start_ts) for g in segs] == [("gate-walkway", T0), ("package", T0 + 30)]


def test_find_segments_skips_settled_and_retries_errors(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path, max_attempts=2)
    _add_recordings(frigate_db_path, tmp_path, [
        ("gate-walkway", T0, 30, 0), ("gate-walkway", T0 + 10, 30, 0),
        ("gate-walkway", T0 + 20, 30, 0),
    ])
    conn = _joined(s)
    try:
        for start, status, attempts in ((T0, "scanned", 1), (T0 + 10, "error", 1),
                                        (T0 + 20, "error", 2)):
            conn.execute(
                "INSERT INTO sidecar.wildlife_segments (camera, start_ts, status, attempts, "
                "scanned_at) VALUES ('gate-walkway', ?, ?, ?, 'now')",
                (start, status, attempts),
            )
        conn.commit()
        segs = finder.find_segments(conn, cfg=s.wildlife, now=T0 + 3600)
    finally:
        conn.close()
    assert [(g.start_ts, g.attempts) for g in segs] == [(T0 + 10, 1)]


def _scan(s: Settings, models: FakeModels, monkeypatch: pytest.MonkeyPatch,
          frames: int = 1) -> dict[str, Any]:
    monkeypatch.setattr(
        finder, "extract_frames",
        lambda path, *, width, count: [_frame(pts=float(i)) for i in range(frames)],
    )
    return finder.scan(s, now=T0 + 3600, models=models)


PUMPKIN = Detection(0.5, 0.8, 0.6, 0.9, 0.35)
SQ = Detection(0.2, 0.5, 0.25, 0.55, 0.6)
BIRD_BOX = Detection(0.7, 0.1, 0.72, 0.12, 0.4)


def test_scan_learns_scenery_then_stops_classifying_it(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0 + 10 * i, 30, 0)
                                                for i in range(5)])
    models = FakeModels([PUMPKIN], {PUMPKIN.x1: [(BLANK, 0.97)]})
    summary = _scan(s, models, monkeypatch)
    assert summary["scanned"] == 5
    assert summary["verdicts"] == {"blank": 2, "scenery": 3}
    assert models.classified == 2  # scenery_min_blanks
    assert summary["animals"] == 0


def test_scan_groups_animal_hits_into_visits_with_images(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    starts = [T0, T0 + 10, T0 + 20, T0 + 300]  # last one is past visit_gap_s
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", t, 30, 0) for t in starts])
    models = FakeModels(
        [SQ, BIRD_BOX],
        {SQ.x1: [(SQUIRREL, 0.6), (DOUGLAS, 0.3)], BIRD_BOX.x1: [(BIRD, 0.9)]},
    )
    summary = _scan(s, models, monkeypatch)
    assert summary["animals"] == 4
    assert summary["verdicts"]["excluded"] == 4  # birds are recorded, not queued

    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        visits = conn.execute("SELECT * FROM wildlife_visits ORDER BY start_ts").fetchall()
        boxes = conn.execute(
            "SELECT * FROM wildlife_boxes WHERE verdict = 'animal' ORDER BY frame_ts"
        ).fetchall()
    finally:
        conn.close()
    assert [(v["hits"], v["start_ts"], v["end_ts"]) for v in visits] == [
        (3, T0, T0 + 20), (1, T0 + 300, T0 + 300)
    ]
    assert {b["suggestion"] for b in boxes} == {"squirrel"}
    for b in boxes:
        assert (s.wildlife.output_dir / b["crop_path"]).is_file()
        assert (s.wildlife.output_dir / b["frame_path"]).is_file()
    assert finder.read_last_run(s)["animals"] == 4  # type: ignore[index]


def test_reviewed_visit_is_not_rejoined(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0, 30, 0)])
    models = FakeModels([SQ], {SQ.x1: [(SQUIRREL, 0.9)]})
    _scan(s, models, monkeypatch)
    conn = db.open_sidecar(s.sidecar.db_path)
    conn.execute("UPDATE wildlife_visits SET review = 'discarded'")
    conn.commit()
    conn.close()
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0 + 10, 30, 0)])
    _scan(s, models, monkeypatch)
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        reviews = [r["review"] for r in conn.execute(
            "SELECT review FROM wildlife_visits ORDER BY id")]
    finally:
        conn.close()
    assert reviews == ["discarded", "pending"]


def test_scan_records_missing_files_and_failures(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [
        ("gate-walkway", T0, 30, 0), ("gate-walkway", T0 + 10, 30, 0),
    ])
    (tmp_path / "rec" / f"recordings/2026-10-17/20/gate-walkway/{int(T0)}.mp4").unlink()

    def boom(path: Path, *, width: int, count: int) -> list[Frame]:
        raise finder.FrameError("corrupt")

    monkeypatch.setattr(finder, "extract_frames", boom)
    summary = finder.scan(s, now=T0 + 3600, models=FakeModels([], {}))
    assert (summary["no_file"], summary["error"]) == (1, 1)
    conn = db.open_sidecar(s.sidecar.db_path)
    try:
        rows = {r["start_ts"]: (r["status"], r["attempts"]) for r in conn.execute(
            "SELECT * FROM wildlife_segments")}
    finally:
        conn.close()
    assert rows == {T0: ("no_file", 1), T0 + 10: ("error", 1)}


def test_scan_stops_at_the_time_budget(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0 + 10 * i, 30, 0)
                                                for i in range(3)])
    monkeypatch.setattr(finder, "extract_frames", lambda p, *, width, count: [_frame()])
    summary = finder.scan(s, now=T0 + 3600, models=FakeModels([], {}), max_runtime_s=-1)
    assert (summary["scanned"], summary["deferred"]) == (0, 3)


def test_scan_reports_problems_without_models(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path,
                  detector_path=tmp_path / "missing.onnx")
    summary = finder.scan(s, now=T0)
    assert any("missing.onnx" in p for p in summary["problems"])


def test_disabled_scan_does_nothing(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path, enabled=False)
    assert finder.scan(s, now=T0)["scanned"] == 0


def test_prune_drops_old_rows_and_images(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0, 30, 0)])
    _scan(s, FakeModels([SQ], {SQ.x1: [(SQUIRREL, 0.9)]}), monkeypatch)
    crops = list(s.wildlife.output_dir.glob("*/*.crop.jpg"))
    assert crops
    out = finder.prune(s, now=T0 + 31 * 86400)
    assert out["boxes"] == 1 and out["visits"] == 1 and out["segments"] == 1
    assert not any(p.exists() for p in crops)


def test_scoreboard_compares_frigate_events_with_kept_visits(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    now = time.time()
    conn = sqlite3.connect(frigate_db_path)
    for i, label in enumerate(["cat", "raccoon", "dog", "person"]):
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, data) "
            "VALUES (?, 'gate-walkway', ?, ?, ?, '{}')",
            (f"w{i}", label, now - 3600, now - 3500),
        )
    conn.commit()
    conn.close()
    side = db.open_sidecar(s.sidecar.db_path)
    for review in ("kept", "kept", "discarded", "pending"):
        side.execute(
            "INSERT INTO wildlife_visits (camera, start_ts, end_ts, hits, review, created_at) "
            "VALUES ('gate-walkway', ?, ?, 1, ?, 'now')",
            (now - 600, now - 590, review),
        )
    side.commit()
    side.close()
    board = {r["camera"]: r for r in finder.scoreboard(s, days=7, now=now)}
    gw = board["gate-walkway"]
    # cat + raccoon count; dog and person do not.
    assert gw["frigate_events"] == 2
    assert (gw["visits"], gw["kept"], gw["discarded"], gw["pending"]) == (4, 2, 1, 1)
    assert gw["missed_share"] == pytest.approx(0.5)
    assert board["package"]["missed_share"] is None


def test_check_inputs_flags_bad_timezone(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path, timezone="Mars/Olympus")
    assert any("Mars/Olympus" in p for p in finder.check_inputs(s))


# --------------------------------------------------------------------------
# Frigate+ frame upload client
# --------------------------------------------------------------------------


def _plus_client(handler: Any) -> FrigateClient:
    c = FrigateClient("http://frigate.test:5000")
    c._plus_client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_submit_plus_frame_posts_camera_and_time() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(f"{req.method} {req.url.path}")
        return httpx.Response(
            200, json={"success": True, "message": "Successfully submitted image."}
        )

    with _plus_client(handler) as c:
        assert c.submit_plus_frame("gate-walkway", 1792267471.25) == "Successfully submitted image."
    assert seen == ["POST /api/gate-walkway/plus/1792267471.250"]


def test_submit_plus_frame_raises_on_missing_recording() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"success": False, "message": "Recording not found at 1"})

    with _plus_client(handler) as c, pytest.raises(FrigatePlusError) as ei:
        c.submit_plus_frame("gate-walkway", 1.0)
    assert ei.value.status_code == 404
    assert "Recording not found" in str(ei.value)


class _LockProbingModels(FakeModels):
    """Tries to take the sidecar DB's write lock from another connection
    during inference, as the scrub generator would; records any failure."""

    def __init__(self, db_path: Path, *a: Any) -> None:
        super().__init__(*a)
        self.db_path = db_path
        self.blocked = 0

    def _probe(self) -> None:
        other = sqlite3.connect(self.db_path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        except sqlite3.OperationalError:
            self.blocked += 1
        finally:
            other.close()

    def detect(self, frame: Frame, *, size: int, min_score: float) -> list[Detection]:
        self._probe()
        return super().detect(frame, size=size, min_score=min_score)

    def classify(self, frame: Frame, boxes: list[Detection]) -> list[list[tuple[str, float]]]:
        self._probe()
        return super().classify(frame, boxes)


def test_inference_runs_without_holding_the_write_lock(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0, 30, 0)])
    # Frame 0's squirrel is written before frames 1-3 are inferred.
    models = _LockProbingModels(sidecar_db_path, [SQ], {SQ.x1: [(SQUIRREL, 0.9)]})
    summary = _scan(s, models, monkeypatch, frames=4)
    assert summary["animals"] == 4
    assert models.blocked == 0


def test_scenery_is_learned_within_one_segment(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One segment, five frames: the blanks from its own first frames (not yet
    # written) still make the spot scenery for the rest.
    s = _settings(tmp_path, frigate_db_path, sidecar_db_path)
    _add_recordings(frigate_db_path, tmp_path, [("gate-walkway", T0, 30, 0)])
    models = FakeModels([PUMPKIN], {PUMPKIN.x1: [(BLANK, 0.97)]})
    summary = _scan(s, models, monkeypatch, frames=5)
    assert summary["verdicts"] == {"blank": 2, "scenery": 3}
    assert models.classified == 2
