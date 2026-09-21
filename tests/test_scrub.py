"""Tests for the `/v1` scrub-cache read layer (routes/scrub.py).

Covers docs/scrub-cache-and-proxy-spec.md §4.1 (capabilities), §4.4
(coverage -- latest_segment_end vs authoritative_through), and the §3.2
auth requirement.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from marcellus import auth, db
from marcellus.config import FrigateSection, ScrubSection, Settings, SidecarSection
from marcellus.frigate_api import FrigateAPIError
from marcellus.routes import scrub as scrub_routes
from marcellus.scrub import grid
from marcellus.server import create_app

RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id            VARCHAR(30) PRIMARY KEY,
    camera        VARCHAR(20) NOT NULL,
    path          VARCHAR(255) NOT NULL,
    start_time    DATETIME NOT NULL,
    end_time      DATETIME NOT NULL,
    duration      REAL NOT NULL,
    objects       INTEGER,
    motion        INTEGER,
    segment_size  REAL NOT NULL,
    dBFS          INTEGER,
    regions       INTEGER
);
"""

EVENT_SCHEMA = """
CREATE TABLE event (
    id           TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    start_time   REAL NOT NULL,
    end_time     REAL,
    score        REAL,
    top_score    REAL,
    zones        TEXT,
    data         TEXT
);
"""


def _skip_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the central Frigate-session gate (marcellus.auth)."""

    async def _noop(app: object, cookie: str, *, ttl_s: float | None = None) -> None:
        return None

    monkeypatch.setattr(auth, "validate_frigate_session", _noop)


@pytest.fixture
def frigate_db_with_recordings(tmp_path: Path) -> Path:
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
    conn.executescript(EVENT_SCHEMA)
    now = time.time()
    # 10s segments, contiguous, ending 6.2s before "now" (matches measured
    # publish lag -- the last segment's end_time trails wall-clock).
    seg_end = now - 6.2
    for i in range(20):
        end = seg_end - i * 10
        start = end - 10
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, "
            "duration, segment_size) VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"seg{i}", f"/media/frigate/recordings/x/{i}.mp4", start, end),
        )
    # A gap: no segments between -400 and -300 relative to seg_end.
    # One closed event, one still-in-progress (end_time NULL -> "end": null).
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones) "
        "VALUES ('ev1', 'doorbell', 'person', ?, ?, 0.9, 0.92, '[\"front\"]')",
        (now - 200, now - 180),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones) "
        "VALUES ('ev2', 'doorbell', 'car', ?, NULL, 0.8, 0.85, '[]')",
        (now - 50,),
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def client(frigate_db_with_recordings: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_with_recordings,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    return TestClient(create_app(settings))


def test_capabilities_no_auth_required(client: TestClient) -> None:
    r = client.get("/v1/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert body["scrub_cache"]["enabled"] is False
    assert body["scrub_cache"]["generated"] is False
    assert "http2" not in body  # dropped per review finding (§4.1)
    assert body["proxy"]["enabled"] is True


def test_coverage_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/coverage/doorbell", params={"start": 0, "end": time.time()})
    assert r.status_code == 401


def test_coverage_unknown_camera(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    r = client.get(
        "/v1/coverage/not-a-camera",
        params={"start": 0, "end": time.time()},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "camera_unknown"


def test_coverage_merges_intervals_and_splits_authoritative_from_latest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    now = time.time()
    r = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["recorded"], "contiguous segments should merge into recorded intervals"
    # latest_segment_end is the frozen diagnostic; authoritative_through tracks
    # wall-clock and is what the client should trust (docs spec §4.4 finding 4).
    assert body["latest_segment_end"] < body["authoritative_through"]
    assert abs(body["authoritative_through"] - (now - db.DEFAULT_PUBLISH_LAG_S)) < 1.0
    # `retention_days` here meant the *scrub cache's* horizon on an endpoint
    # about what Frigate recorded -- and §4.2 tells clients to read that field
    # as "past this, it will never exist", which on this deployment would say
    # recordings stop at 4 days when the motion band runs to 8.
    assert "retention_days" not in body
    assert body["scrub_retention_days"] == 4
    assert body["queried"] == [now - 300, now]


def test_authoritative_through_survives_camera_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera with no new segments for a while: latest_segment_end stays
    frozen, but authoritative_through keeps advancing at wall-clock rate --
    their divergence is the outage signal (docs spec §4.4 finding 4)."""

    _skip_auth(monkeypatch)

    now = time.time()
    r1 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    later = now + 120  # camera has been silent for 2 more minutes
    # Simulate by directly calling the DB helper with a later `now`, since the
    # fixture data doesn't change -- latest_segment_end must not move while
    # authoritative_through does.
    conn = db.open_frigate_ro(client.app.state.settings.frigate.db_path)  # type: ignore[attr-defined]
    try:
        r2 = db.recording_coverage(conn, "doorbell", now - 300, now, now=later)
    finally:
        conn.close()

    r1_body = r1.json()
    assert r2["latest_segment_end"] == pytest.approx(r1_body["latest_segment_end"])
    assert r2["authoritative_through"] > r1_body["authoritative_through"]


def _seed_bucket(
    sidecar_db_path: Path, camera: str, start: float, end: float, interval: float
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        db.upsert_scrub_bucket(
            conn, camera=camera, start_ts=start, end_ts=end, interval_s=interval,
            width=320, height=180, generated_through=end, complete=True,
        )
        conn.commit()
    finally:
        conn.close()


def test_scrub_coverage_interval_contract_and_retention_distinction(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    # Recent 1fps bucket.
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 1.0)
    # Older, coarser (aged) bucket.
    _seed_bucket(sidecar_db_path, "doorbell", now - 3600, now - 3000, 5.0)

    r = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": now - 4000, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    intervals = {b["interval"] for b in body["buckets"]}
    assert intervals == {1.0, 5.0}
    assert body["generated_through"] == pytest.approx(now - 40)
    assert body["retention_days"] == 4

    # A span entirely past retention_days has no buckets at all -- distinct
    # from "lagging" only because the client compares queried range against
    # retention_days (both present in the response), per spec §4.2.
    ancient_start = now - 20 * 86400
    r2 = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": ancient_start, "end": ancient_start + 100},
        headers={"cookie": "session=fake"},
    )
    body2 = r2.json()
    assert body2["buckets"] == []
    queried_age_days = (now - ancient_start) / 86400
    assert queried_age_days > body2["retention_days"]  # client's "will never exist" check


