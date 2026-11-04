"""Merge suggestions and dismissals: the table + trigger, merge repointing,
consolidate's dismissal-aware proposals and named matches, and the
`/v1/people/merge-suggestions` routes."""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus.config import (
    FaceEnrichSection,
    FrigateSection,
    Settings,
    SidecarSection,
)
from marcellus.faces import consolidate, enrich
from marcellus.server import create_app
from tests import test_people_cards as _cards
from tests.test_people_routes import World

world = _cards.world
client = _cards.client

T0 = 1_780_000_000.0
DAY = 86400.0


def deg(d: float) -> list[float]:
    return [math.cos(math.radians(d)), math.sin(math.radians(d))]


def seed(world: World, cid: int, angle: float, times: list[float], *, name: str | None = None,
         camera: str = "gate-face") -> None:
    world.cluster(cid, name=name, vec=deg(angle))
    for t in times:
        world.sight(cid, t, camera=camera, vec=deg(angle))


def dismiss_rows(world: World) -> list[tuple[int, int]]:
    return [
        (int(r["id_low"]), int(r["id_high"]))
        for r in world.query("SELECT * FROM face_merge_dismissals ORDER BY id_low, id_high")
    ]


def put_dismissal(world: World, a: int, b: int) -> None:
    c = world.conn()
    try:
        c.execute(
            "INSERT INTO face_merge_dismissals VALUES (?, ?, 1.0)", (min(a, b), max(a, b))
        )
        c.commit()
    finally:
        c.close()


def named(world: World, threshold: float = 0.1) -> list[consolidate.MergeProposal]:
    c = world.conn()
    try:
        return consolidate.propose_named_matches(c, threshold=threshold)
    finally:
        c.close()


def groups(world: World, threshold: float = 0.1) -> list[consolidate.MergeProposal]:
    c = world.conn()
    try:
        return consolidate.propose_merges(c, threshold=threshold)
    finally:
        c.close()


# --------------------------------------------------------------------------
# Table, trigger, merge
# --------------------------------------------------------------------------


def test_merge_repoints_dismissals_and_drops_self_pair(world: World) -> None:
    for cid in (1, 2, 3, 4):
        seed(world, cid, cid * 3, [T0 + cid])
    for pair in ((1, 2), (2, 3), (3, 4), (1, 3)):
        put_dismissal(world, *pair)
    c = world.conn()
    try:
        enrich.merge_clusters(c, 1, 2)  # (1,2) -> (2,2): dropped
        c.commit()
    finally:
        c.close()
    # (2,3) already existed, so (1,3) -> (2,3) is ignored as a duplicate.
    assert dismiss_rows(world) == [(2, 3), (3, 4)]

    c = world.conn()
    try:
        enrich.merge_clusters(c, 4, 2)  # (3,4) -> (2,3), a duplicate again
        c.commit()
    finally:
        c.close()
    assert dismiss_rows(world) == [(2, 3)]


def test_merged_identity_stays_ruled_out(world: World) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 4, [T0 + DAY])
    seed(world, 3, 2, [T0 + 2 * DAY])
    put_dismissal(world, 1, 3)
    c = world.conn()
    try:
        enrich.merge_clusters(c, 1, 2)
        c.commit()
    finally:
        c.close()
    assert dismiss_rows(world) == [(2, 3)]
    assert groups(world) == []


def test_delete_trigger_removes_dismissals(world: World) -> None:
    for cid in (1, 2, 3):
        seed(world, cid, cid, [T0])
    put_dismissal(world, 1, 2)
    put_dismissal(world, 2, 3)
    c = world.conn()
    try:
        enrich.delete_cluster(c, world.settings.face_enrich.crops_dir, 2)
        c.commit()
    finally:
        c.close()
    assert dismiss_rows(world) == []


def test_reap_removes_dismissals(world: World) -> None:
    world.cluster(1, last_seen=T0 - 100 * DAY)
    world.cluster(2, last_seen=T0)
    put_dismissal(world, 1, 2)
    c = world.conn()
    try:
        assert enrich.reap_stale_clusters(c, cfg=FaceEnrichSection(), now=T0) == 1
        c.commit()
    finally:
        c.close()
    assert dismiss_rows(world) == []


# --------------------------------------------------------------------------
# consolidate
# --------------------------------------------------------------------------


