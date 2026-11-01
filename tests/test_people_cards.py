"""`people` on /v1/encounters and /v1/people/for-event/{event_id}."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FaceEnrichSection, FrigateSection, Settings, SidecarSection
from marcellus.faces import people as people_mod
from marcellus.server import create_app
from tests.test_people_routes import LA, NOW, World


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


def encounter(
    world: World, eid: str, start: float, events: list[list[str]], members_end: float | None = None
) -> None:
    """An encounter with one member per list of event ids."""
    c = world.conn()
    try:
        c.execute(
            "INSERT INTO encounters (id, start_time, end_time, updated_at, sealed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, start, members_end or start + 30, start, start + 60),
        )
        for i, ids in enumerate(events):
            c.execute(
                "INSERT INTO encounter_members (atom_id, encounter_id, camera, start_time, "
                "severity, event_ids_json, link_reason, confidence, joined_at) "
                "VALUES (?, ?, 'gate-face', ?, 'alert', ?, 'new', 1.0, ?)",
                (f"{eid}-a{i}", eid, start + i, json.dumps(ids), start),
            )
        c.commit()
    finally:
        c.close()


def listing(client: TestClient, **params: Any) -> dict[str, Any]:
    params.setdefault("since", 0)
    r = client.get("/v1/encounters", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return {e["id"]: e for e in body["encounters"]}


def test_people_listed_named_first_then_days(world: World, client: TestClient) -> None:
    world.cluster(1)  # unnamed, 3 days
    world.cluster(2)  # unnamed, 1 day
    world.cluster(3, name="Horatio")  # named, 1 day
    e1 = world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")
    world.sight(1, "2026-08-03 10:00")
    e2 = world.sight(2, "2026-08-01 10:05")
    e3 = world.sight(3, "2026-08-01 10:10")
    encounter(world, "enc", NOW, [[e2, e1], [e3]])
    got = listing(client)["enc"]["people"]
    assert [p["id"] for p in got] == [3, 1, 2]
    assert got[0]["name"] == "Horatio" and got[1]["days"] == 3
    assert {"handle", "familiarity", "sightings", "avatar_path"} <= set(got[0])
    detail = client.get("/v1/encounters/enc").json()
    assert [p["id"] for p in detail["encounter"]["people"]] == [3, 1, 2]
    assert client.get("/v1/encounters/enc", params={"tz": "Nope/X"}).status_code == 422
    assert client.get("/v1/encounters", params={"tz": "Nope/X"}).status_code == 422


def test_distinct_persons_and_no_faces(world: World, client: TestClient) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    b = world.sight(1, "2026-08-01 10:01")
    encounter(world, "twice", NOW, [[a], [b]])
    encounter(world, "none", NOW - 100, [["nothing-here"]])
    encounter(world, "empty", NOW - 200, [[]])
    got = listing(client)
    assert [p["id"] for p in got["twice"]["people"]] == [1]
    assert got["none"]["people"] == [] and got["empty"]["people"] == []


def test_excluded_rename_merge_delete(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.cluster(2)
    a = world.sight(1, "2026-08-01 10:00")
    b = world.sight(1, "2026-08-02 10:00")
    c = world.sight(2, "2026-08-01 10:00")
    encounter(world, "enc", NOW, [[a, c]])

    def ids() -> list[int]:
        return [p["id"] for p in listing(client)["enc"]["people"]]

    assert ids() == [1, 2]
    # Excluding the event's only sighting of person 2 drops them.
    client.patch(f"/v1/people/2/sightings/{c}", json={"excluded": True})
    assert ids() == [1]
    client.patch(f"/v1/people/2/sightings/{c}", json={"excluded": False})
    assert ids() == [1, 2]
    # Rename shows at once; the encounter's identities strings are not consulted.
    client.post("/v1/people/2/name", json={"name": "Marcellus"})
    got = listing(client)["enc"]
    assert [p["name"] for p in got["people"]] == ["Marcellus", None]
    assert got["identities"] == []
    # Merge: the survivor appears once.
    client.post("/v1/people/1/merge", json={"into": 2})
    assert ids() == [2]
    # Delete: gone.
    client.delete("/v1/people/2")
    assert listing(client)["enc"]["people"] == []
    assert b  # (silence unused)


def test_tz_changes_days(world: World, client: TestClient) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-06 06:30")
    world.sight(1, "2026-08-06 07:30")
    encounter(world, "enc", NOW, [[a]])
    assert listing(client)["enc"]["people"][0]["days"] == 1
    assert listing(client, tz=LA)["enc"]["people"][0]["days"] == 2


def test_disabled_does_no_face_work(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    encounter(world, "enc", NOW, [[a]])
    world.settings.face_enrich.enabled = False

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("face work with face_enrich off")

    monkeypatch.setattr(people_mod, "people_for_events", boom)
    off = TestClient(create_app(world.settings))
    assert listing(off)["enc"]["people"] == []
    assert off.get("/v1/encounters/enc").json()["encounter"]["people"] == []


def test_for_event(world: World, client: TestClient) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    b = world.sight(1, "2026-08-02 10:00", excluded=True)
    r = client.get(f"/v1/people/for-event/{a}")
    assert r.status_code == 200 and r.json()["person"]["id"] == 1
    assert client.get(f"/v1/people/for-event/{b}").json() == {"person": None}
    assert client.get("/v1/people/for-event/unknown").json() == {"person": None}
    assert client.get(f"/v1/people/for-event/{a}", params={"tz": "Nope/X"}).status_code == 422
    assert client.get("/v1/people/1").status_code == 200  # route order intact


def _big_world(world: World, n_enc: int = 200) -> list[str]:
    """~450 clusters, ~3000 sightings, `n_enc` encounters of 3 members x 2 events."""
    c = world.conn()
    try:
        from marcellus.faces import enrich

        vec = enrich.pack_embedding([1.0, 0.0])
        for cid in range(1, 451):
            c.execute(
                "INSERT INTO face_clusters (cluster_id, name, centroid, observation_count, "
                "created_at, last_seen_at) VALUES (?, ?, ?, 1, '', 0)",
                (cid, f"p{cid}" if cid % 9 == 0 else None, vec),
            )
        events = []
        for i in range(3000):
            eid = f"ev{i}"
            events.append(eid)
            c.execute(
                "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                "status, processed_at) VALUES (?, 'gate-face', ?, ?, 'enriched', '')",
                (eid, 1_780_000_000.0 + i * 3000, 1 + i % 450),
            )
        c.commit()
    finally:
        c.close()
    for e in range(n_enc):
        ids = events[e * 6 : e * 6 + 6]
        encounter(world, f"enc{e:03d}", NOW - e * 100, [ids[0:2], ids[2:4], ids[4:6]])
    return events


def test_query_shape_at_realistic_page_size(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _big_world(world)
    face_statements: list[str] = []
    real_open = db.open_sidecar

    def traced(path: Any) -> sqlite3.Connection:
        conn = real_open(path)
        conn.set_trace_callback(
            lambda sql: face_statements.append(sql)
            if "face_enrichments" in sql or "face_clusters" in sql
            else None
        )
        return conn

    monkeypatch.setattr(db, "open_sidecar", traced)
    t0 = time.perf_counter()
    got = listing(client, limit=200)
    elapsed = time.perf_counter() - t0
    print(f"\n/v1/encounters limit=200 with people: {elapsed * 1000:.0f} ms")
    assert len(got) == 200
    assert all(1 <= len(e["people"]) <= 6 for e in got.values())
    # 1200 member events -> ceil(1200/500) lookups + one load_people: independent
    # of the number of encounters on the page.
    assert len(face_statements) <= math.ceil(1200 / 500) + 1
    assert elapsed < 5.0
