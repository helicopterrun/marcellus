"""faces/consolidate.py: read-only merge proposals (complete linkage)."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from marcellus import cli
from marcellus.config import FaceEnrichSection
from marcellus.faces import consolidate
from tests import test_people_cards as _cards
from tests.test_people_routes import World

world = _cards.world
client = _cards.client

T0 = 1_780_000_000.0


def deg(d: float) -> list[float]:
    return [math.cos(math.radians(d)), math.sin(math.radians(d))]


def seed(world: World, cid: int, angle: float, times: list[float], *, name: str | None = None,
         camera: str = "gate-face", followed: float | None = None) -> None:
    world.cluster(cid, name=name, vec=deg(angle), followed=followed)
    for t in times:
        world.sight(cid, t, camera=camera, vec=deg(angle))


def propose(world: World, threshold: float = 0.1, **kw: Any) -> list[consolidate.MergeProposal]:
    conn = world.conn()
    try:
        return consolidate.propose_merges(conn, threshold=threshold, **kw)
    finally:
        conn.close()


def test_default_threshold_is_tighter_than_cluster_threshold() -> None:
    cfg = FaceEnrichSection()
    assert cfg.consolidate_threshold == 0.45 < cfg.cluster_threshold


def test_complete_linkage_does_not_chain(world: World) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 20, [T0 + 86400])  # 1-2: 0.060
    seed(world, 3, 45, [T0 + 2 * 86400])  # 2-3: 0.094 (close), 1-3: 0.293 (not)
    got = propose(world)
    assert len(got) == 1
    assert [m.id for m in got[0].members] == [1, 2]
    assert got[0].max_distance == pytest.approx(0.06, abs=0.001)
    # Wide enough for every pair: one group of three.
    wide = propose(world, threshold=0.3)
    assert [[m.id for m in g.members] for g in wide] == [[1, 2, 3]]


def test_named_and_hidden_clusters_are_never_candidates(world: World) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 1, [T0 + 86400], name="Horatio")  # nearly identical, but named
    world.cluster(3, vec=deg(2))
    world.sight(3, T0, excluded=True, vec=deg(2))  # no live sightings
    assert propose(world) == []


def test_followed_clusters_may_be_candidates(world: World) -> None:
    seed(world, 1, 0, [T0], followed=5.0)
    seed(world, 2, 5, [T0 + 86400])
    assert [m.id for m in propose(world)[0].members] == [1, 2]


def test_same_visit_ordering_and_survivor(world: World) -> None:
    # Pair A (1,2): wide apart in time, closer in distance. Pair B (3,4): same
    # camera two minutes apart, farther apart in distance.
    seed(world, 1, 0, [T0, T0 + 1000, T0 + 2000])
    seed(world, 2, 6, [T0 + 5 * 86400])
    seed(world, 3, 90, [T0 + 86400])
    seed(world, 4, 100, [T0 + 86400 + 120, T0 + 86400 + 4000, T0 + 3 * 86400, T0 + 4 * 86400])
    got = propose(world, threshold=0.2)
    assert [[m.id for m in g.members] for g in got] == [[3, 4], [1, 2]]
    assert got[0].same_visit and not got[1].same_visit
    assert got[0].survivor_id == 4 and got[1].survivor_id == 1  # most sightings
    first = got[0].members[0]
    assert first.handle and first.sightings == 1 and first.days == 1
    assert propose(world, threshold=0.2, visit_gap_s=60)[1].same_visit is False


def test_same_camera_required_for_same_visit(world: World) -> None:
    seed(world, 1, 0, [T0], camera="gate-face")
    seed(world, 2, 5, [T0 + 60], camera="shed")
    assert propose(world)[0].same_visit is False


def test_proposals_write_nothing(world: World) -> None:
    seed(world, 1, 0, [T0, T0 + 30], followed=1.0)
    seed(world, 2, 5, [T0 + 60])
    seed(world, 3, 8, [T0 + 90], name="Horatio")

    def snapshot(conn: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
        tables = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}")), key=repr) for t in tables}

    c = world.conn()
    before = snapshot(c)
    c.close()
    path = Path(world.settings.sidecar.db_path)
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    try:
        got = consolidate.propose_merges(ro, threshold=0.45)
        assert got  # it really did work
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM face_clusters")  # the connection is truly read-only
    finally:
        ro.close()
    c = world.conn()
    assert snapshot(c) == before
    c.close()


def test_web_route(world: World, client: TestClient) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 5, [T0 + 60])
    r = client.get("/enrich/merge-proposals.json")
    assert r.status_code == 200
    body = r.json()
    assert body["threshold"] == 0.45
    group = body["proposals"][0]
    assert [m["id"] for m in group["members"]] == [1, 2] and group["same_visit"] is True
    assert {"handle", "sightings", "days", "last_seen", "avatar_path"} <= set(group["members"][0])
    page = client.get("/enrich/clusters")
    assert 'id="merge-proposals"' in page.text
    assert 'id="merge-proposals"' not in client.get("/enrich/clusters?cluster=1").text


def test_cli_prints_ids_only(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(world, 1, 0, [T0])
    seed(world, 2, 5, [T0 + 60])
    seed(world, 3, 6, [T0], name="Secret Name")
    monkeypatch.setattr(cli, "load_settings", lambda: world.settings)
    res = CliRunner().invoke(cli.app, ["face-enrich", "propose-merges"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["groups"] == 1 and out["largest_group"] == 2 and out["same_visit_groups"] == 1
    assert out["proposals"][0]["ids"] == [1, 2]
    assert "Secret" not in res.output
    assert "handle" not in res.output