def test_scrub_coverage_excludes_derived_tiers_to_avoid_double_reporting(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Derived tiers (default `derived_intervals_s=[60.0, 300.0, 900.0,
    3600.0]`) span the whole retention window and deliberately overlap the
    recent/aged decode tiers in time. `/v1/scrub/{camera}/coverage` must not
    report all of them for the same span -- that would look like several
    times the actual coverage for one instant -- so it keeps its
    pre-derived-tier contract: one bucket per covered instant, drawn only
    from the recent/aged tiers."""
    _skip_auth(monkeypatch)
    now = time.time()
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 1.0)
    # Derived-tier buckets covering the exact same span, at two coarser
    # cadences -- these must be excluded from the response.
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 60.0)
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 300.0)

    r = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": now - 200, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    intervals = {b["interval"] for b in body["buckets"]}
    assert intervals == {1.0}, f"derived-tier buckets leaked into coverage: {intervals}"
    # generated_through must not be pulled ahead by a derived-tier bucket that
    # reaches further than the recent/aged tiers actually do.
    assert body["generated_through"] == pytest.approx(now - 40)


def test_reel_and_scrub_coverage_agree_on_derived_tier_exclusion(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/v1/reel`'s `frames` and `/v1/scrub/{camera}/coverage`'s `buckets` must
    exclude the same derived-tier rows for the same overlapping-tier window --
    they share `grid.exclude_derived_buckets` for exactly this reason."""
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    # A decode-tier bucket and a derived-tier bucket covering the exact same
    # span -- the derived one must vanish from both endpoints identically.
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 1.0)
    _seed_bucket(sidecar_db_path, "doorbell", now - 100, now - 40, 60.0)

    coverage_r = client.get(
        "/v1/scrub/doorbell/coverage",
        params={"start": now - 200, "end": now},
        headers={"cookie": "session=fake"},
    )
    reel_r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 200, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert coverage_r.status_code == reel_r.status_code == 200

    coverage_intervals = {b["interval"] for b in coverage_r.json()["buckets"]}
    reel_intervals = {f["interval"] for f in reel_r.json()["frames"]}
    assert coverage_intervals == {1.0}
    assert reel_intervals == coverage_intervals


