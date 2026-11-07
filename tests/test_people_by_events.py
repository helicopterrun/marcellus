"""`GET /v1/people/by-events`: batch "who is in these events?"."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FaceEnrichSection, FrigateSection, Settings, SidecarSection
from marcellus.faces import people as people_mod
from marcellus.server import create_app
from tests.test_people_routes import LA, World


@pytest.fixture
def world(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> World:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        face_enrich=FaceEnrichSection(enabled=True, crops_dir=tmp_path / "crops"),
    )
    return World(settings)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(create_app(world.settings))


def get(client: TestClient, ids: str, **params: Any) -> Any:
    return client.get("/v1/people/by-events", params={"ids": ids, **params})


def test_two_events_one_person_each_with_own_crop(world: World, client: TestClient) -> None:
    world.cluster(11)
    world.sight(11, "2026-08-01 10:00", event_id="a_1", crop=True)
    world.sight(11, "2026-08-01 11:00", event_id="b_2", crop=True)
    body = get(client, "a_1,b_2").json()
    assert [p["id"] for p in body["people"]] == [11]
    assert body["events"] == [
        {"event_id": "a_1", "person": 11, "crop": "/v1/faces/a_1/0.jpg"},
        {"event_id": "b_2", "person": 11, "crop": "/v1/faces/b_2/0.jpg"},
    ]
    assert isinstance(body["t"], float)


def test_unknown_excluded_and_null_cluster_are_null(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00", event_id="ex", excluded=True, crop=True)
    world.sight(1, "2026-08-01 11:00", event_id="nocl")
    c = world.conn()
    c.execute("UPDATE face_enrichments SET cluster_id = NULL WHERE event_id = 'nocl'")
    c.commit()
    c.close()
    body = get(client, "ex,nocl,unknown").json()
    assert body["people"] == []
    assert body["events"] == [
        {"event_id": e, "person": None, "crop": None} for e in ("ex", "nocl", "unknown")
    ]


def test_merged_person_resolves_to_survivor(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.cluster(2)
    world.sight(1, "2026-08-01 10:00", event_id="s1")
    world.sight(2, "2026-08-02 10:00", event_id="s2")
    assert client.post("/v1/people/2/merge", json={"into": 1}).status_code == 200
    body = get(client, "s1,s2").json()
    assert [e["person"] for e in body["events"]] == [1, 1]
    assert [p["id"] for p in body["people"]] == [1]


def test_enrichment_without_crop_has_null_crop(world: World, client: TestClient) -> None:
    world.cluster(5)
    world.sight(5, "2026-08-01 10:00", event_id="nc")
    body = get(client, "nc").json()
    assert body["events"] == [{"event_id": "nc", "person": 5, "crop": None}]
    assert body["people"][0]["id"] == 5


def test_days_and_familiarity_count_all_sightings(world: World, client: TestClient) -> None:
    world.cluster(3)
    world.sight(3, "2026-08-01 18:00", event_id="d1")
    world.sight(3, "2026-08-03 18:00", event_id="d2")
    world.sight(3, "2026-08-05 18:00", event_id="d3")
    p = get(client, "d1", tz=LA).json()["people"][0]
    assert p["days"] == 3 and p["sightings"] == 3 and p["familiarity"] == "regular"


def test_people_ordered_named_first(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.cluster(2, name="Ann")
    world.sight(1, "2026-08-01 10:00", event_id="x")
    world.sight(2, "2026-08-01 10:00", event_id="y")
    body = get(client, "x,y").json()
    assert [p["id"] for p in body["people"]] == [2, 1]
    assert [e["event_id"] for e in body["events"]] == ["x", "y"]


def test_id_cap_and_collapsing(world: World, client: TestClient) -> None:
    ids101 = ",".join(f"e{i}" for i in range(101))
    r = get(client, ids101)
    assert r.status_code == 422 and r.json()["detail"]["error"] == "too_many_ids"
    r = get(client, ",".join(f"e{i}" for i in range(100)))
    assert r.status_code == 200 and len(r.json()["events"]) == 100
    r = get(client, " a , b,,a,  ,b,c ")
    assert [e["event_id"] for e in r.json()["events"]] == ["a", "b", "c"]
    # Duplicates count once toward the cap.
    dup = ",".join(["z"] * 150)
    assert get(client, dup).status_code == 200


@pytest.mark.parametrize("ids", ["", " , ,"])
def test_no_ids_is_empty_ok(client: TestClient, ids: str) -> None:
    r = get(client, ids)
    assert r.status_code == 200
    body = r.json()
    assert body["people"] == [] and body["events"] == []
    assert client.get("/v1/people/by-events").json()["events"] == []


def test_bad_tz_is_422(client: TestClient) -> None:
    r = get(client, "a", tz="Not/AZone")
    assert r.status_code == 422 and r.json()["detail"]["error"] == "invalid_tz"


def test_route_not_swallowed_by_person_id(client: TestClient) -> None:
    r = get(client, "a")
    assert r.status_code == 200 and set(r.json()) == {"t", "people", "events"}


def test_capability_flag(world: World) -> None:
    on = TestClient(create_app(world.settings)).get("/v1/capabilities").json()["people"]
    assert on["event_lookup"] is True
    world.settings.face_enrich.enabled = False
    off = TestClient(create_app(world.settings)).get("/v1/capabilities").json()["people"]
    assert off["event_lookup"] is False


def test_never_scans_all_people(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1)
    world.cluster(2)
    world.sight(1, "2026-08-01 10:00", event_id="q1")
    world.sight(2, "2026-08-01 10:00", event_id="q2")
    calls: list[dict[str, Any]] = []
    real = people_mod.load_people

    def spy(conn: Any, tz: Any, **kw: Any) -> Any:
        calls.append(kw)
        return real(conn, tz, **kw)

    monkeypatch.setattr(people_mod, "load_people", spy)
    assert get(client, "q1").status_code == 200
    assert len(calls) == 1 and sorted(calls[0]["person_ids"]) == [1]
