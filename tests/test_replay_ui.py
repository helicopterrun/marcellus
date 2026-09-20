"""Tests for the /replay push-testing web UI."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import replay
from marcellus.server import create_app


def _settings(
    frigate_db_path: Path, sidecar_db_path: Path, *, auth: bool = False
) -> Settings:
    return Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=auth
        ),
        push=PushSection(enabled=False),
    )


@pytest.fixture(autouse=True)
def _reset_run_state():
    """Clear module-level run state between tests."""
    replay._current_run = None
    # Ensure the lock is released if a prior test failed mid-run.
    if replay._run_lock.locked():
        replay._run_lock.release()
    yield
    replay._current_run = None
    if replay._run_lock.locked():
        replay._run_lock.release()


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    return TestClient(create_app(_settings(frigate_db_path, sidecar_db_path)))


@pytest.fixture
def authed_client(frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    return TestClient(
        create_app(_settings(frigate_db_path, sidecar_db_path, auth=True))
    )


def test_replay_page_loads(client: TestClient) -> None:
    r = client.get("/replay")
    assert r.status_code == 200
    assert "card-notify-resolve" in r.text
    assert "real events" in r.text


def test_replay_page_requires_auth(authed_client: TestClient) -> None:
    r = authed_client.get("/replay")
    assert r.status_code == 401


def test_scenarios_endpoint(client: TestClient) -> None:
    r = client.get("/replay/scenarios")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["scenarios"]]
    assert "card-notify-resolve" in names


def test_run_unknown_scenario_rejected(client: TestClient) -> None:
    r = client.post("/replay/run", json={
        "scenarios": ["nonexistent"],
        "speed": 10,
        "dry_run": True,
    })
    assert r.status_code == 400
    assert "nonexistent" in r.json()["detail"]["message"]


def _poll_done(client: TestClient, run_id: str) -> dict:
    """The run now executes in the background; poll status until it settles."""
    for _ in range(200):
        r = client.get("/replay/status")
        run = r.json()["run"]
        if run and run["run_id"] == run_id and run["state"] in ("done", "failed"):
            return run
        time.sleep(0.05)
    raise AssertionError("replay run did not finish in time")


def test_dry_run_publishes_nothing(client: TestClient) -> None:
    # `with` keeps one event loop alive across requests so the background
    # replay task can actually run between our polls.
    with client:
        r = client.post("/replay/run", json={
            "scenarios": ["card-notify-resolve"],
            "speed": 100,
            "dry_run": True,
        })
        assert r.status_code == 200
        run = _poll_done(client, r.json()["run_id"])
    assert run["dry_run"] is True
    assert run["state"] == "done"
    assert len(run["decisions"]) > 0
    assert run["decisions"][0]["mutation"] == "create"


def test_live_run_rejected_when_push_disabled(client: TestClient) -> None:
    r = client.post("/replay/run", json={
        "scenarios": ["card-notify-resolve"],
        "speed": 10,
        "dry_run": False,
    })
    assert r.status_code == 503


def test_single_run_lock(client: TestClient) -> None:
    """Only one run at a time — if the lock is held, a new run gets 409."""
    original_lock = replay._run_lock

    class FakeLock:
        def locked(self) -> bool:
            return True

    replay._run_lock = FakeLock()  # type: ignore[assignment]
    try:
        r = client.post("/replay/run", json={
            "scenarios": ["card-notify-resolve"],
            "speed": 100,
            "dry_run": True,
        })
        assert r.status_code == 409
    finally:
        replay._run_lock = original_lock


def test_status_when_no_run(client: TestClient) -> None:
    r = client.get("/replay/status")
    assert r.status_code == 200
    assert r.json()["run"] is None


def test_stacked_dry_run(client: TestClient) -> None:
    with client:
        r = client.post("/replay/run", json={
            "scenarios": ["card-notify-resolve", "card-la-package"],
            "speed": 100,
            "dry_run": True,
            "stagger": 0,
        })
        assert r.status_code == 200
        run = _poll_done(client, r.json()["run_id"])
    assert run["state"] == "done"
    assert len(run["scenarios"]) == 2
    assert len(run["decisions"]) > 3


def test_capture_window_endpoint_shapes_tracks(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
):
    import json
    import time

    settings = _settings(frigate_db_path, sidecar_db_path)
    capture_path = tmp_path / "mqtt-capture.jsonl"
    settings.push.capture_path = str(capture_path)

    now = time.time()
    lines = []
    for i in range(3):
        lines.append({
            "ts": now - 10 + i,
            "topic": "frigate/events",
            "payload": {"after": {
                "id": "trk1", "camera": "garden", "label": "person",
                "path_data": [[[0.1 + 0.1 * i, 0.5], now - 10 + i]],
            }},
        })
    # A single-point track is dropped; a reviews line is ignored.
    lines.append({
        "ts": now - 5, "topic": "frigate/events",
        "payload": {"after": {"id": "trk2", "camera": "street", "label": "car",
                              "path_data": [[[0.5, 0.5], now - 5]]}},
    })
    lines.append({"ts": now - 4, "topic": "frigate/reviews", "payload": {}})
    capture_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")

    client = TestClient(create_app(settings))
    resp = client.get("/replay/capture-window?minutes=15")
    assert resp.status_code == 200
    tracks = resp.json()["tracks"]
    assert len(tracks) == 1
    t = tracks[0]
    assert t["camera"] == "garden" and t["label"] == "person"
    assert len(t["points"]) == 3
    assert t["points"][0][0] == pytest.approx(0.1)

    # Camera filter.
    resp = client.get("/replay/capture-window?minutes=15&camera=street")
    assert resp.json()["tracks"] == []
