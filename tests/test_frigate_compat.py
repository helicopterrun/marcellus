"""Frigate version compatibility matrix (docs/frigate-compatibility.md).

Marcellus reads Frigate's SQLite file directly and calls Frigate's HTTP API,
and neither contract is ours. Every test here runs once per Frigate release
in `FRIGATE_VERSIONS`, against fixtures generated from that release's
upstream source by tools/frigate_schema_fixture.py:

* `fixtures/frigate_schema/<tag>.sql` -- the schema Frigate's own migrations
  build. The read-path tests seed rows the way that release writes them and
  run Marcellus's real queries over them.
* `fixtures/frigate_api/<tag>.json` -- that release's route table and
  request/query models. The HTTP tests check that every endpoint Marcellus
  calls exists with the method and auth it assumes, and that the requests it
  sends only use fields that release accepts.

Several read paths swallow `sqlite3.Error` on purpose (a reel without a
severity spine beats a 500), so the tests assert on returned values, never
merely on "did not raise".

Adding a release: run the generator for the new tag, add it to
`FRIGATE_VERSIONS`, and update docs/frigate-compatibility.md.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.analysis.pull_events import pull
from marcellus.config import (
    FrigateSection,
    ProxySection,
    PushSection,
    ScrubSection,
    Settings,
    SidecarSection,
)
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.observations import load_direction
from marcellus.encounters.repair import _frigate_row
from marcellus.encounters.service import EncounterService
from marcellus.frigate_api import FrigateAPIError, FrigateClient
from marcellus.push.engine import PushEngine
from marcellus.push.mqtt import MqttReviewSubscriber, backfill_since
from marcellus.push.transport import LogTransport
from marcellus.routes import scrub as scrub_routes
from marcellus.routes.triage import _get_event_full
from marcellus.server import create_app
from marcellus.triage.outbox import check_event
from marcellus.zones import load_camera_zones, zones_containing_box

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: Every Frigate release the fixture tests cover. Only v0.17.2 has also been
#: verified against a live server -- see the README / compatibility doc.
FRIGATE_VERSIONS = ("v0.17.2", "v0.18.2")

CAMERA = "front"

#: Every column Marcellus reads from Frigate's tables (inventory in
#: docs/frigate-compatibility.md). A release that drops or renames one fails
#: `test_every_column_marcellus_reads_exists`.
FRIGATE_COLUMNS_READ: dict[str, set[str]] = {
    "event": {
        "id", "camera", "label", "sub_label", "start_time", "end_time", "zones",
        "has_clip", "has_snapshot", "false_positive", "plus_id", "score",
        "top_score", "area", "ratio", "retain_indefinitely", "data",
    },
    "reviewsegment": {"id", "camera", "start_time", "end_time", "severity", "data"},
    "recordings": {"id", "camera", "path", "start_time", "end_time", "motion", "objects"},
}

#: (method, Frigate route template, auth dependency Marcellus relies on) for
#: every Frigate endpoint the sidecar itself calls. Admin-only routes work
#: because the sidecar talks to `frigate.base_url`, Frigate's internal port,
#: which Frigate's own auth treats as admin.
MARCELLUS_ENDPOINTS: tuple[tuple[str, str, str], ...] = (
    ("GET", "/api/version", "[Depends(allow_public())]"),
    ("POST", "/api/login", "[Depends(allow_public())]"),
    ("GET", "/api/stats", "[Depends(allow_any_authenticated())]"),
    ("GET", "/api/config", "[Depends(allow_any_authenticated())]"),
    ("GET", "/api/config/raw", "[Depends(require_role(['admin']))]"),
    ("PUT", "/api/config/set", "[Depends(require_role(['admin']))]"),
    ("POST", "/api/restart", "[Depends(require_role(['admin']))]"),
    ("GET", "/api/events", "[Depends(allow_any_authenticated())]"),
    ("GET", "/api/events/{event_id}/thumbnail.{extension}", ""),
    ("GET", "/api/events/{event_id}/snapshot.jpg", ""),
    ("POST", "/api/events/{event_id}/sub_label", "[Depends(require_role(['admin']))]"),
    ("POST", "/api/events/{event_id}/plus", "[Depends(require_role(['admin']))]"),
    ("PUT", "/api/events/{event_id}/false_positive", "[Depends(require_role(['admin']))]"),
    ("GET", "/api/review/activity/motion", "[Depends(allow_any_authenticated())]"),
    ("GET", "/api/{camera_name}/recordings/summary", "[Depends(require_camera_access)]"),
    (
        "GET",
        "/api/{camera_name}/recordings/{frame_time}/snapshot.{format}",
        "[Depends(require_camera_access)]",
    ),
    ("GET", "/api/{camera_name}/latest.{extension}", "[Depends(require_camera_access)]"),
    ("POST", "/api/{camera_name}/plus/{frame_time}", "[Depends(require_camera_access)]"),
    ("POST", "/api/faces/train/{name}/classify", ""),
)


# -- fixtures ----------------------------------------------------------------


def _schema_sql(version: str) -> str:
    return (FIXTURES / "frigate_schema" / f"{version}.sql").read_text()


def _manifest(version: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "frigate_api" / f"{version}.json").read_text())


def _routes(version: str) -> dict[tuple[str, str], str]:
    return {(r["method"], r["path"]): r["auth"] for r in _manifest(version)["routes"]}


def _model_fields(version: str, model: str) -> set[str]:
    return set(_manifest(version)["models"][model])


def _event_data(version: str, t0: float, **extra: Any) -> dict[str, Any]:
    """`event.data` as frigate/events/maintainer.py writes it at `version`.

    The score/top_score/box/region COLUMNS stay NULL -- both releases write
    those only into this blob (the columns carry "TODO remove" in models.py).
    """
    data: dict[str, Any] = {
        "box": [0.40, 0.30, 0.10, 0.40],
        "region": [0.30, 0.20, 0.40, 0.60],
        "score": 0.82,
        "top_score": 0.91,
        "attributes": [],
        "average_estimated_speed": 0,
        "velocity_angle": 0,
        "type": "object",
        "max_severity": "alert",
        "path_data": [[[0.30 + 0.02 * i, 0.70], t0 + i] for i in range(12)],
    }
    if version >= "v0.18":
        # 0.18 adds the clean-snapshot bookkeeping (additive keys only).
        data.update(
            snapshot_clean=True,
            snapshot_frame_time=t0 + 3,
            snapshot_area=1234,
            snapshot_estimated_speed=0,
        )
    data.update(extra)
    return data


def _seed(conn: sqlite3.Connection, version: str, now: float) -> dict[str, float]:
    t0 = now - 600
    for i in range(6):
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "objects, motion, segment_size, dBFS, regions) "
            "VALUES (?, ?, ?, ?, ?, 10.0, ?, ?, 1.2, NULL, 2)",
            (
                f"seg{i}",
                CAMERA,
                f"/media/frigate/recordings/2026-11-12/10/{CAMERA}/00.{i:02d}.mp4",
                t0 + 10 * i,
                t0 + 10 * i + 10,
                1 if i < 3 else 0,
                40 if i % 2 == 0 else 0,
            ),
        )
    events = (
        # id, label, start, end, zones, sub_label, plus_id, false_positive
        ("e1", "person", t0 + 5, t0 + 25, ["walkway", "porch"], "Alex", None, 0),
        ("e2", "car", t0 + 30, None, ["street"], None, "plus-123", 0),
    )
    for eid, label, start, end, zones, sub_label, plus_id, fp in events:
        conn.execute(
            "INSERT INTO event (id, label, camera, start_time, end_time, top_score, "
            "false_positive, zones, thumbnail, has_clip, has_snapshot, region, box, area, "
            "retain_indefinitely, sub_label, ratio, plus_id, score, model_hash, "
            "detector_type, model_type, data) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, '', 1, 1, "
            "NULL, NULL, NULL, 0, ?, NULL, ?, NULL, 'abc', 'cpu', 'ssd', ?)",
            (eid, label, CAMERA, start, end, fp, json.dumps(zones), sub_label, plus_id,
             json.dumps(_event_data(version, start))),
        )
    # reviewsegment.data as frigate/review/maintainer.py writes it (identical
    # in both releases).
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, "
        "thumb_path, data) VALUES ('r1', ?, ?, ?, 'alert', ?, ?)",
        (
            CAMERA, t0 + 4, t0 + 27,
            f"/media/frigate/clips/review/thumb-{CAMERA}-r1.webp",
            json.dumps({
                "detections": ["e1"], "objects": ["person"], "verified_objects": [],
                "sub_labels": ["Alex"], "zones": ["walkway", "porch"], "audio": [],
                "thumb_time": t0 + 10, "metadata": None,
            }),
        ),
    )
    conn.commit()
    return {"t0": t0, "now": now}


@pytest.fixture(params=FRIGATE_VERSIONS)
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def frigate_db(version: str, tmp_path: Path) -> Iterator[tuple[Path, dict[str, float]]]:
    p = tmp_path / f"frigate-{version}.db"
    conn = sqlite3.connect(p)
    conn.executescript(_schema_sql(version))
    times = _seed(conn, version, time.time())
    conn.close()
    yield p, times


def _settings(tmp_path: Path, frigate_path: Path, **kw: Any) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    if not cfg.exists():
        cfg.write_text(f"cameras:\n  {CAMERA}: {{}}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url="http://frigate.test:8971",
            config_path=cfg,
            db_path=frigate_path,
        ),
        sidecar=SidecarSection(
            db_path=tmp_path / "marcellus.db", bind_port=5001, require_frigate_auth=False
        ),
        scrub=ScrubSection(
            enabled=False, retention_days=4, cache_dir=tmp_path / "scrub",
            reel_motion_deadline_s=0,
        ),
        **kw,
    )


# -- the fixtures themselves --------------------------------------------------


def test_fixtures_name_their_upstream_source(version: str) -> None:
    header = _schema_sql(version).splitlines()[1]
    source = rf"github\.com/blakeblackshear/frigate @ {re.escape(version)} \([0-9a-f]{{40}}\)"
    assert re.fullmatch(rf"-- Source: {source}", header), header
    manifest = _manifest(version)
    assert manifest["tag"] == version
    assert manifest["source"] == header.removeprefix("-- Source: ")


def test_versions_are_documented() -> None:
    doc = (ROOT / "docs" / "frigate-compatibility.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "docs/frigate-compatibility.md" in readme
    for v in FRIGATE_VERSIONS:
        assert v.removeprefix("v") in doc
        assert v.removeprefix("v") in readme


# -- database: schema --------------------------------------------------------


def test_every_column_marcellus_reads_exists(version: str, tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "schema.db")
    conn.executescript(_schema_sql(version))
    for table, wanted in FRIGATE_COLUMNS_READ.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert have, f"{version}: table {table} missing"
        assert wanted <= have, f"{version}: {table} lacks {sorted(wanted - have)}"
    conn.close()


# -- database: read paths ----------------------------------------------------


def test_recordings_coverage(frigate_db: tuple[Path, dict[str, float]]) -> None:
    path, t = frigate_db
    conn = db.open_frigate_ro(path)
    try:
        cov = db.recording_coverage(conn, CAMERA, t["t0"] - 10, t["now"], now=t["now"])
        newest = db.newest_recording_end(conn, CAMERA)
        cameras = scrub_routes._known_cameras(SimpleNamespace(), conn)
    finally:
        conn.close()
    assert cov["recorded"] == [(t["t0"], t["t0"] + 60)]
    assert cov["latest_segment_end"] == t["t0"] + 60
    assert newest == t["t0"] + 60
    assert cameras == {CAMERA}


def test_reel_events_from_event_table(frigate_db: tuple[Path, dict[str, float]]) -> None:
    path, t = frigate_db
    conn = db.open_frigate_ro(path)
    try:
        events = scrub_routes._events_json(conn, CAMERA, t["t0"], t["now"])
    finally:
        conn.close()
    by_id = {e["id"]: e for e in events}
    assert set(by_id) == {"e1", "e2"}
    e1 = by_id["e1"]
    assert e1["label"] == "person"
    assert e1["zones"] == ["walkway", "porch"]
    assert e1["score"] == pytest.approx(0.91)  # from data.top_score; column is NULL
    assert e1["sub_label"] == "Alex"
    assert e1["has_clip"] is True and e1["has_snapshot"] is True
    assert e1["path"] is not None  # data.path_data decoded
    assert by_id["e2"]["end"] is None  # in progress


def test_reel_reviews_from_reviewsegment(frigate_db: tuple[Path, dict[str, float]]) -> None:
    path, t = frigate_db
    conn = db.open_frigate_ro(path)
    try:
        reviews = scrub_routes._reviews_json(conn, CAMERA, t["t0"], t["now"])
        repaired = _frigate_row(conn, "r1")
    finally:
        conn.close()
    assert reviews == [
        {
            "id": "r1", "start": t["t0"] + 4, "end": t["t0"] + 27, "severity": "alert",
            "objects": ["person"], "zones": ["walkway", "porch"], "detections": ["e1"],
        }
    ]
    assert repaired is not None and repaired["end_time"] == t["t0"] + 27


def test_encounters_reconcile_reads_reviewsegment(
    frigate_db: tuple[Path, dict[str, float]], tmp_path: Path
) -> None:
    path, t = frigate_db
    service = EncounterService(
        _settings(tmp_path, path), adjacency=Adjacency(edges=frozenset()), now=lambda: t["now"]
    )
    stats = service.reconcile()
    assert (stats.rows, stats.new, stats.errors) == (1, 1, 0)


def test_event_detail_and_direction(
    frigate_db: tuple[Path, dict[str, float]], tmp_path: Path
) -> None:
    path, _t = frigate_db
    full = _get_event_full(frigate_db=path, sidecar_db=tmp_path / "side.db", event_id="e2")
    assert full is not None
    assert full["plus_id"] == "plus-123"
    assert full["top_score"] == pytest.approx(0.91)
    assert full["box"] == [0.40, 0.30, 0.10, 0.40]
    assert full["max_severity"] == "alert"

    conn = db.open_frigate_ro(path)
    try:
        direction = load_direction(conn, ["e1"])
    finally:
        conn.close()
    assert (direction.first_zone, direction.last_zone) == ("walkway", "porch")


def test_analysis_and_triage_reads(frigate_db: tuple[Path, dict[str, float]]) -> None:
    path, _t = frigate_db
    rows = {r["id"]: r for r in pull(frigate_db=path, days=1)}
    assert set(rows) == {"e1", "e2"}
    assert rows["e1"]["top_score"] == pytest.approx(0.91)
    assert rows["e1"]["box"] == [0.40, 0.30, 0.10, 0.40]
    assert rows["e1"]["type"] == "object"
    assert rows["e1"]["zones"] == ["walkway", "porch"]
    assert rows["e2"]["plus_id"] == "plus-123"

    ev = check_event(path, "e1", "tp")  # json_extract(data, '$.box')
    assert json.loads(ev["box"]) == [0.40, 0.30, 0.10, 0.40]


def test_reel_and_highlights_end_to_end(
    frigate_db: tuple[Path, dict[str, float]], tmp_path: Path, version: str
) -> None:
    """The whole `/v1/reel` read: recordings + event + reviewsegment from the
    fixture DB, motion from a mocked `/api/review/activity/motion`."""
    path, t = frigate_db
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_motion_response(version, [(t["t0"], 80.0)]))

    app = create_app(_settings(tmp_path, path))
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TestClient(app)
    r = client.get(
        f"/v1/reel/{CAMERA}",
        params={"start": t["t0"] - 60, "end": t["now"], "motion_scale": 60},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert {e["id"] for e in body["events"]} == {"e1", "e2"}
    assert [x["id"] for x in body["reviews"]] == ["r1"]
    assert "motion_unavailable" not in body  # only present (true) on a failed fetch
    assert max(body["motion"]["values"]) == pytest.approx(80.0)
    assert seen and set(seen[0].url.params) <= _model_fields(
        version, "ReviewActivityMotionQueryParams"
    )

    r = client.get(f"/v1/highlights/{CAMERA}", params={"before": t["now"], "limit": 10})
    assert r.status_code == 200, r.text
    highlights = r.json()["highlights"]
    assert {h["reason"] for h in highlights} >= {"person"}
    assert all(h["score"] == pytest.approx(0.91) for h in highlights)  # from data


# -- HTTP: route table -------------------------------------------------------


@pytest.mark.parametrize(("method", "route", "auth"), MARCELLUS_ENDPOINTS)
def test_frigate_endpoint_exists_with_expected_auth(
    version: str, method: str, route: str, auth: str
) -> None:
    routes = _routes(version)
    assert (method, route) in routes, f"{version} has no {method} {route}"
    assert routes[(method, route)] == auth, (
        f"{version} changed the auth on {method} {route}: re-check how the sidecar calls it"
    )


# -- HTTP: requests the sidecar sends ----------------------------------------


def _frigate_client(handler: Any) -> FrigateClient:
    fc = FrigateClient("http://frigate.test:5000")
    fc._client = httpx.Client(transport=httpx.MockTransport(handler))
    fc._plus_client = httpx.Client(transport=httpx.MockTransport(handler))
    return fc


def _config_set_response(version: str) -> dict[str, Any]:
    # frigate/api/app.py config_set, requires_restart=1 path; same text in both.
    return {"success": True, "message": "Config successfully updated, restart to apply"}


def test_config_set_request_matches_version(version: str) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_config_set_response(version))

    _frigate_client(handler).set_annotation_offset(CAMERA, -1200)
    (req,) = seen
    assert (req.method, req.url.path) == ("PUT", "/api/config/set")
    body = json.loads(req.content)
    assert set(body) <= _model_fields(version, "AppConfigSetBody")
    assert body["requires_restart"] == 1  # writes YAML only; restart() applies it
    assert body["config_data"] == {"cameras": {CAMERA: {"detect": {"annotation_offset": -1200}}}}


def test_config_set_busy_lock_is_an_error(version: str) -> None:
    """0.18 serialises config writes behind a file lock and answers 503 when
    it is held; 0.17 can answer 400/500. Each must surface, never pass."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"success": False, "message": "busy"})

    with pytest.raises(FrigateAPIError):
        _frigate_client(handler).set_annotation_offset(CAMERA, 0)