def test_scrub_sheets_interval_filter(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`?interval=` restricts the sheet index to one tier -- e.g. a client
    building a whole-history scrubber wants only a derived tier's sheets for
    a window the recent/aged tiers also cover."""
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    start = 1_785_380_400.0

    def _write_sheet(interval: float, count: int) -> str:
        rel = grid.sheet_rel_path("doorbell", interval, start, count)
        out = cache_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10)).save(out)
        return rel

    conn = db.open_sidecar(sidecar_db_path)
    try:
        for interval, count in ((1.0, 40), (60.0, 4)):
            rel = _write_sheet(interval, count)
            db.upsert_scrub_sheet(
                conn, camera="doorbell", start_ts=start, interval_s=interval, cols=12, rows=8,
                cell_w=320, cell_h=180, count=count, path=rel, complete=False,
            )
        conn.commit()
    finally:
        conn.close()

    r_all = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    assert {s["interval"] for s in r_all.json()["sheets"]} == {1.0, 60.0}

    r_coarse = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200, "interval": 60.0},
        headers={"cookie": "session=fake"},
    )
    sheets = r_coarse.json()["sheets"]
    assert len(sheets) == 1
    assert sheets[0]["interval"] == 60.0
    assert sheets[0]["count"] == 4


def test_scrub_sheet_url_immutable_across_growing_count(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    start = 1_785_380_400.0

    def _write_sheet(count: int) -> str:
        rel = grid.sheet_rel_path("doorbell", 1.0, start, count)
        out = cache_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10)).save(out)
        return rel

    conn = db.open_sidecar(sidecar_db_path)
    try:
        for count in (12, 40):
            rel = _write_sheet(count)
            db.upsert_scrub_sheet(
                conn, camera="doorbell", start_ts=start, interval_s=1.0, cols=12, rows=8,
                cell_w=320, cell_h=180, count=count, path=rel, complete=(count == 96),
            )
        conn.commit()
    finally:
        conn.close()

    r_index = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    sheets = r_index.json()["sheets"]
    assert len(sheets) == 1
    assert sheets[0]["count"] == 40  # only the latest version is advertised
    assert sheets[0]["url"] == "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg"

    r12 = client.get(
        "/v1/scrub/doorbell/sheet/1785380400-1.0-12.jpg", headers={"cookie": "session=fake"}
    )
    r40 = client.get(
        "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg", headers={"cookie": "session=fake"}
    )
    assert r12.status_code == 200
    assert r40.status_code == 200
    # Two different generations of the same live sheet -> two different URLs
    # (differing count); both served unconditionally immutable (spec §4.3,
    # §11 test_scrub_sheet_url_immutable) -- no cache-freshness logic anywhere.
    assert r12.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r40.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_reel_bundle_shape(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        n = int((end - start) / scale)
        return [0.0] * n, False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    _seed_bucket(sidecar_db_path, "doorbell", now - 300, now - 200, 1.0)
    _seed_bucket(sidecar_db_path, "doorbell", now - 3600, now - 3000, 5.0)

    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    for key in ("queried", "recorded", "latest_segment_end", "authoritative_through",
                "frames", "motion", "events"):
        assert key in body

    assert body["queried"] == [now - 3700, now]
    assert isinstance(body["frames"], list)
    assert len(body["frames"]) == 2  # straddles the 1.0/5.0 thinning boundary
    assert isinstance(body["motion"]["values"], list)
    assert len(body["motion"]["values"]) == int(3700 / 10)  # zero-filled to full range
    assert "motion_unavailable" not in body  # success path omits the key entirely

    events_by_id = {e["id"]: e for e in body["events"]}
    assert events_by_id["ev1"]["end"] is not None
    assert events_by_id["ev2"]["end"] is None  # still-in-progress -> null, not omitted/placeholder


def test_reel_flags_motion_unavailable_when_frigate_api_fails(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frigate down must not masquerade as a genuinely quiet camera (§4.6):
    events/coverage still come from the DB and stay valid, but the motion
    trace is flagged so a flat line can be told apart from real silence."""
    _skip_auth(monkeypatch)

    async def _raising_activity_motion(
        client: object, base_url: str, camera: str, start: float, end: float, scale: float
    ) -> list[dict[str, object]]:
        raise FrigateAPIError("frigate api down")

    monkeypatch.setattr(scrub_routes, "async_activity_motion", _raising_activity_motion)

    now = time.time()
    _seed_bucket(sidecar_db_path, "doorbell", now - 300, now - 200, 1.0)

    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["motion_unavailable"] is True
    assert body["motion"]["values"] == [0.0] * int(3700 / 10)  # zero-filled, unchanged
    # DB-backed fields remain valid even though motion failed.
    assert "events" in body
    assert isinstance(body["events"], list)
    assert "recorded" in body


def test_motion_totalizes_full_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        from marcellus.scrub.motion import aggregate_motion

        # Only 9 of the requested buckets have "upstream" data -- simulates
        # the measured short-window cliff (§4.6).
        points = [(start + i * scale, 55.0) for i in range(9)]
        return aggregate_motion(points, start, end, scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    r = client.get(
        "/v1/motion/doorbell",
        params={"start": now - 1800, "end": now, "scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["values"]) == 30  # full 1800/60, not truncated
    assert body["values"][:9] == [55.0] * 9
    assert body["values"][9:] == [0.0] * 21
    assert "motion_unavailable" not in body  # success path omits the key entirely


def test_motion_flags_unavailable_when_frigate_api_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)

    async def _raising_activity_motion(
        client: object, base_url: str, camera: str, start: float, end: float, scale: float
    ) -> list[dict[str, object]]:
        raise FrigateAPIError("frigate api down")

    monkeypatch.setattr(scrub_routes, "async_activity_motion", _raising_activity_motion)

    now = time.time()
    r = client.get(
        "/v1/motion/doorbell",
        params={"start": now - 1800, "end": now, "scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["motion_unavailable"] is True
    assert body["values"] == [0.0] * 30  # zero-filled, unchanged


def test_highlights_use_frigate_label_vocabulary(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    body = r.json()
    reasons = {h["reason"] for h in body["highlights"]}
    assert reasons <= {"person", "car"}  # Frigate object labels, not a separate scheme
    for h in body["highlights"]:
        assert "score" in h


def test_unknown_v1_path_is_json_404_not_html(client: TestClient) -> None:
    r = client.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_coverage_etag_304(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    # Freeze time for this endpoint so authoritative_through (which otherwise
    # advances every call) doesn't change the body -- and with it the ETag --
    # between the two requests below.
    monkeypatch.setattr(scrub_routes.time, "time", lambda: now)
    r1 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake"},
    )
    etag = r1.headers["etag"]
    r2 = client.get(
        "/v1/coverage/doorbell",
        params={"start": now - 300, "end": now},
        headers={"cookie": "session=fake", "if-none-match": etag},
    )
    assert r2.status_code == 304


def test_motion_rejects_an_unbounded_series(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`values` is materialised, so (end-start)/scale is an allocation lever.

    Before this bound, scale=0.001 over a multi-day window asked for hundreds
    of millions of buckets and the process died allocating them.
    """
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/motion/doorbell",
        params={"start": 0, "end": now, "scale": 0.001},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


@pytest.mark.parametrize("scale", [0, -5])
def test_motion_rejects_non_positive_scale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, scale: float
) -> None:
    """These used to raise ValueError out of aggregate_motion -> a 500."""
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/motion/doorbell",
        params={"start": now - 100, "end": now, "scale": scale},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


def test_reel_rejects_an_unbounded_series(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": 0, "end": now, "motion_scale": 0.5},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


def test_coverage_rejects_inverted_range(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/v1/coverage was the one window endpoint that didn't check this."""
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/coverage/doorbell",
        params={"start": now, "end": now - 100},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


@pytest.mark.parametrize("path", ["coverage", "sheets"])
def test_scrub_endpoints_report_an_unknown_camera(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """These returned an empty result for a camera that doesn't exist, while
    /v1/coverage, /v1/reel and /v1/highlights all 404 `camera_unknown`."""
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        f"/v1/scrub/not-a-camera/{path}",
        params={"start": now - 100, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "camera_unknown"


def test_sheet_index_advertises_the_stored_extension(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With scrub.format=webp the sheet on disk is a .webp; the advertised URL
    (and hence the served content-type) has to match it."""
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    start = 1_785_380_400.0
    rel = grid.sheet_rel_path("doorbell", 1.0, start, 24, ".webp")
    out = cache_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (10, 10)).save(out)

    conn = db.open_sidecar(sidecar_db_path)
    try:
        db.upsert_scrub_sheet(
            conn, camera="doorbell", start_ts=start, interval_s=1.0, cols=12, rows=8,
            cell_w=320, cell_h=180, count=24, path=rel, complete=False,
        )
        conn.commit()
    finally:
        conn.close()

    r = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    url = r.json()["sheets"][0]["url"]
    assert url == "/v1/scrub/doorbell/sheet/1785380400-1.0-24.webp"

    img = client.get(url, headers={"cookie": "session=fake"})
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/webp"


@pytest.mark.parametrize(
    ("cycle_cost_s", "expected_sleep_s"),
    [(5.0, 15.0), (25.0, 0.0)],
)
def test_generation_loop_holds_its_tick_as_a_deadline(
    monkeypatch: pytest.MonkeyPatch, cycle_cost_s: float, expected_sleep_s: float
) -> None:
    """The tick is a deadline, not a sleep.

    Cadence is the floor on how stale the newest sprite cell can be, so the
    trailing-window pass has to start every `live_edge_interval_s` regardless of
    what history cost. A cycle that fits sleeps out the remainder; one that
    overruns sleeps nothing and the next pass starts immediately, rather than
    the loop adding an idle wait on top of an already-late tick.
    """
    import asyncio

    from marcellus import server

    settings = Settings(
        scrub=ScrubSection(generate_interval_s=60.0, live_edge_interval_s=20.0)
    )
    app = type("_App", (), {"state": type("_S", (), {"settings": settings})})()

    clock = {"monotonic": 1000.0}
    deadlines: list[float] = []
    sleeps: list[float] = []
    cycles = 0

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["monotonic"]

        @staticmethod
        def time() -> float:
            return 1_800_000_000.0  # wall clock; only the prune schedule reads it

    async def _fake_cycle(
        _settings: object, *, backfill_deadline: float | None = None, **kw: object
    ) -> list[dict[str, object]]:
        nonlocal cycles
        cycles += 1
        if cycles > 2:
            raise asyncio.CancelledError
        deadlines.append(backfill_deadline)  # type: ignore[arg-type]
        clock["monotonic"] += cycle_cost_s
        return [{"camera": "doorbell", "segments": 12}]

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("marcellus.scrub.generator.generate_cycle", _fake_cycle)
    monkeypatch.setattr(server.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(server, "time", _Clock)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server._scrub_generation_loop(app))  # type: ignore[arg-type]

    assert sleeps, "loop should always yield"
    assert sleeps[0] == pytest.approx(expected_sleep_s)
    # Backfill is handed the tick's own deadline, so it can never push the next
    # trailing-window pass out.
    assert deadlines[0] == pytest.approx(1020.0)


def test_generation_loop_tick_is_the_finer_of_the_two_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`generate_interval_s` stays meaningful as the ceiling, so a deployment
    that deliberately slowed generation down is not sped back up by the new
    knob's default."""
    import asyncio

    from marcellus import server

    settings = Settings(
        scrub=ScrubSection(generate_interval_s=10.0, live_edge_interval_s=20.0)
    )
    app = type("_App", (), {"state": type("_S", (), {"settings": settings})})()
    deadlines: list[float] = []

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return 500.0

        @staticmethod
        def time() -> float:
            return 1_800_000_000.0

    async def _fake_cycle(
        _settings: object, *, backfill_deadline: float | None = None, **kw: object
    ) -> list[dict[str, object]]:
        deadlines.append(backfill_deadline)  # type: ignore[arg-type]
        raise asyncio.CancelledError

    async def _fake_sleep(seconds: float) -> None:  # pragma: no cover - never reached
        return None

    monkeypatch.setattr("marcellus.scrub.generator.generate_cycle", _fake_cycle)
    monkeypatch.setattr(server.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(server, "time", _Clock)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server._scrub_generation_loop(app))  # type: ignore[arg-type]

    assert deadlines[0] == pytest.approx(510.0), "tick must be the 10s ceiling, not the 20s knob"


def test_generation_loop_skips_generation_below_free_space_floor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A full cache filesystem used to retry `generate_cycle` every tick
    forever, logging the same ENOSPC stack with no distinct signal and no
    backoff. Below `scrub.min_free_bytes`, the loop must skip generation
    (letting `prune` run so it can free space back up), must NOT advance
    `scrub_last_cycle` (that's what lets healthz staleness surface the
    condition), and must log the warning at most once per rate-limit window,
    not once per tick.
    """
    import asyncio
    from collections import namedtuple

    from marcellus import server

    cache_dir = tmp_path / "scrub"
    cache_dir.mkdir()
    settings = Settings(
        scrub=ScrubSection(
            cache_dir=cache_dir,
            generate_interval_s=10.0,
            live_edge_interval_s=10.0,
            prune_interval_s=0.0,
            min_free_bytes=2 * 1024 * 1024 * 1024,
        )
    )
    app = type("_App", (), {"state": type("_S", (), {"settings": settings})})()

    clock = {"monotonic": 1000.0}
    ticks = 0
    _Usage = namedtuple("_Usage", ["total", "used", "free"])

    class _Clock:
        @staticmethod
        def monotonic() -> float:
            return clock["monotonic"]

        @staticmethod
        def time() -> float:
            return 1_800_000_000.0

    async def _fake_cycle(*a: object, **kw: object) -> list[dict[str, object]]:
        raise AssertionError("generate_cycle must not run below the free-space floor")

    prune_calls = 0

    async def _fake_to_thread(fn: object, *a: object, **kw: object) -> dict[str, object]:
        nonlocal prune_calls
        prune_calls += 1
        return {}

    async def _fake_sleep(seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        clock["monotonic"] += 10.0
        if ticks >= 3:
            raise asyncio.CancelledError

    def _fake_disk_usage(path: object) -> object:
        # Well below the 2GB floor.
        return _Usage(total=10_000_000_000, used=9_999_000_000, free=1_000_000)

    monkeypatch.setattr("marcellus.scrub.generator.generate_cycle", _fake_cycle)
    monkeypatch.setattr(server.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(server.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(server, "time", _Clock)
    monkeypatch.setattr(server.shutil, "disk_usage", _fake_disk_usage)

    with (
        caplog.at_level("WARNING", logger="marcellus.server"),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(server._scrub_generation_loop(app))  # type: ignore[arg-type]

    assert not hasattr(app.state, "scrub_last_cycle"), (
        "floor-skip must not advance scrub_last_cycle -- that's what lets "
        "healthz's staleness check surface the low-disk condition"
    )
    assert prune_calls >= 1, "pruning must still run below the floor -- it's what frees space"
    warnings = [r for r in caplog.records if "below free-space floor" in r.message]
    assert len(warnings) == 1, "warning must be rate-limited, not logged once per tick"


# ----- /v1/highlights: score, ranking, clustering (§4.7) -----


def _seed_events(
    frigate_db: Path, camera: str, specs: list[tuple[float, float, str, float]]
) -> None:
    """specs: (start, end, label, top_score) -- score lives in `data`, as
    current Frigate writes it (the columns are NULL for every row)."""
    conn = sqlite3.connect(frigate_db)
    for i, (start, end, label, top) in enumerate(specs):
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, "
            "zones, data) VALUES (?, ?, ?, ?, ?, NULL, NULL, '[]', ?)",
            (f"h{i}", camera, label, start, end, json.dumps({"top_score": top, "score": top})),
        )
    conn.commit()
    conn.close()


def test_highlights_report_a_score_from_the_data_blob(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current Frigate leaves the score/top_score COLUMNS null and writes into
    `data`, so reading the column made `score` null on every highlight -- and
    an always-null field is one every consumer has to write a comment about."""
    _skip_auth(monkeypatch)
    now = time.time()
    _seed_events(frigate_db_with_recordings, "doorbell", [
        (now - 300, now - 290, "person", 0.91),
        (now - 200, now - 195, "car", 0.62),
    ])
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 50},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    scored = [h for h in r.json()["highlights"] if h["score"] is not None]
    assert len(scored) >= 2, "every seeded event should carry a score"
    assert all(0.0 <= h["score"] <= 1.0 for h in scored)


def test_reel_events_also_carry_a_score(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same column-vs-blob bug, same fix -- /v1/reel read the column too."""
    _skip_auth(monkeypatch)

    async def _no_motion(
        request: object, camera: str, s: float, e: float, sc: float
    ) -> tuple[list[float], bool]:
        return [], False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _no_motion)
    now = time.time()
    _seed_events(frigate_db_with_recordings, "doorbell", [(now - 100, now - 90, "person", 0.77)])
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 200, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    scored = [e for e in r.json()["events"] if e["score"] is not None]
    assert scored and any(abs(e["score"] - 0.77) < 1e-6 for e in scored)


def test_highlights_default_to_raw_events_newest_first(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default order is unchanged: a client scanning for adjacency depends on
    it, so ranking and clustering are both opt-in."""
    _skip_auth(monkeypatch)
    now = time.time()
    _seed_events(frigate_db_with_recordings, "doorbell", [
        (now - 300, now - 295, "person", 0.9),
        (now - 250, now - 245, "person", 0.5),
    ])
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 50},
        headers={"cookie": "session=fake"},
    )
    starts = [h["start"] for h in r.json()["highlights"]]
    assert starts == sorted(starts, reverse=True)
    assert all("events" not in h for h in r.json()["highlights"])


def test_highlights_cluster_runs_into_single_destinations(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One subject crossing frame emits several events -- measured, 40-50% of
    consecutive highlights are under 45s apart -- so an unclustered "jump to
    next" presses the same person four times."""
    _skip_auth(monkeypatch)
    now = time.time()
    base = now - 4000
    _seed_events(frigate_db_with_recordings, "doorbell", [
        (base, base + 10, "person", 0.60),
        (base + 20, base + 30, "person", 0.95),   # same subject, 10s gap
        (base + 40, base + 50, "person", 0.70),   # same subject, 10s gap
        (base + 900, base + 910, "car", 0.80),    # a genuinely separate visit
    ])
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 50, "cluster_s": 45},
        headers={"cookie": "session=fake"},
    )
    got = [h for h in r.json()["highlights"] if base - 1 <= h["start"] <= base + 1000]
    assert len(got) == 2, f"expected 2 destinations, got {len(got)}"
    newest, run = got[0], got[1]
    assert newest["reason"] == "car" and newest["events"] == 1
    assert run["events"] == 3
    assert run["start"] == pytest.approx(base)          # earliest start of the run
    assert run["end"] == pytest.approx(base + 50)       # latest end
    assert run["score"] == pytest.approx(0.95)          # most confident member


def test_highlights_can_rank_by_score(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.7 calls these ranked; `order=score` is what makes that true without
    reordering the response for consumers that depend on time order."""
    _skip_auth(monkeypatch)
    now = time.time()
    _seed_events(frigate_db_with_recordings, "doorbell", [
        (now - 300, now - 295, "person", 0.42),
        (now - 250, now - 245, "car", 0.99),
        (now - 200, now - 195, "person", 0.71),
    ])
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 50, "order": "score"},
        headers={"cookie": "session=fake"},
    )
    scores = [h["score"] for h in r.json()["highlights"] if h["score"] is not None]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(0.99)


@pytest.mark.parametrize(
    ("params", "why"),
    [({"order": "sideways"}, "unknown order"), ({"cluster_s": -5}, "negative window")],
)
def test_highlights_reject_bad_parameters(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, params: dict, why: str
) -> None:
    _skip_auth(monkeypatch)
    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": time.time(), **params},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400, why
    assert r.json()["detail"]["error"] == "bad_range"


def test_clustering_keeps_an_in_progress_destination_open(
    client: TestClient, frigate_db_with_recordings: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If any member is still running the destination has no end, for the same
    reason events[].end stays null in /v1/reel: a synthesised end asserts an
    exit that hasn't happened."""
    _skip_auth(monkeypatch)
    now = time.time()
    base = now - 5000
    conn = sqlite3.connect(frigate_db_with_recordings)
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones, data)"
        " VALUES ('c0', 'doorbell', 'person', ?, ?, NULL, NULL, '[]', ?)",
        (base, base + 10, json.dumps({"top_score": 0.5})),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, zones, data)"
        " VALUES ('c1', 'doorbell', 'person', ?, NULL, NULL, NULL, '[]', ?)",
        (base + 20, json.dumps({"top_score": 0.8})),
    )
    conn.commit()
    conn.close()

    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 50, "cluster_s": 45},
        headers={"cookie": "session=fake"},
    )
    got = [h for h in r.json()["highlights"] if base - 1 <= h["start"] <= base + 1000]
    assert len(got) == 1 and got[0]["events"] == 2
    assert got[0]["end"] is None
    assert got[0]["score"] == pytest.approx(0.8)


def test_capabilities_hides_renamed_camera_ghosts(
    frigate_db_with_recordings: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    # Buckets cached under a camera's pre-rename name must not be advertised:
    # the app would list a dead camera and scrub against a frozen cache.
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras:\n  doorbell: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_with_recordings,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=True, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    now = time.time()
    _seed_bucket(sidecar_db_path, "doorbell", now - 600, now, 5.0)
    _seed_bucket(sidecar_db_path, "old-name", now - 600, now, 5.0)
    c = TestClient(create_app(settings))
    body = c.get("/v1/capabilities").json()
    assert body["scrub_cache"]["cameras"] == ["doorbell"]


# --- Reel identity + severity (the fields the web reel draws) ---------------
#
# The reel stopped being a colour-coded tick strip and started answering "who
# was here, in which zone, and did Frigate call it an alert". Two of those
# answers come from columns/tables that older Frigate builds may not have, so
# every one of them is exercised twice: present, and absent.

FULL_EVENT_SCHEMA = """
CREATE TABLE event (
    id           TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    start_time   REAL NOT NULL,
    end_time     REAL,
    score        REAL,
    top_score    REAL,
    zones        TEXT,
    sub_label    TEXT,
    has_clip     INTEGER,
    has_snapshot INTEGER,
    data         TEXT
);
"""

REVIEWSEGMENT_SCHEMA = """
CREATE TABLE reviewsegment (
    id         TEXT PRIMARY KEY,
    camera     TEXT NOT NULL,
    start_time REAL NOT NULL,
    end_time   REAL,
    severity   TEXT NOT NULL,
    thumb_path TEXT,
    data       TEXT
);
"""


@pytest.fixture
def rich_client(
    tmp_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    """A Frigate DB carrying everything a current build carries."""
    _skip_auth(monkeypatch)
    p = tmp_path / "frigate-rich.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
    conn.executescript(FULL_EVENT_SCHEMA)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    now = time.time()
    conn.execute(
        "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
        "segment_size) VALUES ('s0', 'alley-wide', '/x.mp4', ?, ?, 10.0, 5.0)",
        (now - 300, now - 290),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
        "sub_label, has_clip, has_snapshot) VALUES ('e1', 'alley-wide', 'package', ?, ?, "
        "0.91, '[\"parking_area\",\"charger\"]', 'amazon', 1, 1)",
        (now - 200, now - 150),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
        "sub_label, has_clip, has_snapshot) VALUES ('e2', 'alley-wide', 'person', ?, NULL, "
        "0.88, '[\"alley\"]', NULL, 0, 1)",
        (now - 100,),
    )
    # e3 carries a path_data blob: walks x 0.2->0.4 over 10s, holds still for
    # 16s (a dwell), then walks on to 0.7. 40 points, 40 seconds.
    path = []
    t0 = now - 90
    for i in range(10):
        path.append([[0.2 + 0.02 * i, 0.5], t0 + i])
    for i in range(16):
        path.append([[0.4, 0.5], t0 + 10 + i])
    for i in range(14):
        path.append([[0.4 + 0.02 * i, 0.5], t0 + 26 + i])
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
        "sub_label, has_clip, has_snapshot, data) VALUES ('e3', 'alley-wide', 'person', ?, ?, "
        "NULL, '[]', NULL, 1, 1, ?)",
        (t0, t0 + 40, json.dumps({"top_score": 0.9, "path_data": path})),
    )
    # e4 on ANOTHER camera, same label as e3, starting 12s after e3 -- the
    # continuation target.
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
        "sub_label, has_clip, has_snapshot) VALUES ('e4', 'alley-east', 'person', ?, ?, "
        "0.8, '[]', NULL, 1, 1)",
        (t0 + 12, t0 + 30),
    )
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES ('r1', 'alley-wide', ?, ?, 'alert', ?)",
        (now - 205, now - 145,
         json.dumps({"objects": ["package"], "zones": ["charger"], "detections": ["e1"]})),
    )
    conn.execute(
        "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
        "VALUES ('r2', 'alley-wide', ?, NULL, 'detection', ?)",
        (now - 100, json.dumps({"objects": ["person"], "zones": ["alley"]})),
    )
    conn.commit()
    conn.close()

    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=p
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    return TestClient(create_app(settings))


def _rich_reel(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> dict:
    async def _no_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [], False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _no_motion)
    now = time.time()
    r = client.get(
        "/v1/reel/alley-wide",
        params={"start": now - 600, "end": now, "motion_scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_reel_events_carry_identity_and_media_flags(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sub_label is the whole point of the identification camera; has_clip is
    the difference between a track you can open and one you cannot."""
    events = {e["id"]: e for e in _rich_reel(rich_client, monkeypatch)["events"]}

    assert events["e1"]["sub_label"] == "amazon"
    assert events["e1"]["has_clip"] is True
    assert events["e1"]["has_snapshot"] is True
    # Zones already travelled; assert the order survives, because the reel
    # shows zones[0] on a selected track.
    assert events["e1"]["zones"] == ["parking_area", "charger"]

    assert events["e2"]["sub_label"] is None
    assert events["e2"]["has_clip"] is False
    assert events["e2"]["has_snapshot"] is True


def test_reel_events_survive_a_frigate_without_the_new_columns(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The minimal schema has no sub_label/has_clip/has_snapshot at all. A reel
    without sub-labels is the right degradation; a 500 is not."""
    _skip_auth(monkeypatch)

    async def _no_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [], False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _no_motion)
    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 600, "end": now, "motion_scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    for ev in r.json()["events"]:
        assert ev["sub_label"] is None
        assert ev["has_clip"] is False
        assert ev["has_snapshot"] is False
        # No `data` column -> no trajectory; the field is null, never absent.
        assert ev["path"] is None
        assert "continues" in ev


def test_reel_events_carry_a_path_summary(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift is decimated (thousands of stored points -> <=16 + exit) and
    dwell finds the stayed-put stretch; events without a blob get null."""
    events = {e["id"]: e for e in _rich_reel(rich_client, monkeypatch)["events"]}

    path = events["e3"]["path"]
    assert path is not None
    assert 2 <= len(path["drift"]) <= 17
    # Timestamps ascend and x starts where the seeded walk starts.
    ts = [p[0] for p in path["drift"]]
    assert ts == sorted(ts)
    assert path["drift"][0][1] == pytest.approx(0.2, abs=0.03)
    # The 16 s hold at x=0.4 is one dwell span of roughly that length.
    assert len(path["dwell"]) == 1
    d0, d1 = path["dwell"][0]
    assert d1 - d0 == pytest.approx(16.0, abs=2.5)

    assert events["e1"]["path"] is None  # no data blob seeded


def test_reel_events_link_their_cross_camera_continuation(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """e4 (alley-east, same label) starts 12 s into e3 -- that is a visit
    continuing on the next camera, and the link carries the target's start."""
    events = {e["id"]: e for e in _rich_reel(rich_client, monkeypatch)["events"]}

    cont = events["e3"]["continues"]
    assert cont is not None
    assert cont["camera"] == "alley-east"
    assert cont["event_id"] == "e4"
    assert cont["start"] == pytest.approx(events["e3"]["start"] + 12, abs=0.01)

    # e1 is a package; no other-camera package exists -> no continuation.
    assert events["e1"]["continues"] is None


def test_reel_continuation_start_is_on_the_target_cameras_clock(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offsets are per camera: the link's start must carry the TARGET
    camera's record-clock shift, because that is the time the app scrubs to
    after switching."""
    from marcellus import db as db_mod

    app = rich_client.app
    sconn = db_mod.open_sidecar(app.state.settings.sidecar.db_path)
    try:
        db_mod.set_event_clock_offset(sconn, "alley-east", -5000)
    finally:
        sconn.close()
    scrub_routes.invalidate_event_clock_offsets()
    try:
        events = {e["id"]: e for e in _rich_reel(rich_client, monkeypatch)["events"]}
        cont = events["e3"]["continues"]
        assert cont is not None
        # alley-wide has no offset; alley-east is shifted -5 s.
        assert cont["start"] == pytest.approx(events["e3"]["start"] + 12 - 5.0, abs=0.01)
    finally:
        scrub_routes.invalidate_event_clock_offsets()


def test_reel_body_is_deterministic_for_etag(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reel is ETagged on content; the path/continues enrichment must not
    introduce per-call instability or every poll busts the client cache."""
    first = _rich_reel(rich_client, monkeypatch)
    second = _rich_reel(rich_client, monkeypatch)
    assert first["events"] == second["events"]


def test_reel_reviews_carry_severity_and_the_join_back_to_events(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Severity is the one signal that separates routine from important on this
    fleet -- scores cannot, being bimodal and high. `detections` is what lets a
    client answer "which tracks caused this alert" without a second query."""
    reviews = {r["id"]: r for r in _rich_reel(rich_client, monkeypatch)["reviews"]}

    assert reviews["r1"]["severity"] == "alert"
    assert reviews["r1"]["zones"] == ["charger"]
    assert reviews["r1"]["objects"] == ["package"]
    assert reviews["r1"]["detections"] == ["e1"]

    assert reviews["r2"]["severity"] == "detection"
    # No `detections` key in the blob -> empty list, never None: the reel
    # iterates it.
    assert reviews["r2"]["detections"] == []


def test_reel_review_end_stays_null_for_an_open_segment(
    rich_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as events[].end (§4.5): null means "has not closed", and
    synthesising a timestamp would draw a segment that ended when it did not."""
    reviews = {r["id"]: r for r in _rich_reel(rich_client, monkeypatch)["reviews"]}
    assert reviews["r1"]["end"] is not None
    assert reviews["r2"]["end"] is None


def test_reel_reviews_are_empty_without_a_reviewsegment_table(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Frigate too old to have review segments gets a reel with no severity
    spine, not a broken endpoint."""
    _skip_auth(monkeypatch)

    async def _no_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [], False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _no_motion)
    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 600, "end": now, "motion_scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    assert r.json()["reviews"] == []


def test_reel_reviews_tolerate_a_malformed_data_blob(
    tmp_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data` is opaque JSON written by Frigate. A row that is not a JSON object
    must not take the endpoint down -- same posture as the zones decode."""
    _skip_auth(monkeypatch)
    p = tmp_path / "frigate-bad.db"
    conn = sqlite3.connect(p)
    conn.executescript(RECORDINGS_SCHEMA)
    conn.executescript(FULL_EVENT_SCHEMA)
    conn.executescript(REVIEWSEGMENT_SCHEMA)
    now = time.time()
    conn.execute(
        "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
        "segment_size) VALUES ('s0', 'street', '/x.mp4', ?, ?, 10.0, 5.0)",
        (now - 300, now - 290),
    )
    for rid, blob in (("bad1", "not json at all"), ("bad2", "[1, 2, 3]"), ("bad3", None)):
        conn.execute(
            "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
            "VALUES (?, 'street', ?, ?, 'detection', ?)",
            (rid, now - 200, now - 190, blob),
        )
    conn.commit()
    conn.close()

    fake_config = tmp_path / "c.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=fake_config, db_path=p),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    tc = TestClient(create_app(settings))

    async def _no_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [], False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _no_motion)
    r = tc.get(
        "/v1/reel/street",
        params={"start": now - 600, "end": now, "motion_scale": 60},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    assert [rv["objects"] for rv in r.json()["reviews"]] == [[], [], []]


def test_event_times_are_shifted_by_annotation_offset(
    frigate_db_with_recordings: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events land on the record clock: `detect.annotation_offset` (ms) is added
    to start/end in /v1/reel and /v1/highlights, and window bounds are shifted
    the other way so filtering is consistent. Frames/coverage in the same
    responses are already record-clock; without this shift the event lanes sit
    visibly beside the pictures they describe."""
    fake_config = tmp_path / "frigate-config-offset.yml"
    fake_config.write_text(
        "cameras:\n  doorbell:\n    detect:\n      annotation_offset: -5000\n"
    )
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_with_recordings,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp_path / "scrub"),
    )
    client = TestClient(create_app(settings))
    _skip_auth(monkeypatch)
    scrub_routes._annotation_offset_cache.clear()

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    conn = sqlite3.connect(frigate_db_with_recordings)
    conn.row_factory = sqlite3.Row
    raw = {
        r["id"]: (r["start_time"], r["end_time"])
        for r in conn.execute("SELECT * FROM event").fetchall()
    }
    conn.close()

    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    events = {e["id"]: e for e in r.json()["events"]}
    assert events["ev1"]["start"] == pytest.approx(raw["ev1"][0] - 5.0)
    assert events["ev1"]["end"] == pytest.approx(raw["ev1"][1] - 5.0)
    assert events["ev2"]["end"] is None  # in-progress stays null, never shifted

    r = client.get(
        "/v1/highlights/doorbell",
        params={"before": now, "limit": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    starts = sorted(h["start"] for h in r.json()["highlights"])
    expected = sorted(s - 5.0 for (s, _e) in raw.values())
    assert starts == pytest.approx(expected)


def test_event_times_unshifted_without_annotation_offset(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No annotation_offset in the config -> byte-identical behavior."""
    _skip_auth(monkeypatch)
    scrub_routes._annotation_offset_cache.clear()

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    events = {e["id"]: e for e in r.json()["events"]}
    assert events["ev1"]["start"] == pytest.approx(now - 200, abs=5)


def test_reel_honours_sidecar_applied_offset(
    client: TestClient,
    frigate_db_with_recordings: Path,
    sidecar_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config annotation_offset, but a Settings-page apply -> same shift."""
    _skip_auth(monkeypatch)
    scrub_routes._annotation_offset_cache.clear()
    conn = db.open_sidecar(sidecar_db_path)
    db.set_event_clock_offset(conn, "doorbell", -5000)
    conn.close()
    scrub_routes.invalidate_event_clock_offsets()

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)

    now = time.time()
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": now - 3700, "end": now, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    events = {e["id"]: e for e in r.json()["events"]}
    assert events["ev1"]["start"] == pytest.approx(now - 205, abs=5)


def _publish_sheet(
    cache_dir: Path, sidecar_db_path: Path, *, camera: str = "doorbell",
    start: float = 1_785_380_400.0, interval: float = 1.0, count: int = 24,
    body: bytes = b"x" * 500,
) -> str:
    rel = grid.sheet_rel_path(camera, interval, start, count)
    out = cache_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(body)
    conn = db.open_sidecar(sidecar_db_path)
    try:
        db.upsert_scrub_sheet(
            conn, camera=camera, start_ts=start, interval_s=interval, cols=12, rows=8,
            cell_w=320, cell_h=180, count=count, path=rel, complete=False,
        )
        conn.commit()
    finally:
        conn.close()
    return rel


def test_sheet_image_range_request(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    _publish_sheet(cache_dir, sidecar_db_path, body=b"y" * 500)
    url = "/v1/scrub/doorbell/sheet/1785380400-1.0-24.jpg"

    r_range = client.get(url, headers={"cookie": "session=fake", "Range": "bytes=0-99"})
    assert r_range.status_code == 206
    assert r_range.headers["content-range"] == "bytes 0-99/500"
    assert len(r_range.content) == 100
    assert r_range.headers["accept-ranges"] == "bytes"

    r_bad = client.get(
        url, headers={"cookie": "session=fake", "Range": "bytes=10000-20000"}
    )
    assert r_bad.status_code == 416

    r_full = client.get(url, headers={"cookie": "session=fake"})
    assert r_full.status_code == 200
    assert r_full.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_sheet_image_vanishing_after_exists_check_is_404(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file can be unlinked between the `exists()` check and the response
    being built; the stat seam introduced for this must turn that into a
    clean 404 instead of a 500 (Starlette's FileResponse otherwise raises
    RuntimeError deep inside the ASGI send, not FileNotFoundError, and not
    until after the route has already returned)."""
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    rel = _publish_sheet(cache_dir, sidecar_db_path)
    sheet_path = cache_dir / rel
    url = "/v1/scrub/doorbell/sheet/1785380400-1.0-24.jpg"

    # Vanish it between the route's `exists()` check and its own explicit
    # `stat()` call -- that's the window the fix closes; a vanish during the
    # ASGI send itself (after `FileResponse` is already built) is the residual
    # risk the spec accepts as unclosable for a file-backed response. Scoped
    # to this one path so it doesn't disturb every other `exists()` check the
    # request touches (e.g. Frigate's own DB file).
    real_exists = Path.exists

    def _exists_then_unlink(self: Path, *a: object, **kw: object) -> bool:
        result = real_exists(self, *a, **kw)
        if result and self == sheet_path:
            self.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(Path, "exists", _exists_then_unlink)

    r = client.get(url, headers={"cookie": "session=fake"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_generated"


def test_sheets_index_etag_and_304(
    client: TestClient, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    cache_dir = client.app.state.settings.scrub.cache_dir  # type: ignore[attr-defined]
    start = 1_785_380_400.0
    _publish_sheet(cache_dir, sidecar_db_path, start=start, count=24)

    r1 = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    assert r1.status_code == 200
    assert r1.headers["cache-control"] == "no-cache"
    etag = r1.headers["etag"]

    r2 = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake", "if-none-match": etag},
    )
    assert r2.status_code == 304
    assert r2.content == b""

    # Publishing another sheet changes the index body -> the ETag changes.
    _publish_sheet(cache_dir, sidecar_db_path, start=start, count=40)
    r3 = client.get(
        "/v1/scrub/doorbell/sheets",
        params={"start": start, "end": start + 200},
        headers={"cookie": "session=fake"},
    )
    assert r3.headers["etag"] != etag


def test_reel_route_matches_compose_reel_byte_for_byte(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M4 refactor regression: `reel()` is now a thin `compose_reel` wrapper
    -- assert the route's JSON is byte-identical (via `sort_keys` dumps) to
    calling `compose_reel` directly with the same params, so a future change
    to either can't silently diverge them."""
    _skip_auth(monkeypatch)

    async def _fake_motion(
        settings: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fake_motion)
    frozen_now = time.time()
    monkeypatch.setattr(scrub_routes.time, "time", lambda: frozen_now)

    now = frozen_now
    start, end = now - 3700, now
    r = client.get(
        "/v1/reel/doorbell",
        params={"start": start, "end": end, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200
    route_body = r.json()

    # Build a real Request against the same TestClient's app so
    # `request.app.state.scrub_known_cameras_cache` etc. behave identically.
    import asyncio as _asyncio

    from fastapi import Request as _Request

    from marcellus.routes.scrub import compose_reel as _compose_reel

    async def _run() -> dict:
        scope = {
            "type": "http",
            "app": client.app,
            "headers": [],
            "method": "GET",
            "path": "/v1/reel/doorbell",
        }
        req = _Request(scope)
        settings = client.app.state.settings
        return await _compose_reel(req, settings, "doorbell", start, end, 10.0)

    direct_body = _asyncio.run(_run())

    assert json.dumps(route_body, sort_keys=True) == json.dumps(direct_body, sort_keys=True)