def test_propose_merges_respects_dismissal_and_splits_group(world: World) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 2, [T0 + DAY])
    seed(world, 3, 4, [T0 + 2 * DAY])
    assert [[m.id for m in g.members] for g in groups(world)] == [[1, 2, 3]]
    put_dismissal(world, 1, 3)
    # 1-3 is ruled out, so complete linkage cannot hold all three together.
    got = [[m.id for m in g.members] for g in groups(world)]
    assert all(not ({1, 3} <= set(g)) for g in got)
    put_dismissal(world, 1, 2)
    put_dismissal(world, 2, 3)
    assert groups(world) == []


def test_named_match_picks_nearest_named_and_survives_as_survivor(world: World) -> None:
    seed(world, 1, 0, [T0], name="Fakename A")
    seed(world, 2, 10, [T0], name="Fakename B")
    seed(world, 3, 4, [T0 + DAY])  # nearer 1 (4 deg) than 2 (6 deg)
    got = named(world, threshold=0.1)
    assert len(got) == 1
    assert got[0].survivor_id == 1
    assert sorted(m.id for m in got[0].members) == [1, 3]


def test_named_match_threshold_dismissal_and_never_two_named(world: World) -> None:
    seed(world, 1, 0, [T0], name="Fakename A")
    seed(world, 2, 1, [T0], name="Fakename B")  # named-named: never offered
    assert named(world) == []
    seed(world, 3, 40, [T0 + DAY])
    assert named(world, threshold=0.1) == []  # too far
    seed(world, 4, 3, [T0 + DAY])
    assert [g.survivor_id for g in named(world)] == [2]  # 4 is 2 deg from B
    put_dismissal(world, 2, 4)
    # Nearest not-dismissed named cluster takes over.
    assert [g.survivor_id for g in named(world)] == [1]
    put_dismissal(world, 1, 4)
    assert named(world) == []


def test_named_match_same_visit(world: World) -> None:
    seed(world, 1, 0, [T0], name="Fakename A")
    seed(world, 2, 3, [T0 + 60])
    assert named(world)[0].same_visit is True


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def suggestions(client: TestClient, **params: Any) -> dict[str, Any]:
    resp = client.get("/v1/people/merge-suggestions", params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def test_route_not_captured_by_person_id(client: TestClient) -> None:
    resp = client.get("/v1/people/merge-suggestions")
    assert resp.status_code == 200
    assert set(resp.json()) == {"threshold", "suggestions"}
    assert resp.json()["threshold"] == FaceEnrichSection().consolidate_threshold


def test_route_wire_ordering_and_full_person(world: World, client: TestClient) -> None:
    # Named match, not same visit, far-ish: 1 (named) <- 2.
    seed(world, 1, 0, [T0], name="Fakename A")
    seed(world, 2, 20, [T0 + DAY])
    # Unnamed pair 3,4 seen on one camera a minute apart (same visit) but wider apart.
    seed(world, 3, 90, [T0 + 5 * DAY])
    seed(world, 4, 100, [T0 + 5 * DAY + 60])
    body = suggestions(client, tz="America/Los_Angeles")
    got = body["suggestions"]
    assert [s["same_visit"] for s in got] == [True, False]
    assert got[0]["survivor"] in (3, 4)
    assert sorted(m["id"] for m in got[0]["members"]) == [3, 4]
    assert got[1]["survivor"] == 1
    assert sorted(m["id"] for m in got[1]["members"]) == [1, 2]
    # Members are the full Person wire, not the web proposal member.
    person = client.get("/v1/people/1", params={"tz": "America/Los_Angeles"}).json()
    person.pop("rhythm")
    assert [m for m in got[1]["members"] if m["id"] == 1][0] == person
    assert {"familiarity", "name", "note", "followed", "last_camera"} <= set(got[0]["members"][0])
    assert set(got[0]) == {"members", "survivor", "same_visit", "max_distance"}


def test_route_orders_by_distance_then_survivor(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0], name="Fakename A")
    seed(world, 2, 8, [T0 + DAY])  # named match, wider
    seed(world, 3, 100, [T0 + 2 * DAY])
    seed(world, 4, 102, [T0 + 3 * DAY])  # group, tighter
    got = suggestions(client)["suggestions"]
    assert [s["max_distance"] for s in got] == sorted(s["max_distance"] for s in got)
    assert {tuple(sorted(m["id"] for m in s["members"])) for s in got} == {(1, 2), (3, 4)}


def test_route_bad_tz(client: TestClient) -> None:
    assert client.get("/v1/people/merge-suggestions", params={"tz": "Nope/Zone"}).status_code == 422