def test_event_write_requests_match_version(version: str) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"success": True, "plus_id": "p1", "message": "ok"})

    fc = _frigate_client(handler)
    fc.set_sub_label("e1", "Alex", score=0.93)
    fc.submit_plus("e1")
    sub_label, plus = seen
    assert sub_label.url.path == "/api/events/e1/sub_label"
    assert set(json.loads(sub_label.content)) <= _model_fields(version, "EventsSubLabelBody")
    assert plus.url.path == "/api/events/e1/plus"
    assert set(json.loads(plus.content)) <= _model_fields(version, "SubmitPlusBody")


def test_snapshot_query_matches_version(version: str) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"\xff\xd8jpeg")

    assert _frigate_client(handler).event_snapshot_jpeg("e1") == (b"\xff\xd8jpeg", 200)
    assert set(seen[0].url.params) <= _model_fields(version, "MediaEventsSnapshotQueryParams")


async def test_events_backfill_query_matches_version(version: str, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    now = time.time()

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        # EventResponse fields read: id, camera, label, start_time (same in both).
        return httpx.Response(
            200, json=[{"id": "e9", "camera": CAMERA, "label": "person", "start_time": now}]
        )

    engine = PushEngine(
        db_path=str(tmp_path / "push.db"), transport=LogTransport(), server_id="s1"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await backfill_since(
            engine,
            frigate_base_url="http://frigate.test:5000",
            after=now - 60,
            client=client,
        )
    assert seen[0].url.path == "/api/events"
    assert set(seen[0].url.params) <= _model_fields(version, "EventsQueryParams")


# -- HTTP: motion activity response ------------------------------------------


def _motion_response(
    version: str, buckets: list[tuple[float, float | None]]
) -> list[dict[str, Any]]:
    """`/api/review/activity/motion` as each release shapes it.

    `buckets` is the resampled series, `None` for a bucket with no
    motion>0 recording. Both releases resample with pandas and fill gaps
    with motion 0 and camera "" (frigate/api/review.py `motion_activity`);
    0.18 then drops those gap rows (`df = df[df["camera"] != ""]`).
    """
    out = []
    for ts, motion in buckets:
        if motion is None:
            if version >= "v0.18":
                continue
            out.append({"start_time": ts, "motion": 0.0, "camera": ""})
        else:
            out.append({"start_time": ts, "motion": motion, "camera": CAMERA})
    return out


async def _aggregate(version: str, start: float, end: float, scale: float) -> list[float]:
    buckets: list[tuple[float, float | None]] = [
        (start, 100.0), (start + 300, None), (start + 600, 50.0),
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_motion_response(version, buckets))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    http_client=client,
                    settings=SimpleNamespace(
                        frigate=SimpleNamespace(base_url="http://frigate.test:5000")
                    ),
                )
            )
        )
        values, unavailable = await scrub_routes._fetch_and_aggregate_motion(
            request,  # type: ignore[arg-type]
            CAMERA, start, end, scale,
        )
    assert unavailable is False
    return values


