"""faces/accuracy.py: blind face-pair labelling, and its routes."""

from __future__ import annotations

import json
import math
import random
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import (
    FaceEnrichSection,
    FrigateSection,
    Settings,
    SidecarSection,
)
from marcellus.faces import accuracy, enrich
from marcellus.faces.handles import handle_for
from marcellus.server import create_app

NOW = 1_780_000_000.0
HOUR = 3600.0


def deg(d: float) -> list[float]:
    return [math.cos(math.radians(d)), math.sin(math.radians(d))]


class Rig:
    def __init__(self, path: Path, crops: Path) -> None:
        self.conn = db.open_sidecar(path)
        self.crops = crops
        crops.mkdir(parents=True, exist_ok=True)

    def cluster(self, cid: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO face_clusters (cluster_id, centroid, observation_count, "
            "created_at, last_seen_at) VALUES (?, ?, 1, '', 0)",
            (cid, enrich.pack_embedding([1.0, 0.0])),
        )

    def sight(
        self,
        eid: str,
        cid: int,
        angle: float,
        *,
        ts: float = NOW,
        camera: str = "gate-face",
        quality: float = 0.5,
        crop: bool = True,
        excluded: bool = False,
        vec: list[float] | None = None,
    ) -> None:
        self.cluster(cid)
        if crop:
            (self.crops / f"{eid}.jpg").write_bytes(b"jpg")
        self.conn.execute(
            "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
            "embedding, best_quality, status, processed_at, excluded_at, best_crop_path) "
            "VALUES (?, ?, ?, ?, ?, ?, 'enriched', '', ?, ?)",
            (
                eid,
                camera,
                ts,
                cid,
                enrich.pack_embedding(vec or deg(angle)),
                quality,
                "x" if excluded else None,
                f"{eid}.jpg" if crop else None,
            ),
        )
        if crop:
            for rank in range(2):
                self.conn.execute(
                    "INSERT INTO face_crops (event_id, rank, quality, frame_ts, bbox, used, "
                    "crop_path) VALUES (?, ?, 0.5, 0, '[]', 1, ?)",
                    (eid, rank, f"{eid}.jpg"),
                )
        self.conn.commit()

    def pairs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM face_pair_labels ORDER BY pair_id").fetchall()

    def draw(self, count: int, seed: int = 1, since: float | None = None) -> int:
        n = accuracy.draw_pairs(
            self.conn, count=count, rng=random.Random(seed), now=NOW, since=since
        )
        self.conn.commit()
        return n


@pytest.fixture
def rig(sidecar_db_path: Path, tmp_path: Path) -> Rig:
    return Rig(sidecar_db_path, tmp_path / "crops")


def populate(rig: Rig, clusters: int = 8, each: int = 4) -> None:
    for c in range(1, clusters + 1):
        for k in range(each):
            rig.sight(f"e{c}-{k}", c, c * 20 + k, ts=NOW + k * 2 * HOUR, quality=0.1 * (k + 1))


def test_half_split_half_join(rig: Rig) -> None:
    populate(rig)
    assert rig.draw(10) == 10
    kinds = [r["kind"] for r in rig.pairs()]
    assert kinds.count("split") == 5 and kinds.count("join") == 5
    # Inserted in shuffled order, not split-then-join.
    assert kinds != sorted(kinds)


def test_thin_data_returns_fewer(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 90)
    assert rig.draw(50) == 1  # one split pair; no cluster has two sightings
    assert rig.pairs()[0]["kind"] == "split"


def test_second_draw_never_repeats(rig: Rig) -> None:
    populate(rig)
    first = rig.draw(12)
    second = rig.draw(12, seed=2)
    seen = [frozenset((r["event_a"], r["event_b"])) for r in rig.pairs()]
    assert len(seen) == first + second
    assert len(set(seen)) == len(seen)


def test_split_pairs_cross_clusters_and_pick_nearest(rig: Rig) -> None:
    rig.sight("a1", 1, 0, quality=0.9)
    rig.sight("a2", 1, -40, quality=0.1)
    rig.sight("b1", 2, 30)
    rig.sight("c1", 3, 100)
    rig.draw(2)
    splits = [r for r in rig.pairs() if r["kind"] == "split"]
    assert splits
    for r in splits:
        assert r["cluster_a"] != r["cluster_b"]
    best = {frozenset((r["event_a"], r["event_b"])) for r in splits}
    # a1 is cluster 1's best sighting; its nearest outsider is b1 (30 deg).
    assert frozenset(("a1", "b1")) in best
    r = next(r for r in splits if {r["event_a"], r["event_b"]} == {"a1", "b1"})
    assert r["distance"] == pytest.approx(1 - math.cos(math.radians(30)))


def test_join_pairs_stay_in_cluster_and_prefer_other_visit(rig: Rig) -> None:
    # Same cluster: s (t0), near (t0+60s, closest to the mean), far (t0+5h).
    rig.sight("s", 1, 0, ts=NOW)
    rig.sight("near", 1, 4, ts=NOW + 60)
    rig.sight("far", 1, 20, ts=NOW + 5 * HOUR)
    rig.draw(6)
    joins = [r for r in rig.pairs() if r["kind"] == "join"]
    assert joins
    for r in joins:
        assert r["cluster_a"] == r["cluster_b"] == 1
    s_pair = next(r for r in joins if "s" in (r["event_a"], r["event_b"]))
    assert {s_pair["event_a"], s_pair["event_b"]} != {"s", "near"}
    assert s_pair["same_visit"] == 0


def test_since_none_matches_default_and_stores_null(rig: Rig) -> None:
    populate(rig)
    rig.draw(10, seed=3)
    a = [(r["event_a"], r["event_b"], r["kind"]) for r in rig.pairs()]
    assert all(r["drawn_since"] is None for r in rig.pairs())
    rig.conn.execute("DELETE FROM face_pair_labels")
    accuracy.draw_pairs(rig.conn, count=10, rng=random.Random(3), now=NOW, since=None)
    assert a == [(r["event_a"], r["event_b"], r["kind"]) for r in rig.pairs()]


def test_since_filters_joins_to_recent_sightings(rig: Rig) -> None:
    populate(rig, 4, 4)  # k=0..3 at NOW + 0/2/4/6 h
    cut = NOW + 3 * HOUR
    rig.draw(40, since=cut)
    joins = [r for r in rig.pairs() if r["kind"] == "join"]
    assert joins
    for r in joins:
        # Exactly one side is the judged (recent) sighting; the partner may be old.
        ks = [int(r[c].split("-")[1]) for c in ("event_a", "event_b")]
        assert max(ks) >= 2
    assert all(r["drawn_since"] == cut for r in rig.pairs())


def test_since_split_representative_is_best_recent_sighting(rig: Rig) -> None:
    # Cluster 1: best overall (old, q .9) vs best recent (new, q .5).
    rig.sight("a-old", 1, 0, ts=NOW, quality=0.9)
    rig.sight("a-new", 1, 10, ts=NOW + 10 * HOUR, quality=0.5)
    rig.sight("a-new2", 1, 12, ts=NOW + 11 * HOUR, quality=0.2)
    rig.sight("b-new", 2, 60, ts=NOW + 10 * HOUR, quality=0.5)
    rig.draw(6, since=NOW + 5 * HOUR)
    splits = [r for r in rig.pairs() if r["kind"] == "split"]
    sides = {e for r in splits for e in (r["event_a"], r["event_b"])}
    assert "a-new" in sides and "a-old" not in sides


def test_since_excludes_clusters_without_recent_sightings(rig: Rig) -> None:
    rig.sight("old1", 1, 0, ts=NOW)
    rig.sight("old2", 1, 5, ts=NOW + HOUR)
    rig.sight("new1", 2, 50, ts=NOW + 10 * HOUR)
    rig.sight("new2", 3, 90, ts=NOW + 10 * HOUR)
    rig.draw(10, since=NOW + 5 * HOUR)
    for r in rig.pairs():
        if r["kind"] == "split":
            # Cluster 1 has nothing recent: it may be the far side, never the anchor.
            assert r["cluster_a"] != r["cluster_b"]
    anchors = {r["cluster_a"] for r in rig.pairs() if r["kind"] == "split"} | {
        r["cluster_b"] for r in rig.pairs() if r["kind"] == "split"
    }
    assert anchors & {2, 3}
    assert not any(r["kind"] == "join" for r in rig.pairs())  # no recent join candidate


def test_since_with_nothing_recent_draws_zero(rig: Rig) -> None:
    populate(rig)
    assert rig.draw(10, since=NOW + 100 * HOUR) == 0


def test_migration_adds_drawn_since_to_existing_db(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    # Rebuild without the column (no DROP COLUMN: CI's SQLite rejects it).
    cols = [
        r[1] for r in conn.execute("PRAGMA table_info(face_pair_labels)") if r[1] != "drawn_since"
    ]
    conn.execute(f"CREATE TABLE fpl_old AS SELECT {', '.join(cols)} FROM face_pair_labels")
    conn.execute("DROP TABLE face_pair_labels")
    conn.execute("ALTER TABLE fpl_old RENAME TO face_pair_labels")
    conn.execute(
        "INSERT INTO face_pair_labels (kind, event_a, event_b, cluster_a, cluster_b, "
        "distance, same_visit, drawn_at) VALUES ('split', 'x', 'y', 1, 2, .3, 0, 0)"
    )
    conn.commit()
    conn.close()
    db._SCHEMA_APPLIED.discard(str(sidecar_db_path.resolve()))
    conn = db.open_sidecar(sidecar_db_path)
    try:
        assert "drawn_since" in {r[1] for r in conn.execute("PRAGMA table_info(face_pair_labels)")}
        assert conn.execute("SELECT drawn_since FROM face_pair_labels").fetchone()[0] is None
    finally:
        conn.close()


def test_same_visit_flag(rig: Rig) -> None:
    rig.sight("p", 1, 0, ts=NOW)
    rig.sight("q", 1, 5, ts=NOW + 60)
    rig.draw(2)
    assert [r["same_visit"] for r in rig.pairs()] == [1]


def test_ineligible_sightings_are_never_drawn(rig: Rig) -> None:
    rig.sight("ok1", 1, 0)
    rig.sight("ok2", 2, 40)
    rig.sight("excl", 3, 41, excluded=True)
    rig.sight("nocrop", 4, 42, crop=False)
    rig.draw(10)
    used = {e for r in rig.pairs() for e in (r["event_a"], r["event_b"])}
    assert used == {"ok1", "ok2"}


def test_odd_embedding_dimension_dropped(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 40)
    rig.sight("odd", 3, 0, vec=[1.0, 0.0, 0.0])
    rig.draw(10)
    used = {e for r in rig.pairs() for e in (r["event_a"], r["event_b"])}
    assert "odd" not in used


def test_draw_changes_no_identity_rows(rig: Rig) -> None:
    populate(rig)
    tables = ("face_clusters", "face_enrichments", "face_crops")
    before = {t: rig.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}
    before = {t: [tuple(r) for r in rows] for t, rows in before.items()}
    rig.draw(20)
    accuracy.state(rig.conn, rig.crops)
    after = {
        t: [tuple(r) for r in rig.conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in tables
    }
    assert after == before


def test_wilson_known_values() -> None:
    low, high = accuracy.wilson(5, 25)
    assert low == pytest.approx(0.089, abs=0.002)
    assert high == pytest.approx(0.391, abs=0.002)
    assert accuracy.wilson(0, 0) == (0.0, 0.0)
    assert accuracy.wilson(10, 10)[1] == pytest.approx(1.0)


def label_all(rig: Rig, label: str = "same") -> None:
    while (p := accuracy.next_pair(rig.conn, rig.crops)) is not None:
        accuracy.set_label(rig.conn, p["pair_id"], label, NOW)
    rig.conn.commit()


def insert_pair(
    rig: Rig, kind: str, a: str, b: str, ca: int, cb: int, dist: float, label: str | None
) -> int:
    cur = rig.conn.execute(
        "INSERT INTO face_pair_labels (kind, event_a, event_b, cluster_a, cluster_b, distance, "
        "same_visit, drawn_at, label) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (kind, a, b, ca, cb, dist, NOW, label),
    )
    rig.conn.commit()
    return int(cur.lastrowid or 0)


def test_band_edges_half_open(rig: Rig) -> None:
    for i, d in enumerate((0.0, 0.2, 0.3, 0.449999, 0.45, 1.5)):
        insert_pair(rig, "split", f"x{i}", f"y{i}", 1, 2, d, "different")
    bands = {b["lo"]: b["different"] for b in accuracy.results(rig.conn)["bands"]}
    assert bands[0.0] == 1  # 0.0
    assert bands[0.2] == 1  # 0.2 sits in [0.2, 0.3)
    assert bands[0.3] == 1  # 0.3 and not 0.449999's neighbour
    assert bands[0.4] == 1  # 0.449999
    assert bands[0.45] == 1  # 0.45 starts its own band
    assert bands[0.7] == 1  # 1.5


def test_set_label_none_returns_pair_and_unknown_id(rig: Rig) -> None:
    populate(rig, 3, 2)
    rig.draw(4)
    first = accuracy.next_pair(rig.conn, rig.crops)
    assert first is not None
    assert accuracy.set_label(rig.conn, first["pair_id"], "same", NOW)
    second = accuracy.next_pair(rig.conn, rig.crops)
    assert second is not None and second["pair_id"] != first["pair_id"]
    assert accuracy.set_label(rig.conn, first["pair_id"], None, NOW)
    again = accuracy.next_pair(rig.conn, rig.crops)
    assert again is not None and again["pair_id"] == first["pair_id"]
    row = rig.conn.execute(
        "SELECT label, labeled_at FROM face_pair_labels WHERE pair_id = ?", (first["pair_id"],)
    ).fetchone()
    assert row["label"] is None and row["labeled_at"] is None
    assert accuracy.set_label(rig.conn, 9999, "same", NOW) is False
    with pytest.raises(ValueError):
        accuracy.set_label(rig.conn, first["pair_id"], "maybe", NOW)


def test_missing_crop_file_becomes_gone_and_leaves_totals(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 40)
    rig.sight("c", 3, 80)
    insert_pair(rig, "split", "a", "b", 1, 2, 0.3, None)
    insert_pair(rig, "split", "b", "c", 2, 3, 0.3, None)
    (rig.crops / "a.jpg").unlink()
    st = accuracy.state(rig.conn, rig.crops)
    assert st["pair"] is not None and st["pair"]["pair_id"] == 2
    assert st["total"] == 1 and st["done"] == 0
    row = rig.conn.execute("SELECT label, labeled_at FROM face_pair_labels WHERE pair_id=1")
    r = row.fetchone()
    assert r["label"] == "gone" and r["labeled_at"] is not None


def test_wire_form_is_blind(rig: Rig) -> None:
    populate(rig, 3, 2)
    rig.draw(4)
    pair = accuracy.next_pair(rig.conn, rig.crops)
    assert pair is not None
    assert set(pair) == {"pair_id", "a", "b"}
    assert set(pair["a"]) == {"crops"} and set(pair["b"]) == {"crops"}
    assert 1 <= len(pair["a"]["crops"]) <= 3
    row = rig.conn.execute(
        "SELECT * FROM face_pair_labels WHERE pair_id = ?", (pair["pair_id"],)
    ).fetchone()
    blob = json.dumps(pair)
    for banned in (
        "split",
        "join",
        "distance",
        "cluster",
        "handle",
        "camera",
        "gate-face",
        str(row["distance"]),
    ):
        assert banned not in blob
    assert handle_for(int(row["cluster_a"])) not in blob


def test_crop_falls_back_to_best_crop_path(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 40)
    rig.conn.execute("DELETE FROM face_crops WHERE event_id = 'a'")
    insert_pair(rig, "split", "a", "b", 1, 2, 0.3, None)
    pair = accuracy.next_pair(rig.conn, rig.crops)
    assert pair is not None
    assert pair["a"]["crops"] == ["/enrich/face/a.jpg"]
    assert pair["b"]["crops"] == ["/enrich/face/b/0.jpg", "/enrich/face/b/1.jpg"]


def test_only_faces_that_fed_the_embedding_are_shown(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 5)
    rig.conn.execute("UPDATE face_crops SET used = 0 WHERE rank = 1")
    rig.conn.commit()
    assert rig.draw(1) == 1
    pair = accuracy.next_pair(rig.conn, rig.crops)
    assert pair is not None
    assert len(pair["a"]["crops"]) == 1 and pair["a"]["crops"][0].endswith("/0.jpg")


def test_deleting_or_reaping_an_identity_removes_its_pairs(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 5)
    rig.sight("c", 3, 90, ts=NOW - 90 * 86400.0)
    rig.sight("d", 4, 95, ts=NOW)
    pair = (
        "INSERT INTO face_pair_labels (kind, event_a, event_b, cluster_a, cluster_b, "
        "distance, same_visit, drawn_at) VALUES ('split', ?, ?, ?, ?, 0.3, 0, 0)"
    )
    rig.conn.execute(pair, ("a", "b", 1, 2))
    rig.conn.execute(pair, ("c", "d", 3, 4))
    rig.conn.execute("UPDATE face_clusters SET last_seen_at = ?", (NOW,))
    rig.conn.execute(
        "UPDATE face_clusters SET last_seen_at = ? WHERE cluster_id = 3", (NOW - 90 * 86400.0,)
    )
    rig.conn.commit()
    enrich.delete_cluster(rig.conn, rig.crops, 1)
    assert [p["event_a"] for p in rig.pairs()] == ["c"]
    assert enrich.reap_stale_clusters(rig.conn, cfg=FaceEnrichSection(), now=NOW) == 1
    assert rig.pairs() == []


def test_merging_keeps_pairs(rig: Rig) -> None:
    rig.sight("a", 1, 0)
    rig.sight("b", 2, 5)
    assert rig.draw(1) == 1
    enrich.merge_clusters(rig.conn, 1, 2)
    assert len(rig.pairs()) == 1


def test_results_withheld_while_pairs_remain(rig: Rig) -> None:
    populate(rig, 3, 2)
    rig.draw(4)
    st = accuracy.state(rig.conn, rig.crops)
    assert st["results"] is None and st["total"] == 4 and st["done"] == 0
    p = st["pair"]
    assert p is not None
    accuracy.set_label(rig.conn, p["pair_id"], "same", NOW)
    assert accuracy.state(rig.conn, rig.crops)["results"] is None
    label_all(rig)
    done = accuracy.state(rig.conn, rig.crops)
    assert done["pair"] is None and done["results"] is not None and done["done"] == 4


def test_empty_table_has_no_results(rig: Rig) -> None:
    st = accuracy.state(rig.conn, rig.crops)
    assert st == {"total": 0, "done": 0, "pair": None, "results": None}


def test_tiny_negative_distance_lands_in_first_band(rig: Rig) -> None:
    insert_pair(rig, "split", "a", "b", 1, 2, -1e-16, "different")
    bands = accuracy.results(rig.conn)["bands"]
    assert bands[0]["lo"] == 0.0 and bands[0]["different"] == 1
    assert sum(b["different"] for b in bands) == 1


def test_rates_and_no_data(rig: Rig) -> None:
    insert_pair(rig, "split", "a", "b", 1, 2, 0.3, "same")
    insert_pair(rig, "split", "c", "d", 1, 2, 0.3, "different")
    insert_pair(rig, "split", "e", "f", 1, 2, 0.3, "unsure")
    res = accuracy.results(rig.conn)
    assert res["split"]["hits"] == 1 and res["split"]["n"] == 2
    assert res["split"]["rate"] == 0.5
    assert res["join"]["n"] == 0 and res["join"]["rate"] is None
    assert res["labeled"] == 3 and res["unsure"] == 1


def test_duplicates_follow_redirects_and_drop_merged_pairs(rig: Rig) -> None:
    for cid in (1, 2, 3, 4, 5):
        rig.cluster(cid)
    insert_pair(rig, "split", "a", "b", 1, 2, 0.30, "same")  # stays
    insert_pair(rig, "split", "c", "d", 3, 4, 0.31, "same")  # 3 merged into 4
    insert_pair(rig, "split", "e", "f", 5, 99, 0.32, "same")  # 99 never existed
    insert_pair(rig, "join", "g", "h", 5, 5, 0.50, "different")
    insert_pair(rig, "join", "i", "j", 7, 7, 0.40, "different")  # 7 -> 2
    rig.conn.execute("DELETE FROM face_clusters WHERE cluster_id = 3")
    rig.conn.execute("INSERT INTO face_cluster_redirects VALUES (3, 4, 1.0)")
    rig.conn.execute("INSERT INTO face_cluster_redirects VALUES (7, 2, 1.0)")
    rig.conn.commit()
    res = accuracy.results(rig.conn)
    assert res["duplicates"] == [
        {"a": 1, "a_handle": handle_for(1), "b": 2, "b_handle": handle_for(2), "distance": 0.3}
    ]
    assert res["wrong_joins"] == [
        {"cluster": 2, "handle": handle_for(2), "event_ids": ["i", "j"], "distance": 0.4},
        {"cluster": 5, "handle": handle_for(5), "event_ids": ["g", "h"], "distance": 0.5},
    ]


# --- routes -------------------------------------------------------------


def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, auth: bool) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=auth),
        face_enrich=FaceEnrichSection(cameras=["gate-face"], crops_dir=tmp_path / "crops"),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path, False)
    c = TestClient(create_app(settings))
    r = Rig(sidecar_db_path, tmp_path / "crops")
    populate(r, 4, 3)
    r.conn.close()
    return c


