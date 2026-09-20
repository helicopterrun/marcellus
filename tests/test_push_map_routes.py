"""`GET /v1/push/map/zones` and `/map/live` — floorplan overlay data."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import policy_settings
from marcellus.server import create_app

OPTICS = {"hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0}
LAYOUT = {"x": 0.5, "y": 0.5, "azimuth": 0.0, "fov": 90.0}


@pytest.fixture(autouse=True)
def _isolated_active_policy():
    policy_settings.reset_for_tests()
    yield
    policy_settings.reset_for_tests()


def _make_client(tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    config = tmp_path / "frigate-config.yml"
    config.write_text(yaml.safe_dump({
        "cameras": {
            # A floor-hugging zone (projectable) on a placed camera.
            "cam": {"zones": {"walkway": {"coordinates": "0.3,0.6,0.7,0.6,0.7,0.9,0.3,0.9"}}},
            # Full-frame gate zone: must be skipped.
            "gate": {"zones": {"all": {"coordinates": "0,0,1,0,1,1,0,1"}}},
            # Camera with a zone but no layout: omitted.
            "unplaced": {"zones": {"side": {"coordinates": "0.3,0.6,0.7,0.6,0.5,0.9"}}},
        }
    }))
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp_path / "push_settings.json"),
        ),
    )
    app = create_app(settings)
    return TestClient(app)


def _apply_map_policy(**extra):
    active = dict(policy_settings.get_active())
    active.update({
        "camera_optics": {"cam": dict(OPTICS), "gate": dict(OPTICS)},
        "camera_layout": {"cam": dict(LAYOUT), "gate": dict(LAYOUT)},
        "map_scale_ft": 200.0,
    })
    active.update(extra)
    policy_settings.apply_settings(active)


def test_map_zones_projects_placed_cameras_only(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/zones").json()
    assert body["aspect"] == 1.0
    names = [(z["camera"], z["name"]) for z in body["zones"]]
    assert names == [("cam", "walkway")]
    z = body["zones"][0]
    assert len(z["points"]) >= 3
    assert all(len(p) == 2 for p in z["points"])


def test_map_zones_without_scale_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    body = client.get("/v1/push/map/zones").json()
    assert body["zones"] == []


def test_map_live_without_engine_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/live").json()
    assert body["objects"] == []
    assert body["t"] > 0


def test_map_live_serves_fused_tracks(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    import time

    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()

    from marcellus.push.situations import TrackStore

    class _Engine:
        tracks = TrackStore()

    engine = _Engine()
    now = time.time()
    engine.tracks.observe_object(
        "cam", "t1", (), now=now, path_data=((0.5, 0.7, now),), label="person",
    )
    client.app.state.push_engine = engine
    body = client.get("/v1/push/map/live?debug=1").json()
    assert len(body["objects"]) == 1
    obj = body["objects"][0]
    assert obj["label"] == "person"
    assert obj["cameras"] == ["cam"]
    assert obj["members"][0]["forward_ft"] > 0


class _FakeSubscriber:
    """Minimal stand-in for MqttReviewSubscriber's staleness contract."""

    def __init__(self, last_seen: float) -> None:
        self.last_seen = last_seen

    def is_stale(self, *, now: float) -> bool:
        return (now - self.last_seen) > 60.0  # matches PushSection default


def test_map_live_omits_stale_when_feed_is_fresh(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    import time

    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    client.app.state.push_subscriber = _FakeSubscriber(time.time())
    body = client.get("/v1/push/map/live").json()
    assert "stale" not in body


def test_map_live_flags_stale_when_feed_is_quiet(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    import time

    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    client.app.state.push_subscriber = _FakeSubscriber(time.time() - 3600)
    body = client.get("/v1/push/map/live").json()
    assert body["stale"] is True


def test_map_footprints_projects_placed_cameras(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    body = client.get("/v1/push/map/footprints").json()
    cams = [f["camera"] for f in body["footprints"]]
    assert cams == ["cam", "gate"]  # placed+optics only; sorted
    fp = body["footprints"][0]
    assert len(fp["points"]) >= 3
    assert fp["clipped"] is True  # frame top is sky at 12 deg tilt


def test_map_footprints_without_scale_is_empty(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    assert client.get("/v1/push/map/footprints").json()["footprints"] == []


# ---- /map/track: one event's trail projected for the app mini-map ----


def _live_engine(points):
    import time

    from marcellus.push.situations import TrackStore

    class _Engine:
        tracks = TrackStore()

    engine = _Engine()
    now = time.time()
    path = tuple((x, y, now - (len(points) - 1 - i)) for i, (x, y) in enumerate(points))
    engine.tracks.observe_object("cam", "ev1", (), now=now, path_data=path, label="person")
    return engine


def test_map_track_projects_a_live_track(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy(secure_area={"x0": 0.4, "y0": 0.4, "x1": 0.6, "y1": 0.6})
    client.app.state.push_engine = _live_engine([(0.5, 0.6), (0.5, 0.7), (0.5, 0.8)])
    body = client.get("/v1/push/map/track?camera=cam&event_id=ev1").json()
    assert len(body["points_map"]) == 3
    assert body["camera"] == {"x": 0.5, "y": 0.5}
    assert body["secure_area"]["x0"] == 0.4
    assert body["aspect"] == 1.0
    assert body["speed_ft_s"] is not None and body["speed_ft_s"] > 0
    lo, hi = body["distance_ft_range"]
    assert 0 <= lo <= hi


def test_map_track_404s_without_calibration_or_track(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    # No map policy at all: not projectable.
    r = client.get("/v1/push/map/track?camera=cam&event_id=ev1")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "not_projectable"
    # Calibrated but the track doesn't exist anywhere.
    _apply_map_policy()
    r = client.get("/v1/push/map/track?camera=cam&event_id=ghost")
    assert r.status_code == 404


def test_map_track_decimates_to_sixty_points(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
):
    client = _make_client(tmp_path, frigate_db_path, sidecar_db_path)
    _apply_map_policy()
    pts = [(0.3 + 0.4 * i / 199, 0.7) for i in range(200)]
    client.app.state.push_engine = _live_engine(pts)
    body = client.get("/v1/push/map/track?camera=cam&event_id=ev1").json()
    assert len(body["points_map"]) == 60
    # Endpoints preserved by the even decimation.
    first, last = body["points_map"][0], body["points_map"][-1]
    assert first[0] == pytest.approx(body["points_map"][0][0])
    assert last[0] > first[0]
    assert body["secure_area"] is None
    assert body["distance_ft_range"] is None