def test_dismiss_hides_suggestion_and_is_idempotent(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 2, [T0 + DAY])
    assert len(suggestions(client)["suggestions"]) == 1
    r = client.post("/v1/people/merge-suggestions/dismiss", json={"ids": [2, 1]})
    assert r.status_code == 200 and r.json() == {"ok": True, "dismissed": 1}
    assert dismiss_rows(world) == [(1, 2)]
    assert suggestions(client)["suggestions"] == []
    again = client.post("/v1/people/merge-suggestions/dismiss", json={"ids": [1, 2]})
    assert again.json() == {"ok": True, "dismissed": 1}
    assert dismiss_rows(world) == [(1, 2)]


def test_dismiss_records_every_pair(world: World, client: TestClient) -> None:
    for cid in (1, 2, 3):
        seed(world, cid, cid, [T0])
    r = client.post("/v1/people/merge-suggestions/dismiss", json={"ids": [3, 1, 2, 2]})
    assert r.json()["dismissed"] == 3
    assert dismiss_rows(world) == [(1, 2), (1, 3), (2, 3)]


def test_dismiss_validation(world: World, client: TestClient) -> None:
    seed(world, 2, 0, [T0])
    seed(world, 3, 2, [T0])
    url = "/v1/people/merge-suggestions/dismiss"
    for ids in ([], [2], [2, 2]):
        r = client.post(url, json={"ids": ids})
        assert r.status_code == 400, ids
        assert r.json()["detail"]["error"] == "invalid_dismissal"
    assert client.post(url, json={"ids": [2, 999]}).status_code == 404
    # 1 was merged into 2: [1, 2] resolves to a single person.
    c = world.conn()
    try:
        world.cluster(1, vec=deg(1))
        world.sight(1, T0)
        enrich.merge_clusters(c, 1, 2)
        c.commit()
    finally:
        c.close()
    assert client.post(url, json={"ids": [1, 2]}).status_code == 400
    assert dismiss_rows(world) == []


def test_dismiss_resolves_redirected_ids(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 2, [T0])
    seed(world, 3, 4, [T0])
    c = world.conn()
    try:
        enrich.merge_clusters(c, 1, 2)
        c.commit()
    finally:
        c.close()
    r = client.post("/v1/people/merge-suggestions/dismiss", json={"ids": [1, 3]})
    assert r.status_code == 200
    assert dismiss_rows(world) == [(2, 3)]


def test_suggestion_routes_are_behind_the_auth_gate(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    cfg = tmp_path / "c.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=cfg, db_path=frigate_db_path),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=True),
        face_enrich=FaceEnrichSection(enabled=True),
    )
    anon = TestClient(create_app(settings))
    sibling = anon.get("/v1/people").status_code
    assert sibling in (401, 403)
    assert anon.get("/v1/people/merge-suggestions").status_code == sibling
    assert (
        anon.post("/v1/people/merge-suggestions/dismiss", json={"ids": [1, 2]}).status_code
        == sibling
    )


def test_capabilities_advertise_merge_suggestions(world: World, client: TestClient) -> None:
    assert client.get("/v1/capabilities").json()["people"]["merge_suggestions"] is True
    world.settings.face_enrich.enabled = False
    off = TestClient(create_app(world.settings)).get("/v1/capabilities").json()["people"]
    assert off["merge_suggestions"] is False


# --------------------------------------------------------------------------
# Unchanged surfaces
# --------------------------------------------------------------------------


def test_web_merge_proposals_shape_unchanged(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 2, [T0 + DAY])
    body = client.get("/enrich/merge-proposals.json").json()
    assert set(body) == {"threshold", "proposals"}
    p = body["proposals"][0]
    assert set(p) == {"survivor", "max_distance", "same_visit", "members"}
    assert set(p["members"][0]) == {
        "id", "handle", "sightings", "days", "last_seen", "avatar_path",
    }


def test_web_proposals_respect_dismissals(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 2, [T0 + DAY])
    put_dismissal(world, 1, 2)
    assert client.get("/enrich/merge-proposals.json").json()["proposals"] == []


def test_push_code_does_not_touch_dismissals() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "marcellus" / "push"
    for path in root.rglob("*.py"):
        assert "merge_dismissal" not in path.read_text().lower(), path.name


def test_dismissal_table_schema(world: World) -> None:
    cols = {r["name"] for r in world.query("PRAGMA table_info(face_merge_dismissals)")}
    assert cols == {"id_low", "id_high", "dismissed_at"}
    c: sqlite3.Connection = world.conn()
    try:
        c.execute("INSERT INTO face_merge_dismissals VALUES (1, 2, 0)")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO face_merge_dismissals VALUES (1, 2, 0)")
    finally:
        c.close()