def test_page_renders_with_tab_and_thresholds(client: TestClient) -> None:
    r = client.get("/enrich/accuracy")
    assert r.status_code == 200
    assert 'data-match-threshold="0.45"' in r.text
    assert "face_accuracy.js" in r.text
    assert 'aria-current="page">Accuracy' in r.text
    assert "/enrich/accuracy" in client.get("/enrich/clusters").text


def test_draw_label_flow(client: TestClient) -> None:
    st = client.get("/enrich/accuracy/state.json").json()
    assert st == {"total": 0, "done": 0, "pair": None, "results": None}
    r = client.post("/enrich/accuracy/draw", json={"count": 4})
    assert r.status_code == 200
    st = r.json()
    assert st["total"] == 4 and st["pair"] is not None and st["results"] is None
    # Pending pairs: a second draw must not pile up.
    assert client.post("/enrich/accuracy/draw", json={"count": 4}).status_code == 409
    for _ in range(4):
        pid = st["pair"]["pair_id"] if st["pair"] else None
        assert pid is not None
        st = client.post(f"/enrich/accuracy/pairs/{pid}/label", json={"label": "different"}).json()
    assert st["pair"] is None and st["results"]["labeled"] == 4
    # Back: clearing the last label returns it.
    back = client.post(f"/enrich/accuracy/pairs/{pid}/label", json={"label": None}).json()
    assert back["pair"]["pair_id"] == pid and back["results"] is None
    # A fresh draw after everything is labelled is allowed.
    client.post(f"/enrich/accuracy/pairs/{pid}/label", json={"label": "unsure"})
    assert client.post("/enrich/accuracy/draw", json={"count": 2}).status_code == 200


