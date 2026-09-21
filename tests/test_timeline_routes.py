"""`/v1/timeline` (routes/timeline.py, docs/encounters.md "Global
timeline"): multi-camera reel composition + observation/encounter overlay."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import auth, db
from marcellus.config import EncountersSection, FrigateSection, Settings, SidecarSection
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.observations import Direction
from marcellus.encounters.store import upsert_atom
from marcellus.routes import scrub as scrub_routes
from marcellus.routes import timeline as timeline_routes
from marcellus.server import create_app

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


def _skip_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(app: object, cookie: str, *, ttl_s: float | None = None) -> None:
        return None

    monkeypatch.setattr(auth, "validate_frigate_session", _noop)


def _fake_motion(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fn(
        request: object, camera: str, start: float, end: float, scale: float
    ) -> tuple[list[float], bool]:
        return [0.0] * int((end - start) / scale), False

    monkeypatch.setattr(scrub_routes, "_fetch_and_aggregate_motion", _fn)


@pytest.fixture
def multi_camera_frigate_db(tmp_path: Path) -> Path:
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(EVENT_SCHEMA)
    conn.executescript(RECORDINGS_SCHEMA)
    now = time.time()
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones) "
        "VALUES ('ev1', 'alley-wide', 'person', ?, ?, 0.9, '[]')",
        (now - 200, now - 180),
    )
    conn.execute(
        "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones) "
        "VALUES ('ev2', 'shed', 'person', ?, ?, 0.8, '[]')",
        (now - 150, now - 140),
    )
    # `_known_cameras` (and thus every camera-existence check) reads
    # `recordings`, not `event` -- both cameras need at least one row there.
    for cam in ("alley-wide", "shed"):
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, "
            "duration, segment_size) VALUES (?, ?, ?, ?, ?, 10.0, 5.0)",
            (f"seg-{cam}", cam, f"/media/{cam}.mp4", now - 20, now - 10),
        )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def settings(tmp_path: Path, multi_camera_frigate_db: Path, sidecar_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=cfg,
            db_path=multi_camera_frigate_db,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        encounters=EncountersSection(timeline_max_window_s=21600.0),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _seed_atom(
    settings: Settings,
    atom_id: str,
    *,
    camera: str,
    offset: float,
    label: str = "person",
    direction: Direction | None = None,
    encounter_id: str | None = None,
) -> str:
    conn = db.open_sidecar(settings.sidecar.db_path)
    now = time.time()
    try:
        atom = Atom(
            atom_id=atom_id,
            camera=camera,
            start_time=now + offset,
            end_time=now + offset + 10,
            labels=(label,),
            zones=(),
            event_ids=(f"ev-{atom_id}",),
            sub_labels=(),
            severity="alert",
        )
        decision = (
            LinkDecision(encounter_id, "linked", 1.0)
            if encounter_id
            else LinkDecision(None, "new", 1.0)
        )
        return upsert_atom(conn, atom, decision, now, direction=direction)
    finally:
        conn.close()


def test_lane_order_matches_request_order(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 3600, "end": now, "cameras": "shed,alley-wide"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [lane["camera"] for lane in body["lanes"]] == ["shed", "alley-wide"]


def test_lane_reel_keys_match_reel_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    frozen_now = time.time()
    monkeypatch.setattr(scrub_routes.time, "time", lambda: frozen_now)
    now = frozen_now
    start, end = now - 3600, now

    r_reel = client.get(
        "/v1/reel/alley-wide",
        params={"start": start, "end": end, "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r_reel.status_code == 200
    reel_body = r_reel.json()

    r_tl = client.get(
        "/v1/timeline",
        params={"start": start, "end": end, "cameras": "alley-wide", "motion_scale": 10},
        headers={"cookie": "session=fake"},
    )
    assert r_tl.status_code == 200, r_tl.text
    lane = r_tl.json()["lanes"][0]
    lane_reel_part = {k: v for k, v in lane.items() if k not in ("camera", "observations")}
    assert json.dumps(lane_reel_part, sort_keys=True) == json.dumps(reel_body, sort_keys=True)


def test_observations_land_on_correct_lane_with_expected_fields(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    _seed_atom(
        settings, "a1", camera="alley-wide", offset=-100.0,
        direction=Direction("front_garden", "shed", "out:shed", None, "zones"),
    )
    _seed_atom(settings, "a2", camera="shed", offset=-90.0)

    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 3600, "end": now, "cameras": "alley-wide,shed"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    lanes = {lane["camera"]: lane for lane in body["lanes"]}
    alley_ids = [o["id"] for o in lanes["alley-wide"]["observations"]]
    shed_ids = [o["id"] for o in lanes["shed"]["observations"]]
    assert alley_ids == ["a1"]
    assert shed_ids == ["a2"]
    obs = lanes["alley-wide"]["observations"][0]
    assert set(obs) == {"id", "start", "end", "encounter_id", "labels", "direction", "severity"}
    assert obs["direction"] == "out:shed"
    assert obs["labels"] == ["person"]


def test_encounters_populated_and_deduped(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    eid = _seed_atom(settings, "a1", camera="alley-wide", offset=-100.0)
    _seed_atom(settings, "a2", camera="shed", offset=-90.0, encounter_id=eid)

    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 3600, "end": now, "cameras": "alley-wide,shed"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    encounters = r.json()["encounters"]
    assert len(encounters) == 1
    assert encounters[0]["id"] == eid
    assert encounters[0]["atom_count"] == 2


def test_encounter_param_derives_window_and_cameras(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    eid = _seed_atom(settings, "a1", camera="alley-wide", offset=-100.0)
    _seed_atom(settings, "a2", camera="shed", offset=-90.0, encounter_id=eid)

    r = client.get(
        "/v1/timeline", params={"encounter": eid}, headers={"cookie": "session=fake"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    cameras = [lane["camera"] for lane in body["lanes"]]
    assert set(cameras) == {"alley-wide", "shed"}


def test_encounter_param_explicit_cameras_overrides(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    eid = _seed_atom(settings, "a1", camera="alley-wide", offset=-100.0)
    _seed_atom(settings, "a2", camera="shed", offset=-90.0, encounter_id=eid)

    r = client.get(
        "/v1/timeline",
        params={"encounter": eid, "cameras": "shed"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    cameras = [lane["camera"] for lane in r.json()["lanes"]]
    assert cameras == ["shed"]


def test_unknown_encounter_is_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    r = client.get(
        "/v1/timeline", params={"encounter": "nope"}, headers={"cookie": "session=fake"}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_too_many_cameras_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    cameras = ",".join(f"cam{i}" for i in range(13))
    r = client.get(
        "/v1/timeline",
        params={"start": now - 60, "end": now, "cameras": cameras},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


def test_oversize_window_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 100000, "end": now, "cameras": "alley-wide"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_range"


def test_unknown_camera_mirrors_reel_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 60, "end": now, "cameras": "not-a-camera"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "camera_unknown"


def test_missing_params_is_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _skip_auth(monkeypatch)
    r = client.get(
        "/v1/timeline", params={"cameras": "alley-wide"}, headers={"cookie": "session=fake"}
    )
    assert r.status_code == 422

    now = time.time()
    r2 = client.get(
        "/v1/timeline",
        params={"start": now - 60, "end": now},
        headers={"cookie": "session=fake"},
    )
    assert r2.status_code == 422


def test_etag_304_on_matching_if_none_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    frozen_now = time.time()
    monkeypatch.setattr(timeline_routes.time, "time", lambda: frozen_now)
    monkeypatch.setattr(scrub_routes.time, "time", lambda: frozen_now)

    params = {"start": frozen_now - 3600, "end": frozen_now, "cameras": "alley-wide"}
    r1 = client.get("/v1/timeline", params=params, headers={"cookie": "session=fake"})
    assert r1.status_code == 200
    etag = r1.headers["etag"]

    r2 = client.get(
        "/v1/timeline",
        params=params,
        headers={"cookie": "session=fake", "if-none-match": etag},
    )
    assert r2.status_code == 304


def test_truncated_flag_with_cap_monkeypatched_low(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _skip_auth(monkeypatch)
    _fake_motion(monkeypatch)
    monkeypatch.setattr(timeline_routes, "_OBSERVATIONS_CAP", 1)
    _seed_atom(settings, "a1", camera="alley-wide", offset=-100.0)
    _seed_atom(settings, "a2", camera="alley-wide", offset=-90.0)

    now = time.time()
    r = client.get(
        "/v1/timeline",
        params={"start": now - 3600, "end": now, "cameras": "alley-wide"},
        headers={"cookie": "session=fake"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True