async def test_motion_trace_at_fetch_scale(version: str) -> None:
    start = 1_780_000_200.0
    assert await _aggregate(version, start, start + 900, 300) == [100.0, 0.0, 50.0]


@pytest.mark.parametrize(
    "motion_version",
    [
        "v0.17.2",
        pytest.param(
            "v0.18.2",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "0.18 drops zero-motion gap buckets from /api/review/activity/motion, "
                    "and aggregate_motion averages only the points it gets, so a /v1/motion "
                    "bucket wider than 300 s reads higher than on 0.17 (75 vs 50 here). "
                    "Fix needs a decision: treat missing fetch buckets as zero motion "
                    "(see docs/frigate-compatibility.md)."
                ),
            ),
        ),
    ],
)
async def test_coarse_motion_trace_is_version_independent(motion_version: str) -> None:
    start = 1_780_000_200.0
    assert await _aggregate(motion_version, start, start + 900, 900) == [50.0]


# -- HTTP: exports (proxied for the app) --------------------------------------

#: The export calls a client makes through the sidecar's catch-all proxy, per
#: release: (method, Frigate route template, concrete path, JSON body, body model).
EXPORT_CALLS: dict[str, tuple[tuple[str, str, str, dict[str, Any] | None, str | None], ...]] = {
    "v0.17.2": (
        ("POST", "/api/export/{camera_name}/start/{start_time}/end/{end_time}",
         f"/api/export/{CAMERA}/start/1780000000/end/1780000060",
         {"playback": "realtime", "source": "recordings", "name": "clip"},
         "ExportRecordingsBody"),
        ("GET", "/api/exports", "/api/exports", None, None),
        ("PATCH", "/api/export/{event_id}/rename", "/api/export/x1/rename",
         {"name": "renamed"}, "ExportRenameBody"),
        ("DELETE", "/api/export/{event_id}", "/api/export/x1", None, None),
    ),
    "v0.18.2": (
        ("POST", "/api/export/{camera_name}/start/{start_time}/end/{end_time}",
         f"/api/export/{CAMERA}/start/1780000000/end/1780000060",
         {"source": "recordings", "name": "clip"},
         "ExportRecordingsBody"),
        ("GET", "/api/exports", "/api/exports", None, None),
        ("PATCH", "/api/export/{event_id}/rename", "/api/export/x1/rename",
         {"name": "renamed"}, "ExportRenameBody"),
        ("POST", "/api/exports/delete", "/api/exports/delete", {"ids": ["x1"]},
         "ExportBulkDeleteBody"),
    ),
}