def test_draw_defaults_and_bounds(client: TestClient) -> None:
    assert client.post("/enrich/accuracy/draw", json={"count": 0}).status_code == 422
    assert client.post("/enrich/accuracy/draw", json={"count": 201}).status_code == 422
    r = client.post("/enrich/accuracy/draw", json={})
    assert r.status_code == 200 and r.json()["total"] > 0


def test_draw_since_returns_drawn_and_stores_cutoff(
    client: TestClient, sidecar_db_path: Path
) -> None:
    cut = NOW + 3 * HOUR
    r = client.post("/enrich/accuracy/draw", json={"count": 4, "since": cut})
    assert r.status_code == 200
    body = r.json()
    assert body["drawn"] == body["total"] > 0
    conn = db.open_sidecar(sidecar_db_path)
    try:
        vals = {x[0] for x in conn.execute("SELECT drawn_since FROM face_pair_labels")}
    finally:
        conn.close()
    assert vals == {cut}
    # Pending pairs still block, with or without since.
    assert client.post("/enrich/accuracy/draw", json={"since": cut}).status_code == 409


def test_draw_since_nothing_qualifies(client: TestClient) -> None:
    r = client.post("/enrich/accuracy/draw", json={"since": NOW + 100 * HOUR})
    assert r.status_code == 200
    assert r.json()["drawn"] == 0 and r.json()["total"] == 0


