"""`/v1/people` (handoff/sidecar-people-api.md): reads, writes, merge
redirects, ID lifetime, and the web merge route's redirect writes."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FaceEnrichSection,
    FrigateSection,
    Settings,
    SidecarSection,
)
from marcellus.faces import enrich
from marcellus.faces.handles import handle_for
from marcellus.frigate_api import FrigateClient
from marcellus.server import create_app

LA = "America/Los_Angeles"
NOW = 1_785_952_622.0


def _ts(s: str) -> float:
    """UTC 'YYYY-MM-DD HH:MM' -> epoch."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


class World:
    """A sidecar DB plus helpers to seed clusters/sightings directly."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.n = 0

    def conn(self) -> sqlite3.Connection:
        return db.open_sidecar(self.settings.sidecar.db_path)

    def cluster(
        self,
        cid: int,
        *,
        name: str | None = None,
        vec: list[float] | None = None,
        last_seen: float = NOW,
        followed: float | None = None,
        note: str | None = None,
    ) -> None:
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO face_clusters (cluster_id, name, centroid, observation_count, "
                "created_at, last_seen_at, followed_at, note) VALUES (?, ?, ?, 0, '', ?, ?, ?)",
                (cid, name, enrich.pack_embedding(vec or [1.0, 0.0]), last_seen, followed, note),
            )
            c.commit()
        finally:
            c.close()

    def sight(
        self,
        cid: int,
        start: float | str,
        *,
        event_id: str | None = None,
        camera: str = "gate-face",
        quality: float = 0.5,
        excluded: bool = False,
        crop: bool = False,
        vec: list[float] | None = None,
    ) -> str:
        self.n += 1
        eid = event_id or f"ev{self.n}"
        ts = _ts(start) if isinstance(start, str) else start
        c = self.conn()
        try:
            c.execute(
                "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                "distance, best_quality, best_frame_ts, embedding, status, processed_at, "
                "excluded_at) VALUES (?, ?, ?, ?, 0.3, ?, ?, ?, 'enriched', '', ?)",
                (
                    eid,
                    camera,
                    ts,
                    cid,
                    quality,
                    ts + 2.0,
                    enrich.pack_embedding(vec or [1.0, 0.0]),
                    "x" if excluded else None,
                ),
            )
            if crop:
                rel = f"{eid}/0.jpg"
                path = Path(self.settings.face_enrich.crops_dir) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"\xff\xd8jpeg-" + eid.encode())
                c.execute(
                    "INSERT INTO face_crops (event_id, rank, quality, frame_ts, bbox, used, "
                    "crop_path) VALUES (?, 0, ?, ?, '[0,0,1,1]', 1, ?)",
                    (eid, quality, ts, rel),
                )
            c.commit()
        finally:
            c.close()
        return eid

    def query(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        c = self.conn()
        try:
            return list(c.execute(sql, args))
        finally:
            c.close()


@pytest.fixture
def world(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> World:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        face_enrich=FaceEnrichSection(
            enabled=True, cameras=["gate-face"], crops_dir=tmp_path / "crops"
        ),
    )
    return World(settings)


@pytest.fixture
def client(world: World) -> TestClient:
    return TestClient(create_app(world.settings))


@pytest.fixture
def labels(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    written: list[tuple[str, str]] = []

    def _fake(self: Any, event_id: str, sub_label: str, *, score: Any = None) -> None:
        written.append((event_id, sub_label))

    monkeypatch.setattr(FrigateClient, "set_sub_label", _fake)
    return written


def _ids(body: dict[str, Any]) -> list[int]:
    return [p["id"] for p in body["people"]]


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_capabilities_people_block(client: TestClient) -> None:
    people = client.get("/v1/capabilities").json()["people"]
    assert people == {
        "enabled": True,
        "filters": ["all", "named", "regular", "new", "followed"],
        "sorts": ["recent", "days"],
    }


def test_capabilities_people_disabled(world: World) -> None:
    world.settings.face_enrich.enabled = False
    people = TestClient(create_app(world.settings)).get("/v1/capabilities").json()["people"]
    assert people == {"enabled": False, "filters": [], "sorts": []}


# --------------------------------------------------------------------------
# GET /v1/people
# --------------------------------------------------------------------------


def _seed_mix(world: World) -> None:
    world.cluster(1, name="Horatio", last_seen=100.0)
    world.sight(1, "2026-08-01 12:00", crop=True, quality=0.9)
    world.cluster(2, followed=1.0, note="courier")
    world.sight(2, "2026-08-01 12:00")
    world.sight(2, "2026-08-03 12:00", camera="shed")
    world.sight(2, "2026-08-04 12:00", camera="alley-wide")
    world.cluster(3)
    world.sight(3, "2026-08-05 12:00")
    world.sight(3, "2026-08-05 13:00")
    world.cluster(4)
    world.sight(4, "2026-08-02 12:00")
    world.sight(4, "2026-08-06 12:00")


def test_list_shape_and_sort_recent(world: World, client: TestClient) -> None:
    _seed_mix(world)
    body = client.get("/v1/people").json()
    assert body["total"] == 4
    assert _ids(body) == [4, 3, 2, 1]
    p2 = next(p for p in body["people"] if p["id"] == 2)
    assert p2 == {
        "id": 2,
        "handle": handle_for(2),
        "name": None,
        "note": "courier",
        "followed": True,
        "familiarity": "regular",
        "sightings": 3,
        "days": 3,
        "first_seen": _ts("2026-08-01 12:00"),
        "last_seen": _ts("2026-08-04 12:00"),
        "last_camera": "alley-wide",
        "avatar_path": None,
    }
    p1 = next(p for p in body["people"] if p["id"] == 1)
    assert p1["avatar_path"] == "/v1/faces/ev1/0.jpg"
    assert p1["familiarity"] == "named"
    assert isinstance(body["t"], float)


def test_list_last_seen_ignores_cluster_row(world: World, client: TestClient) -> None:
    _seed_mix(world)
    p1 = next(p for p in client.get("/v1/people").json()["people"] if p["id"] == 1)
    assert p1["last_seen"] == _ts("2026-08-01 12:00")  # not the cluster's 100.0


def test_list_sort_days_and_filters(world: World, client: TestClient) -> None:
    _seed_mix(world)
    assert _ids(client.get("/v1/people", params={"sort": "days"}).json()) == [2, 4, 3, 1]
    for flt, want in (
        ("named", [1]),
        ("regular", [4, 2]),
        ("new", [3]),
        ("followed", [2]),
        ("all", [4, 3, 2, 1]),
    ):
        body = client.get("/v1/people", params={"filter": flt}).json()
        assert _ids(body) == want, flt
        assert body["total"] == len(want)


def test_list_paging_and_param_validation(world: World, client: TestClient) -> None:
    _seed_mix(world)
    body = client.get("/v1/people", params={"limit": 2, "offset": 1}).json()
    assert body["total"] == 4
    assert _ids(body) == [3, 2]
    assert client.get("/v1/people", params={"limit": 0}).status_code == 422
    assert client.get("/v1/people", params={"limit": 501}).status_code == 422
    assert client.get("/v1/people", params={"filter": "bogus"}).status_code == 422
    assert client.get("/v1/people", params={"sort": "bogus"}).status_code == 422


def test_excluded_sightings_count_for_nothing(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 12:00")
    world.sight(1, "2026-08-09 12:00", excluded=True, crop=True)
    world.cluster(2)
    world.sight(2, "2026-08-02 12:00", excluded=True)
    body = client.get("/v1/people").json()
    assert _ids(body) == [1]
    p = body["people"][0]
    assert (p["sightings"], p["days"], p["avatar_path"]) == (1, 1, None)
    assert p["last_seen"] == _ts("2026-08-01 12:00")
    assert client.get("/v1/people/2").status_code == 404


def test_days_use_caller_timezone(world: World, client: TestClient) -> None:
    # 06:30Z and 07:30Z on Aug 6 are 23:30 Aug 5 and 00:30 Aug 6 in Los Angeles.
    world.cluster(1)
    world.sight(1, "2026-08-06 06:30")
    world.sight(1, "2026-08-06 07:30")
    utc = client.get("/v1/people").json()["people"][0]
    assert (utc["days"], utc["familiarity"]) == (1, "new")
    la = client.get("/v1/people", params={"tz": LA}).json()["people"][0]
    assert (la["days"], la["familiarity"]) == (2, "regular")
    assert client.get("/v1/people/1", params={"tz": LA}).json()["days"] == 2


def test_invalid_tz(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-06 06:30")
    for path in ("/v1/people", "/v1/people/1", "/v1/people/1/similar"):
        r = client.get(path, params={"tz": "Mars/Olympus"})
        assert r.status_code == 422, path
        assert r.json()["detail"]["error"] == "invalid_tz"


# --------------------------------------------------------------------------
# GET /v1/people/{id}
# --------------------------------------------------------------------------


def test_detail_rhythm_monday_first_local_hours(world: World, client: TestClient) -> None:
    world.cluster(1)
    # Mon 2026-08-03 17:15Z = 10:15 PDT; again at 10:45 PDT; Sun 08-09 03:00Z = Sat 20:00 PDT.
    world.sight(1, "2026-08-03 17:15")
    world.sight(1, "2026-08-03 17:45")
    world.sight(1, "2026-08-09 03:00")
    d = client.get("/v1/people/1", params={"tz": LA}).json()
    assert len(d["rhythm"]) == 7 and all(len(row) == 24 for row in d["rhythm"])
    assert d["rhythm"][0][10] == 2  # Monday 10:00
    assert d["rhythm"][5][20] == 1  # Saturday 20:00 (local, not UTC Sunday)
    assert sum(map(sum, d["rhythm"])) == 3
    assert d["sightings"] == 3


def test_detail_unknown_is_404(client: TestClient) -> None:
    r = client.get("/v1/people/999")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "person_not_found"


# --------------------------------------------------------------------------
# GET /v1/people/{id}/sightings
# --------------------------------------------------------------------------


def test_sightings_newest_first_paging_and_crops(world: World, client: TestClient) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00", crop=True, camera="shed")
    b = world.sight(1, "2026-08-02 10:00")
    c = world.sight(1, "2026-08-03 10:00", crop=True)
    world.sight(1, "2026-08-04 10:00", excluded=True)
    body = client.get("/v1/people/1/sightings").json()
    assert [s["event_id"] for s in body["sightings"]] == [c, b, a]
    assert body["sightings"][0] == {
        "event_id": c,
        "camera": "gate-face",
        "start": _ts("2026-08-03 10:00"),
        "face_time": _ts("2026-08-03 10:00") + 2.0,
        "distance": 0.3,
        "crop_paths": [f"/v1/faces/{c}/0.jpg"],
    }
    assert body["sightings"][1]["crop_paths"] == []
    page = client.get(
        "/v1/people/1/sightings", params={"before": _ts("2026-08-03 10:00"), "limit": 1}
    ).json()
    assert [s["event_id"] for s in page["sightings"]] == [b]
    assert client.get("/v1/people/1/sightings", params={"limit": 201}).status_code == 422
    assert client.get("/v1/people/99/sightings").status_code == 404


# --------------------------------------------------------------------------
# GET /v1/people/{id}/similar
# --------------------------------------------------------------------------


def test_similar_closest_first_and_capped(world: World, client: TestClient) -> None:
    world.cluster(1, vec=[1.0, 0.0])
    world.sight(1, "2026-08-01 10:00")
    for cid, y in ((2, 0.4), (3, 0.1), (4, 0.2), (5, 0.3), (6, 0.5), (7, 0.05)):
        world.cluster(cid, vec=[1.0, y])
        world.sight(cid, "2026-08-01 10:00")
    world.cluster(8, vec=[0.0, 1.0])  # far away
    world.sight(8, "2026-08-01 10:00")
    world.cluster(9, vec=[1.0, 0.01])  # close, but nothing live
    world.sight(9, "2026-08-01 10:00", excluded=True)
    body = client.get("/v1/people/1/similar").json()
    assert _ids(body) == [7, 3, 4, 5, 2]
    dists = [p["distance"] for p in body["people"]]
    assert dists == sorted(dists)
    assert "name" in body["people"][0] and "days" in body["people"][0]
    assert client.get("/v1/people/99/similar").status_code == 404


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def test_avatar_is_best_quality_crop_and_follows_redirects(
    world: World, client: TestClient
) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00", event_id="lo", crop=True, quality=0.2)
    world.sight(1, "2026-08-02 10:00", event_id="hi", crop=True, quality=0.9)
    world.sight(1, "2026-08-03 10:00", event_id="gone", crop=True, quality=0.99, excluded=True)
    world.cluster(2)
    world.sight(2, "2026-08-03 10:00")
    assert client.post("/v1/people/2/merge", json={"into": 1}).status_code == 200
    for pid in (1, 2):
        r = client.get(f"/v1/people/{pid}/avatar.jpg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == "private, max-age=3600"
        assert r.content.endswith(b"hi")


def test_avatar_missing_is_404(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    r = client.get("/v1/people/1/avatar.jpg")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"
    assert client.get("/v1/people/55/avatar.jpg").status_code == 404


def test_face_crop_route_and_containment(world: World, client: TestClient) -> None:
    world.cluster(1)
    eid = world.sight(1, "2026-08-01 10:00", crop=True)
    r = client.get(f"/v1/faces/{eid}/0.jpg")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, max-age=3600"
    assert client.get(f"/v1/faces/{eid}/1.jpg").status_code == 404
    # A hand-edited row pointing outside crops_dir is not served.
    outside = Path(world.settings.face_enrich.crops_dir).parent / "secret.jpg"
    outside.write_bytes(b"secret")
    c = world.conn()
    c.execute("UPDATE face_crops SET crop_path = '../secret.jpg' WHERE event_id = ?", (eid,))
    c.commit()
    c.close()
    assert client.get(f"/v1/faces/{eid}/0.jpg").status_code == 404


# --------------------------------------------------------------------------
# POST name
# --------------------------------------------------------------------------


def test_name_trims_labels_and_returns_person(
    world: World, client: TestClient, labels: list[tuple[str, str]]
) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    b = world.sight(1, "2026-08-02 10:00")
    world.sight(1, "2026-08-03 10:00", excluded=True)
    r = client.post("/v1/people/1/name", json={"name": "  Horatio "})
    assert r.status_code == 200
    body = r.json()
    assert (body["id"], body["name"], body["familiarity"]) == (1, "Horatio", "named")
    assert sorted(labels) == sorted([(a, "Horatio"), (b, "Horatio")])
    written = world.query("SELECT sub_label_written FROM face_enrichments WHERE event_id = ?", a)
    assert written[0][0] == "Horatio"


def test_name_errors(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    for bad in ("", "   ", "x" * 101):
        r = client.post("/v1/people/1/name", json={"name": bad})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_name"
    assert client.post("/v1/people/9/name", json={"name": "x"}).status_code == 404
    assert client.post("/v1/people/1/name", json={"name": "x" * 100}).status_code == 200


# --------------------------------------------------------------------------
# POST merge: redirects, chains, carrying
# --------------------------------------------------------------------------


def test_merge_chain_resolves_old_ids_to_final_survivor(world: World, client: TestClient) -> None:
    for cid in (1, 2, 3):
        world.cluster(cid)
        world.sight(cid, f"2026-08-0{cid} 10:00")
    assert client.post("/v1/people/1/merge", json={"into": 2}).json()["id"] == 2
    assert client.get("/v1/people/1").json()["id"] == 2
    r = client.post("/v1/people/2/merge", json={"into": 3})
    assert r.status_code == 200 and r.json()["id"] == 3 and r.json()["sightings"] == 3
    for old in (1, 2, 3):
        assert client.get(f"/v1/people/{old}").json()["id"] == 3
    # Chains stay one hop long.
    rows = {r["old_id"]: r["new_id"] for r in world.query("SELECT * FROM face_cluster_redirects")}
    assert rows == {1: 3, 2: 3}
    assert len(client.get("/v1/people/1/sightings").json()["sightings"]) == 3
    assert _ids(client.get("/v1/people").json()) == [3]


def test_merge_into_old_id_follows_redirect(world: World, client: TestClient) -> None:
    for cid in (1, 2, 3):
        world.cluster(cid)
        world.sight(cid, f"2026-08-0{cid} 10:00")
    client.post("/v1/people/1/merge", json={"into": 2})
    r = client.post("/v1/people/3/merge", json={"into": 1})  # 1 -> 2
    assert r.status_code == 200 and r.json()["id"] == 2


def test_merge_invalid_and_missing(world: World, client: TestClient) -> None:
    for cid in (1, 2):
        world.cluster(cid)
        world.sight(cid, f"2026-08-0{cid} 10:00")
    r = client.post("/v1/people/1/merge", json={"into": 1})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_merge"
    client.post("/v1/people/1/merge", json={"into": 2})
    r = client.post("/v1/people/2/merge", json={"into": 1})  # both resolve to 2
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_merge"
    assert client.post("/v1/people/2/merge", json={"into": 77}).status_code == 404
    assert client.post("/v1/people/77/merge", json={"into": 2}).status_code == 404


def test_merge_carries_name_note_follow_and_last_seen(
    world: World, client: TestClient, labels: list[tuple[str, str]]
) -> None:
    world.cluster(1, name="Horatio", note="the neighbour", followed=5.0, last_seen=NOW + 900.0)
    a = world.sight(1, "2026-08-01 10:00", vec=[1.0, 0.0])
    world.cluster(2, last_seen=100.0)
    b = world.sight(2, "2026-08-02 10:00", vec=[0.0, 1.0])
    r = client.post("/v1/people/1/merge", json={"into": 2})
    assert r.status_code == 200
    body = r.json()
    assert (body["id"], body["name"], body["note"], body["followed"]) == (
        2,
        "Horatio",
        "the neighbour",
        True,
    )
    # Retroactive sub_label write covers the survivor's own sightings too.
    assert sorted(labels) == sorted([(a, "Horatio"), (b, "Horatio")])
    row = world.query("SELECT * FROM face_clusters WHERE cluster_id = 2")[0]
    assert row["last_seen_at"] == NOW + 900.0 and row["followed_at"] == 5.0
    assert row["observation_count"] == 2  # rebuilt from the two embeddings


def test_merge_keeps_survivor_note_and_follow(world: World, client: TestClient) -> None:
    world.cluster(1, note="source note", followed=1.0)
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, note="keep me", followed=7.0, last_seen=500.0)
    world.sight(2, "2026-08-02 10:00")
    body = client.post("/v1/people/1/merge", json={"into": 2}).json()
    assert body["note"] == "keep me"
    row = world.query("SELECT followed_at, last_seen_at FROM face_clusters")[0]
    assert (row["followed_at"], row["last_seen_at"]) == (7.0, NOW)


def test_merge_name_conflict_and_same_name(world: World, client: TestClient) -> None:
    world.cluster(1, name="Horatio")
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, name="Marcellus")
    world.sight(2, "2026-08-02 10:00")
    r = client.post("/v1/people/1/merge", json={"into": 2})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "name_conflict"
    assert client.get("/v1/people/1").json()["id"] == 1  # nothing changed
    world.cluster(3, name="marcellus")
    world.sight(3, "2026-08-03 10:00")
    assert client.post("/v1/people/3/merge", json={"into": 2}).status_code == 200


def test_merge_survivor_named_does_not_relabel(
    world: World, client: TestClient, labels: list[tuple[str, str]]
) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, name="Marcellus")
    world.sight(2, "2026-08-02 10:00")
    body = client.post("/v1/people/1/merge", json={"into": 2}).json()
    assert body["name"] == "Marcellus" and labels == []


# --------------------------------------------------------------------------
# follow / note
# --------------------------------------------------------------------------


def test_follow_and_unfollow(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    assert client.put("/v1/people/1/follow", json={"followed": True}).json()["followed"] is True
    first = world.query("SELECT followed_at FROM face_clusters")[0][0]
    assert first is not None
    client.put("/v1/people/1/follow", json={"followed": True})  # idempotent
    assert world.query("SELECT followed_at FROM face_clusters")[0][0] == first
    assert client.put("/v1/people/1/follow", json={"followed": False}).json()["followed"] is False
    assert client.put("/v1/people/9/follow", json={"followed": True}).status_code == 404


def test_note_set_trim_clear_and_limits(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    r = client.put("/v1/people/1/note", json={"note": "  probably the courier "})
    assert r.json()["note"] == "probably the courier"
    assert client.put("/v1/people/1/note", json={"note": "   "}).json()["note"] is None
    client.put("/v1/people/1/note", json={"note": "hi"})
    assert client.put("/v1/people/1/note", json={"note": None}).json()["note"] is None
    assert client.put("/v1/people/1/note", json={"note": "x" * 200}).status_code == 200
    r = client.put("/v1/people/1/note", json={"note": "x" * 201})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_note"


# --------------------------------------------------------------------------
# PATCH sighting
# --------------------------------------------------------------------------


def test_patch_sighting_exclude_undo_and_empty(world: World, client: TestClient) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    b = world.sight(1, "2026-08-02 10:00")
    world.cluster(2)
    other = world.sight(2, "2026-08-03 10:00")
    r = client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": True})
    assert r.status_code == 200 and r.json()["person"]["sightings"] == 1
    r = client.patch(f"/v1/people/1/sightings/{b}", json={"excluded": True})
    assert r.json() == {"person": None}
    assert client.get("/v1/people/1").status_code == 404
    # Undo works even while the person is invisible.
    r = client.patch(f"/v1/people/1/sightings/{b}", json={"excluded": False})
    assert r.json()["person"]["sightings"] == 1
    assert client.get("/v1/people/1").status_code == 200
    # Not this person's sighting / unknown sighting / unknown person.
    for path in (f"/v1/people/1/sightings/{other}", "/v1/people/1/sightings/nope"):
        assert client.patch(path, json={"excluded": True}).status_code == 404
    assert client.patch(f"/v1/people/42/sightings/{a}", json={"excluded": True}).status_code == 404


def test_patch_sighting_follows_redirect(world: World, client: TestClient) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    world.cluster(2)
    world.sight(2, "2026-08-02 10:00")
    client.post("/v1/people/1/merge", json={"into": 2})
    r = client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": True})
    assert r.status_code == 200 and r.json()["person"]["id"] == 2


# --------------------------------------------------------------------------
# DELETE and ID lifetime
# --------------------------------------------------------------------------


def test_delete_erases_and_old_ids_404(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2)
    eid = world.sight(2, "2026-08-02 10:00", crop=True)
    crop = Path(world.settings.face_enrich.crops_dir) / f"{eid}/0.jpg"
    client.post("/v1/people/1/merge", json={"into": 2})
    assert crop.exists()
    assert client.delete("/v1/people/2").json() == {"ok": True}
    assert not crop.exists()
    for pid in (1, 2):
        assert client.get(f"/v1/people/{pid}").status_code == 404
        assert client.get(f"/v1/people/{pid}/avatar.jpg").status_code == 404
    assert world.query("SELECT * FROM face_cluster_redirects") == []
    assert client.delete("/v1/people/2").status_code == 404
    left = world.query("SELECT cluster_id, embedding FROM face_enrichments")
    assert all(r["cluster_id"] is None and r["embedding"] is None for r in left)


def test_ids_are_never_reused(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    client.delete("/v1/people/1")
    c = world.conn()
    try:
        cur = c.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES (NULL, ?, 1, '', 1)",
            (enrich.pack_embedding([1.0, 0.0]),),
        )
        c.commit()
        assert cur.lastrowid == 2
    finally:
        c.close()


def test_trigger_covers_every_deletion_path(world: World, client: TestClient) -> None:
    for cid in (1, 2, 3):
        world.cluster(cid)
        world.sight(cid, f"2026-08-0{cid} 10:00", event_id=f"e{cid}")
    client.post("/v1/people/1/merge", json={"into": 2})
    assert len(world.query("SELECT * FROM face_cluster_redirects")) == 1
    # Web "remove" empties and deletes cluster 2: 1's redirect goes with it.
    assert client.post("/enrich/events/e1/remove").status_code == 200
    r = client.post("/enrich/events/e2/remove")
    assert r.json()["cluster_deleted"] is True
    assert world.query("SELECT * FROM face_cluster_redirects") == []
    assert client.get("/v1/people/1").status_code == 404


def test_schema_reapply_keeps_redirects_and_trigger(world: World) -> None:
    world.cluster(1)
    world.cluster(2)
    c = world.conn()
    try:
        c.execute("INSERT INTO face_cluster_redirects VALUES (7, 2, 1.0)")
        c.commit()
        for _ in range(2):
            c.executescript(db.SIDECAR_SCHEMA)
        assert c.execute("SELECT COUNT(*) FROM face_cluster_redirects").fetchone()[0] == 1
        trig = c.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_face_cluster_redirect_cleanup'"
        ).fetchone()[0]
        assert trig == 1
        c.execute("DELETE FROM face_clusters WHERE cluster_id = 2")
        assert c.execute("SELECT COUNT(*) FROM face_cluster_redirects").fetchone()[0] == 0
    finally:
        c.close()


def test_existing_face_clusters_table_gains_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE face_clusters (cluster_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
        "centroid BLOB NOT NULL, observation_count INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL, last_seen_at REAL NOT NULL)"
    )
    raw.commit()
    raw.close()
    conn = db.open_sidecar(path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(face_clusters)")}
    finally:
        conn.close()
    assert {"followed_at", "note"} <= cols


# --------------------------------------------------------------------------
# Reap exemption
# --------------------------------------------------------------------------


def test_reap_skips_followed_clusters(world: World) -> None:
    old = NOW - 90 * 86400.0
    world.cluster(1, last_seen=old)
    world.sight(1, old)
    world.cluster(2, last_seen=old, followed=1.0)
    world.sight(2, old)
    world.cluster(3, last_seen=old, name="Horatio")
    world.sight(3, old)
    c = world.conn()
    try:
        reaped = enrich.reap_stale_clusters(c, cfg=world.settings.face_enrich, now=NOW)
        c.commit()
        left = [r[0] for r in c.execute("SELECT cluster_id FROM face_clusters ORDER BY 1")]
    finally:
        c.close()
    assert reaped == 1 and left == [2, 3]


def test_reaped_cluster_takes_its_redirects(world: World, client: TestClient) -> None:
    old = NOW - 90 * 86400.0
    for cid in (1, 2):
        world.cluster(cid, last_seen=old)
        world.sight(cid, old)
    client.post("/v1/people/1/merge", json={"into": 2})
    assert client.get("/v1/people/1").status_code == 200
    c = world.conn()
    try:
        enrich.reap_stale_clusters(c, cfg=world.settings.face_enrich, now=NOW)
        c.commit()
    finally:
        c.close()
    assert client.get("/v1/people/1").status_code == 404
    assert client.get("/v1/people/2").status_code == 404


# --------------------------------------------------------------------------
# Web merge route now shares merge_clusters
# --------------------------------------------------------------------------


def test_web_merge_writes_redirect_and_carries(
    world: World, client: TestClient, labels: list[tuple[str, str]]
) -> None:
    world.cluster(1, name="Horatio", note="n", followed=2.0)
    a = world.sight(1, "2026-08-01 10:00")
    world.cluster(2)
    b = world.sight(2, "2026-08-02 10:00")
    assert client.post("/enrich/clusters/1/merge", json={"into": 2}).json() == {"ok": True}
    red = world.query("SELECT old_id, new_id FROM face_cluster_redirects")[0]
    assert (red["old_id"], red["new_id"]) == (1, 2)
    row = world.query("SELECT name, note, followed_at FROM face_clusters")[0]
    assert (row["name"], row["note"], row["followed_at"]) == ("Horatio", "n", 2.0)
    assert sorted(labels) == sorted([(a, "Horatio"), (b, "Horatio")])
    assert client.get("/v1/people/1").json()["id"] == 2


def test_web_merge_both_named_survivor_wins(world: World, client: TestClient) -> None:
    world.cluster(1, name="Horatio")
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, name="Marcellus")
    world.sight(2, "2026-08-02 10:00")
    assert client.post("/enrich/clusters/1/merge", json={"into": 2}).status_code == 200
    assert world.query("SELECT name FROM face_clusters")[0][0] == "Marcellus"


def test_web_merge_invalid_still_errors(world: World, client: TestClient) -> None:
    world.cluster(1)
    assert client.post("/enrich/clusters/1/merge", json={"into": 1}).status_code == 400
    assert client.post("/enrich/clusters/1/merge", json={"into": 5}).status_code == 404


def test_rebuild_after_merge_leaves_count_and_last_seen_right(world: World) -> None:
    world.cluster(1, last_seen=50.0)
    world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-01 11:00", excluded=True)
    world.cluster(2, last_seen=80.0)
    world.sight(2, "2026-08-02 10:00")
    c = world.conn()
    try:
        enrich.merge_clusters(c, 1, 2, now=1.0)
        c.commit()
        row = c.execute("SELECT observation_count, last_seen_at FROM face_clusters").fetchone()
    finally:
        c.close()
    assert (row["observation_count"], row["last_seen_at"]) == (2, 80.0)


# --------------------------------------------------------------------------
# Review fixes: no write lock across Frigate calls; labels follow exclusion
# and deletion
# --------------------------------------------------------------------------


def _label_recorder(
    monkeypatch: pytest.MonkeyPatch, world: World, fail: bool = False
) -> list[tuple[str, str, bool]]:
    """Record (event, label, write-txn-open) for each set_sub_label call; the
    third field probes the DB from another connection for a held write lock."""
    calls: list[tuple[str, str, bool]] = []

    def _fake(self: Any, event_id: str, sub_label: str, *, score: Any = None) -> None:
        probe = sqlite3.connect(world.settings.sidecar.db_path, timeout=0.05)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
            locked = False
        except sqlite3.OperationalError:
            locked = True
        finally:
            probe.close()
        calls.append((event_id, sub_label, locked))
        if fail:
            from marcellus.frigate_api import FrigateAPIError

            raise FrigateAPIError("HTTP 500")

    monkeypatch.setattr(FrigateClient, "set_sub_label", _fake)
    return calls


def test_retro_label_holds_no_write_lock_during_network(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1)
    for i in range(3):
        world.sight(1, f"2026-08-0{i + 1} 10:00")
    calls = _label_recorder(monkeypatch, world)
    assert client.post("/enrich/clusters/1/name", json={"name": "Horatio"}).status_code == 200
    assert len(calls) == 3 and not any(c[2] for c in calls)
    assert {r[0] for r in world.query("SELECT sub_label_written FROM face_enrichments")} == {
        "Horatio"
    }
    calls.clear()
    assert client.post("/v1/people/1/name", json={"name": "Marcellus"}).status_code == 200
    assert len(calls) == 3 and not any(c[2] for c in calls)


def test_exclude_clears_event_label_and_undo_restores(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")
    c = world.conn()
    c.execute("UPDATE face_enrichments SET sub_label_written = 'Horatio'")
    c.commit()
    c.close()
    calls = _label_recorder(monkeypatch, world)
    r = client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": True})
    assert r.status_code == 200
    assert calls == [(a, "", False)]
    row = world.query("SELECT sub_label_written FROM face_enrichments WHERE event_id = ?", a)
    assert row[0][0] is None
    r = client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": False})
    assert r.status_code == 200
    assert calls[-1] == (a, "Horatio", False)
    row = world.query("SELECT sub_label_written FROM face_enrichments WHERE event_id = ?", a)
    assert row[0][0] == "Horatio"


def test_exclude_unlabeled_or_unnamed_makes_no_frigate_calls(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")
    calls = _label_recorder(monkeypatch, world)
    client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": True})
    client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": False})
    assert calls == []


def test_exclude_frigate_failure_never_fails_request(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")
    c = world.conn()
    c.execute("UPDATE face_enrichments SET sub_label_written = 'Horatio'")
    c.commit()
    c.close()
    _label_recorder(monkeypatch, world, fail=True)
    r = client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": True})
    assert r.status_code == 200
    # The clear failed, so the record still says the label is on the event.
    row = world.query("SELECT sub_label_written FROM face_enrichments WHERE event_id = ?", a)
    assert row[0][0] == "Horatio"
    assert client.patch(f"/v1/people/1/sightings/{a}", json={"excluded": False}).status_code == 200


def test_web_exclude_leaves_labels_alone(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    calls = _label_recorder(monkeypatch, world)
    assert client.patch(f"/enrich/events/{a}", json={"excluded": True}).status_code == 200
    assert calls == []


def _seed_encounters(world: World, *names: str) -> None:
    import json as _json

    c = world.conn()
    try:
        c.execute(
            "INSERT INTO encounters (id, start_time, updated_at, identities_json) "
            "VALUES ('enc', 1.0, 1.0, ?)",
            (_json.dumps(list(names)),),
        )
        c.execute(
            "INSERT INTO encounter_members (atom_id, encounter_id, camera, start_time, severity, "
            "sub_labels_json, link_reason, confidence, joined_at) "
            "VALUES ('atom', 'enc', 'gate-face', 1.0, 'alert', ?, 'new', 1.0, 1.0)",
            (_json.dumps(list(names)),),
        )
        c.commit()
    finally:
        c.close()


def _encounter_names(world: World) -> tuple[str, str]:
    return (
        world.query("SELECT identities_json FROM encounters")[0][0],
        world.query("SELECT sub_labels_json FROM encounter_members")[0][0],
    )


def test_delete_scrubs_name_and_clears_event_labels(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")  # never labeled
    c = world.conn()
    c.execute("UPDATE face_enrichments SET sub_label_written = 'Horatio' WHERE event_id = ?", (a,))
    c.commit()
    c.close()
    _seed_encounters(world, "Horatio", "amazon")
    calls = _label_recorder(monkeypatch, world)
    assert client.delete("/v1/people/1").json() == {"ok": True}
    assert calls == [(a, "", False)]
    assert _encounter_names(world) == ('["amazon"]', '["amazon"]')
    assert all(r[0] is None for r in world.query("SELECT sub_label_written FROM face_enrichments"))


def test_delete_keeps_name_while_another_cluster_uses_it(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, name="Horatio")
    world.sight(2, "2026-08-02 10:00")
    _seed_encounters(world, "Horatio")
    _label_recorder(monkeypatch, world)
    assert client.delete("/v1/people/1").status_code == 200
    assert _encounter_names(world) == ('["Horatio"]', '["Horatio"]')


def test_delete_frigate_failure_still_succeeds(
    world: World, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.cluster(1, name="Horatio")
    a = world.sight(1, "2026-08-01 10:00")
    c = world.conn()
    c.execute("UPDATE face_enrichments SET sub_label_written = 'Horatio' WHERE event_id = ?", (a,))
    c.commit()
    c.close()
    _label_recorder(monkeypatch, world, fail=True)
    assert client.delete("/v1/people/1").status_code == 200
    assert client.get("/v1/people/1").status_code == 404


def test_avatar_path_tracks_the_best_crop(world: World, client: TestClient) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00", event_id="lo", crop=True, quality=0.2)
    world.sight(1, "2026-08-02 10:00", event_id="hi", crop=True, quality=0.9)

    def avatar() -> str | None:
        got: str | None = client.get("/v1/people").json()["people"][0]["avatar_path"]
        return got

    assert avatar() == "/v1/faces/hi/0.jpg"
    assert client.get("/v1/people/1").json()["avatar_path"] == "/v1/faces/hi/0.jpg"
    client.patch("/v1/people/1/sightings/hi", json={"excluded": True})
    assert avatar() == "/v1/faces/lo/0.jpg"
    client.patch("/v1/people/1/sightings/hi", json={"excluded": False})
    assert avatar() == "/v1/faces/hi/0.jpg"


def test_avatar_path_null_quality_loses_and_newest_breaks_ties(
    world: World, client: TestClient
) -> None:
    world.cluster(1)
    world.sight(1, "2026-08-01 10:00", event_id="old", crop=True, quality=0.5)
    world.sight(1, "2026-08-02 10:00", event_id="new", crop=True, quality=0.5)
    world.sight(1, "2026-08-03 10:00", event_id="nul", crop=True)
    c = world.conn()
    c.execute("UPDATE face_enrichments SET best_quality = NULL WHERE event_id = 'nul'")
    c.commit()
    c.close()
    assert client.get("/v1/people").json()["people"][0]["avatar_path"] == "/v1/faces/new/0.jpg"
    assert client.get("/v1/people/1/avatar.jpg").content.endswith(b"new")


# --------------------------------------------------------------------------
# Generated handles
# --------------------------------------------------------------------------


def test_handle_present_on_every_person_shape(world: World, client: TestClient) -> None:
    from marcellus.faces.handles import handle_for

    world.cluster(1)
    world.sight(1, "2026-08-01 10:00")
    world.cluster(2, vec=[1.0, 0.01])
    world.sight(2, "2026-08-02 10:00")
    assert client.get("/v1/people").json()["people"][0]["handle"] in {
        handle_for(1),
        handle_for(2),
    }
    assert client.get("/v1/people/1").json()["handle"] == handle_for(1)
    assert client.get("/v1/people/1/similar").json()["people"][0]["handle"] == handle_for(2)
    r = client.put("/v1/people/1/note", json={"note": "x"})
    assert r.json()["handle"] == handle_for(1)


def test_handle_is_not_a_name_and_survives_naming_and_merge(
    world: World, client: TestClient, labels: list[tuple[str, str]]
) -> None:
    from marcellus.faces.handles import handle_for

    world.cluster(1)
    a = world.sight(1, "2026-08-01 10:00")
    world.cluster(2)
    world.sight(2, "2026-08-02 10:00")
    before = client.get("/v1/people/1").json()
    assert before["name"] is None and before["familiarity"] == "new"
    assert before["handle"] == handle_for(1)
    # An unnamed sighting never records a label, and nothing was sent.
    assert world.query("SELECT sub_label_written FROM face_enrichments WHERE event_id = ?", a)[
        0
    ][0] is None
    assert labels == []
    named = client.post("/v1/people/1/name", json={"name": "Horatio"}).json()
    assert named["handle"] == handle_for(1) and named["familiarity"] == "named"
    # Only the name ever reaches Frigate.
    assert labels and {lbl for _, lbl in labels} == {"Horatio"}
    # Merge: survivor keeps its own handle; merged-away ids show the survivor's.
    merged = client.post("/v1/people/2/merge", json={"into": 1}).json()
    assert merged["handle"] == handle_for(1)
    assert client.get("/v1/people/2").json()["handle"] == handle_for(1)
    assert client.get("/v1/people/2").json()["id"] == 1


def test_web_page_shows_handle_for_unnamed_with_id(world: World, client: TestClient) -> None:
    from marcellus.faces.handles import handle_for

    world.cluster(1, name="Horatio")
    world.sight(1, "2026-08-01 10:00")
    world.sight(1, "2026-08-02 10:00")
    world.cluster(2)
    world.sight(2, "2026-08-01 10:00")
    world.sight(2, "2026-08-02 10:00")
    html = client.get("/enrich/clusters?view=all").text
    assert f"{handle_for(2)} · #2" in html
    assert handle_for(1) not in html  # named clusters show their name only