def test_export_delete_moved_in_018() -> None:
    """The one export call whose shape changed: 0.18 removed
    `DELETE /api/export/{id}` for a bulk `POST /api/exports/delete`. The
    sidecar never calls either; a client that still sends the DELETE gets
    Frigate's 405 relayed through the proxy (docs/frigate-compatibility.md)."""
    assert ("DELETE", "/api/export/{event_id}") in _routes("v0.17.2")
    assert ("DELETE", "/api/export/{event_id}") not in _routes("v0.18.2")
    assert ("POST", "/api/exports/delete") in _routes("v0.18.2")


class _StubResponse:
    status_code = 200
    headers = httpx.Headers({"content-type": "application/json"})

    async def aiter_raw(self) -> Any:
        yield b"{}"

    async def aclose(self) -> None:
        return None


class _RecordingAsyncClient:
    """Stand-in for the proxy's httpx.AsyncClient (as in test_proxy.py)."""

    requests: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        req = {"method": method, "url": url, **kwargs}
        _RecordingAsyncClient.requests.append(req)
        return req

    async def send(self, req: dict[str, Any], stream: bool = False) -> _StubResponse:
        return _StubResponse()

    async def aclose(self) -> None:
        return None


def test_export_calls_pass_through_the_proxy(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    frigate_db: tuple[Path, dict[str, float]],
) -> None:
    path, _t = frigate_db
    monkeypatch.setattr(httpx, "AsyncClient", _RecordingAsyncClient)
    _RecordingAsyncClient.requests = []
    client = TestClient(create_app(_settings(tmp_path, path, proxy=ProxySection(enabled=True))))
    routes = _routes(version)
    for method, route, concrete, body, model in EXPORT_CALLS[version]:
        assert (method, route) in routes, f"{version} has no {method} {route}"
        if body is not None:
            assert model is not None
            assert set(body) <= _model_fields(version, model)
        r = client.request(method, concrete, json=body)
        assert r.status_code == 200, (method, concrete, r.text)
        sent = _RecordingAsyncClient.requests[-1]
        assert sent["method"] == method
        assert sent["url"] == f"http://frigate.test:8971{concrete}"
        assert (json.loads(sent["content"]) if sent["content"] else None) == body


