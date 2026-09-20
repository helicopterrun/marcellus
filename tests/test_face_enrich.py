"""Face enrichment: the pure-Python decision layer, no [enrich] deps needed.

Everything here runs in CI's [dev]-only environment by design — embedding
math, quality scoring, candidate discovery, cluster assignment, and the reaper
are all plain Python (faces/enrich.py's module docstring has the split).
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from marcellus import db
from marcellus.faces import enrich
from tests.conftest import FRIGATE_EVENT_SCHEMA


def _cfg(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "cameras": ["gate-face"],
        "interval_s": 15.0,
        "process_delay_s": 45.0,
        "lookback_s": 3600.0,
        "max_frames": 40,
        "min_sample_gap_s": 1.0,
        "best_n": 5,
        "min_face_area_px": 4000,
        "min_quality": 0.15,
        "match_threshold": 0.45,
        "cluster_threshold": 0.55,
        "cluster_ttl_days": 60,
        "model_dir": "/tmp/models",
        "max_events_per_cycle": 10,
        "max_attempts": 3,
        "http_timeout_s": 15.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _unit(dim: int, axis: int) -> list[float]:
    v = [0.0] * dim
    v[axis] = 1.0
    return v


def _joined(
    tmp_path: Path, events: list[tuple[str, str, float, float | None]] | None = None
) -> sqlite3.Connection:
    """Joined conn; `events` (id, camera, start, end) must be seeded up front —
    the frigate side is attached read-only, exactly as in production."""
    frigate = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate)
    conn.executescript(FRIGATE_EVENT_SCHEMA)
    for eid, cam, start, end in events or []:
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time) "
            "VALUES (?, ?, 'person', ?, ?)",
            (eid, cam, start, end),
        )
    conn.commit()
    conn.close()
    return db.open_joined(frigate, tmp_path / "sidecar.db")


# ---------------------------------------------------------------------------
# Embedding math


def test_pack_unpack_roundtrip() -> None:
    vec = [0.25, -1.5, 3.0, 0.0]
    assert enrich.unpack_embedding(enrich.pack_embedding(vec)) == vec


def test_l2_normalize_and_cosine() -> None:
    a = enrich.l2_normalize([3.0, 4.0])
    assert math.isclose(math.hypot(*a), 1.0)
    assert math.isclose(enrich.cosine_distance(a, a), 0.0, abs_tol=1e-9)
    assert math.isclose(enrich.cosine_distance([1.0, 0.0], [0.0, 1.0]), 1.0)


def test_aggregate_weights_pull_toward_higher_quality() -> None:
    a, b = [1.0, 0.0], [0.0, 1.0]
    agg = enrich.aggregate([a, b], [0.9, 0.1])
    assert agg[0] > agg[1]
    assert math.isclose(math.hypot(*agg), 1.0)


def test_aggregate_zero_weights_falls_back_to_uniform() -> None:
    agg = enrich.aggregate([[1.0, 0.0], [0.0, 1.0]], [0.0, 0.0])
    assert math.isclose(agg[0], agg[1])


def test_aggregate_empty_raises() -> None:
    with pytest.raises(ValueError):
        enrich.aggregate([], [])


# ---------------------------------------------------------------------------
# Quality


def test_pose_frontality_frontal_beats_profile() -> None:
    # Frontal: nose centered between level eyes.
    frontal = [(100.0, 100.0), (200.0, 100.0), (150.0, 150.0), (110.0, 200.0), (190.0, 200.0)]
    # Profile: nose pushed past the right eye.
    profile = [(100.0, 100.0), (140.0, 100.0), (150.0, 150.0), (105.0, 200.0), (140.0, 200.0)]
    assert enrich.pose_frontality(frontal) > 0.9
    assert enrich.pose_frontality(profile) < enrich.pose_frontality(frontal)


def test_pose_frontality_degenerate_inputs() -> None:
    assert enrich.pose_frontality([]) == 0.0
    assert enrich.pose_frontality([(0.0, 0.0)] * 5) == 0.0  # zero eye span


def test_quality_score_monotone_and_bounded() -> None:
    kw = {"area_px": 8000.0, "frontality": 1.0, "min_face_area_px": 4000}
    lo = enrich.quality_score(sharpness=50.0, **kw)
    hi = enrich.quality_score(sharpness=400.0, **kw)
    assert 0.0 < lo < hi <= 1.0
    assert enrich.quality_score(sharpness=1e6, area_px=1e9, frontality=1.0,
                                min_face_area_px=4000) == 1.0
    assert enrich.quality_score(sharpness=100.0, area_px=8000.0, frontality=0.0,
                                min_face_area_px=4000) == 0.0


# ---------------------------------------------------------------------------
# Sampling


def test_sample_times_short_event_yields_midpoint() -> None:
    assert enrich.sample_times(10.0, 10.5, max_frames=40, min_gap_s=1.0) == [10.25]


def test_sample_times_bounds_and_gap() -> None:
    ts = enrich.sample_times(0.0, 100.0, max_frames=40, min_gap_s=1.0)
    assert len(ts) == 40
    assert ts[0] == 0.0 and math.isclose(ts[-1], 100.0)
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    assert min(gaps) >= 1.0


def test_sample_times_gap_limits_count() -> None:
    ts = enrich.sample_times(0.0, 5.0, max_frames=40, min_gap_s=1.0)
    assert len(ts) == 6  # one per second inclusive


# ---------------------------------------------------------------------------
# Candidates


def test_find_candidates_due_settled_and_retry(tmp_path: Path) -> None:
    now = time.time()
    conn = _joined(
        tmp_path,
        events=[
            ("due", "gate-face", now - 700, now - 600),        # pending -> candidate
            ("too-fresh", "gate-face", now - 60, now - 10),    # inside process_delay
            ("other-cam", "doorbell", now - 700, now - 600),   # not enrolled
            ("settled", "gate-face", now - 900, now - 800),    # already enriched
            ("erred", "gate-face", now - 900, now - 800),      # error, attempts < cap
            ("exhausted", "gate-face", now - 900, now - 800),  # error, attempts = cap
            ("running", "gate-face", now - 300, None),         # still live
        ],
    )
    try:
        for eid, status, attempts in (
            ("settled", "enriched", 1), ("erred", "error", 1), ("exhausted", "error", 3)
        ):
            conn.execute(
                "INSERT INTO sidecar.face_enrichments "
                "(event_id, camera, event_start_ts, status, attempts, processed_at) "
                "VALUES (?, 'gate-face', ?, ?, ?, '')",
                (eid, now - 900, status, attempts),
            )
        conn.commit()
        got = enrich.find_candidates(conn, cfg=_cfg(), now=now)
        assert [c.event_id for c in got] == ["erred", "due"]
        assert got[0].attempts == 1 and got[1].attempts == 0
    finally:
        conn.close()


def test_find_candidates_no_cameras_is_empty(tmp_path: Path) -> None:
    conn = _joined(tmp_path)
    try:
        assert enrich.find_candidates(conn, cfg=_cfg(cameras=[]), now=time.time()) == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Clustering


def test_match_or_cluster_new_then_join_then_named(tmp_path: Path) -> None:
    conn = _joined(tmp_path)
    cfg = _cfg()
    try:
        e1 = _unit(4, 0)
        cid, name, dist = enrich.match_or_cluster(conn, e1, cfg=cfg, seen_ts=100.0)
        assert name is None and dist == 0.0

        # Nearby embedding joins the same cluster and bumps the count.
        close = enrich.l2_normalize([0.95, 0.05, 0.0, 0.0])
        cid2, name2, dist2 = enrich.match_or_cluster(conn, close, cfg=cfg, seen_ts=200.0)
        assert cid2 == cid and name2 is None and 0.0 < dist2 <= cfg.cluster_threshold
        row = conn.execute(
            "SELECT observation_count, last_seen_at FROM face_clusters WHERE cluster_id = ?",
            (cid,),
        ).fetchone()
        assert row["observation_count"] == 2 and row["last_seen_at"] == 200.0

        # An orthogonal embedding starts a fresh cluster.
        cid3, _, _ = enrich.match_or_cluster(conn, _unit(4, 1), cfg=cfg, seen_ts=300.0)
        assert cid3 != cid

        # Named cluster wins at the tighter threshold.
        conn.execute("UPDATE face_clusters SET name = 'alice' WHERE cluster_id = ?", (cid,))
        cid4, name4, _ = enrich.match_or_cluster(conn, e1, cfg=cfg, seen_ts=400.0)
        assert cid4 == cid and name4 == "alice"
    finally:
        conn.close()


def test_match_prefers_named_within_threshold_over_closer_unnamed(tmp_path: Path) -> None:
    conn = _joined(tmp_path)
    cfg = _cfg()
    try:
        probe = _unit(8, 0)
        named_centroid = enrich.l2_normalize([0.9, 0.4, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES ('bob', ?, 5, '', 0)",
            (enrich.pack_embedding(named_centroid),),
        )
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES (NULL, ?, 5, '', 0)",
            (enrich.pack_embedding(probe),),  # exact match, but unnamed
        )
        _, name, _ = enrich.match_or_cluster(conn, probe, cfg=cfg, seen_ts=1.0)
        assert name == "bob"
    finally:
        conn.close()


def test_rebuild_centroid_from_event_embeddings(tmp_path: Path) -> None:
    conn = _joined(tmp_path)
    try:
        conn.execute(
            "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
            "last_seen_at) VALUES (NULL, ?, 99, '', 0)",
            (enrich.pack_embedding(_unit(2, 1)),),  # stale/wrong running mean
        )
        cid = 1
        for i, emb in enumerate(([1.0, 0.0], [1.0, 0.0])):
            conn.execute(
                "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
                "embedding, status, processed_at) VALUES (?, 'c', 0, ?, ?, 'enriched', '')",
                (f"ev{i}", cid, enrich.pack_embedding(emb)),
            )
        enrich.rebuild_centroid(conn, cid)
        row = conn.execute(
            "SELECT centroid, observation_count FROM face_clusters WHERE cluster_id = ?", (cid,)
        ).fetchone()
        assert enrich.unpack_embedding(row["centroid"]) == [1.0, 0.0]
        assert row["observation_count"] == 2
    finally:
        conn.close()


def test_reap_stale_clusters_spares_named_and_fresh(tmp_path: Path) -> None:
    conn = _joined(tmp_path)
    cfg = _cfg(cluster_ttl_days=60)
    now = time.time()
    old = now - 61 * 86400
    try:
        for name, seen in (("alice", old), (None, old), (None, now - 10.0)):
            conn.execute(
                "INSERT INTO face_clusters (name, centroid, observation_count, created_at, "
                "last_seen_at) VALUES (?, ?, 1, '', ?)",
                (name, enrich.pack_embedding(_unit(2, 0)), seen),
            )
        conn.execute(
            "INSERT INTO face_enrichments (event_id, camera, event_start_ts, cluster_id, "
            "embedding, status, processed_at) VALUES ('e1', 'c', 0, 2, ?, 'enriched', '')",
            (enrich.pack_embedding(_unit(2, 0)),),
        )
        assert enrich.reap_stale_clusters(conn, cfg=cfg, now=now) == 1
        left = {r["cluster_id"] for r in conn.execute("SELECT cluster_id FROM face_clusters")}
        assert left == {1, 3}
        row = conn.execute(
            "SELECT cluster_id, embedding FROM face_enrichments WHERE event_id = 'e1'"
        ).fetchone()
        assert row["cluster_id"] is None and row["embedding"] is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Engine guard


def test_engine_unavailable_message_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in ("insightface", "insightface.app"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    with pytest.raises(enrich.EnrichUnavailable, match=r"\[enrich\]"):
        enrich.check_available()