def test_draw_without_since_reports_drawn(client: TestClient) -> None:
    body = client.post("/enrich/accuracy/draw", json={"count": 4}).json()
    assert body["drawn"] == 4


def test_draw_rejects_future_or_negative_since(client: TestClient) -> None:
    future = NOW * 2
    assert client.post("/enrich/accuracy/draw", json={"since": future}).status_code == 422
    assert client.post("/enrich/accuracy/draw", json={"since": -1}).status_code == 422


def test_label_errors(client: TestClient) -> None:
    client.post("/enrich/accuracy/draw", json={"count": 2})
    assert (
        client.post("/enrich/accuracy/pairs/9999/label", json={"label": "same"}).status_code == 404
    )
    bad = client.post("/enrich/accuracy/pairs/1/label", json={"label": "maybe"})
    assert bad.status_code == 422
    assert client.post("/enrich/accuracy/pairs/1/label", json={}).status_code == 422


def test_routes_behind_same_auth_as_clusters(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path, True)
    authed = TestClient(create_app(settings))
    expected = authed.get("/enrich/clusters").status_code
    assert expected in (401, 403)
    assert authed.get("/enrich/accuracy").status_code == expected
    assert authed.get("/enrich/accuracy/state.json").status_code == expected
    assert authed.post("/enrich/accuracy/draw", json={}).status_code == expected
    assert (
        authed.post("/enrich/accuracy/pairs/1/label", json={"label": "same"}).status_code
        == expected
    )


def test_pure_python_fallback_matches_numpy(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    populate(rig, 5, 3)
    rig.draw(8)
    with_np = [(r["kind"], r["event_a"], r["event_b"]) for r in rig.pairs()]
    rig.conn.execute("DELETE FROM face_pair_labels")
    rig.conn.commit()
    monkeypatch.setitem(__import__("sys").modules, "numpy", None)
    rig.draw(8)
    assert [(r["kind"], r["event_a"], r["event_b"]) for r in rig.pairs()] == with_np