# -- MQTT and config file -----------------------------------------------------

#: Payloads each release publishes on `frigate/available`
#: (frigate/comms/mqtt.py): 0.18 adds a retained "stopped" on clean shutdown.
AVAILABLE_PAYLOADS = {
    "v0.17.2": {"online": True, "offline": False},
    "v0.18.2": {"online": True, "offline": False, "stopped": False},
}


def test_frigate_available_payloads(version: str, tmp_path: Path) -> None:
    engine = PushEngine(
        db_path=str(tmp_path / "push.db"), transport=LogTransport(), server_id="s1"
    )
    sub = MqttReviewSubscriber(PushSection(), engine, frigate_base_url="http://frigate.test:5000")
    for payload, online in AVAILABLE_PAYLOADS[version].items():
        msg = SimpleNamespace(topic="frigate/available", payload=payload.encode())
        sub.on_message(None, None, msg)
        assert sub.frigate_online is online, (version, payload)


def test_zone_config_shapes(version: str, tmp_path: Path) -> None:
    """`ZoneConfig` in frigate/config/camera/zone.py: `coordinates` is a
    string or a list of "x,y" strings in both releases; 0.18 adds `enabled`."""
    cfg = tmp_path / "config.yml"
    lines = [
        "cameras:",
        f"  {CAMERA}:",
        "    zones:",
        "      walkway:",
        "        coordinates: 0.3,0.6,0.6,0.6,0.6,0.9,0.3,0.9",
        "      porch:",
        "        coordinates:",
        "          - 0.3,0.6",
        "          - 0.6,0.6",
        "          - 0.6,0.9",
        "          - 0.3,0.9",
    ]
    if version >= "v0.18":
        lines += [
            "      garden:",
            "        enabled: false",
            "        coordinates: 0.3,0.6,0.6,0.6,0.6,0.9,0.3,0.9",
        ]
    cfg.write_text("\n".join(lines) + "\n")
    zones = load_camera_zones(cfg)[CAMERA]
    expected = {"walkway", "porch"} | ({"garden"} if version >= "v0.18" else set())
    assert {z["name"] for z in zones} == expected
    # A box whose bottom-centre (0.45, 0.8) sits inside every polygon.
    assert zones_containing_box(zones, [0.40, 0.50, 0.10, 0.30]) == {"walkway", "porch"}
