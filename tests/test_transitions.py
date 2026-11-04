"""encounters/transitions.py: pair eligibility, MAD-trimmed percentiles,
learn()'s learned/config/default precedence, idempotency, load_transitions
round-trip, and service-level throttling (docs/encounters.md "Camera
topology")."""

from __future__ import annotations

import time
from pathlib import Path

from marcellus import db
from marcellus.config import EncountersSection, FrigateSection, Settings, SidecarSection
from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.service import EncounterService
from marcellus.encounters.store import upsert_atom
from marcellus.encounters.transitions import (
    MemberRow,
    TransitionConfig,
    collect_samples,
    learn,
    load_transitions,
    summarise,
    transition_config_from_settings,
)

_ADJ = Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})}))
_CFG = TransitionConfig(
    min_samples=3, max_sample_s=180.0, default={"p10": 2.0, "p50": 15.0, "p90": 60.0}
)


def _row(
    atom_id: str,
    camera: str,
    start: float,
    end: float | None,
    *,
    encounter_id: str = "e1",
    labels: tuple[str, ...] = ("person",),
) -> MemberRow:
    return MemberRow(
        encounter_id=encounter_id,
        atom_id=atom_id,
        camera=camera,
        start_time=start,
        end_time=end,
        labels=labels,
    )


# --------------------------------------------------------------------------
# collect_samples: pair eligibility
# --------------------------------------------------------------------------


def test_consecutive_adjacent_shared_family_is_a_sample() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "shed", 15.0, 25.0),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out[("alley-wide", "shed", "person")] == [5.0]


def test_non_consecutive_rejected() -> None:
    """A third member on a THIRD camera between m1 and the shed member means
    the shed member is no longer m1's next-different-camera hop."""
    adj = Adjacency(
        edges=frozenset({frozenset({"alley-wide", "shed"}), frozenset({"alley-wide", "porch"})})
    )
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "porch", 12.0, 20.0),
        _row("a3", "shed", 22.0, 30.0),
    ]
    out = collect_samples(members, adj, _CFG)
    assert ("alley-wide", "shed", "person") not in out
    assert out[("alley-wide", "porch", "person")] == [2.0]


def test_same_camera_pair_skipped() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "alley-wide", 15.0, 25.0),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out == {}


def test_non_adjacent_pair_rejected() -> None:
    adj = Adjacency(edges=frozenset())
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "shed", 15.0, 25.0),
    ]
    out = collect_samples(members, adj, _CFG)
    assert out == {}


def test_family_mismatch_rejected() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0, labels=("person",)),
        _row("a2", "shed", 15.0, 25.0, labels=("car",)),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out == {}


def test_shared_family_yields_one_sample_per_family() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0, labels=("person", "car")),
        _row("a2", "shed", 15.0, 25.0, labels=("person", "car")),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out[("alley-wide", "shed", "person")] == [5.0]
    assert out[("alley-wide", "shed", "vehicle")] == [5.0]


def test_split_decision_excludes_atom() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "shed", 15.0, 25.0),
    ]
    out = collect_samples(members, _ADJ, _CFG, split_atoms=frozenset({"a2"}))
    assert out == {}


def test_small_overlap_clamps_to_zero() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 20.0),
        _row("a2", "shed", 17.0, 25.0),  # 3 s overlap: a hand-off, clamped to 0
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out[("alley-wide", "shed", "person")] == [0.0]


def test_large_overlap_is_copresence_not_a_sample() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 20.0),
        _row("a2", "shed", 5.0, 25.0),  # 15 s overlap: both cameras saw it at once
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out == {}


def test_gap_beyond_max_excluded() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, 10.0),
        _row("a2", "shed", 1000.0, 1010.0),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out == {}


def test_open_m1_skipped() -> None:
    members = [
        _row("a1", "alley-wide", 0.0, None),
        _row("a2", "shed", 15.0, 25.0),
    ]
    out = collect_samples(members, _ADJ, _CFG)
    assert out == {}


# --------------------------------------------------------------------------
# summarise
# --------------------------------------------------------------------------


def test_summarise_empty_is_none() -> None:
    assert summarise([]) is None


def test_summarise_small_list_percentiles() -> None:
    p10, p50, p90 = summarise([1.0, 2.0, 3.0])  # type: ignore[misc]
    assert p50 == 2.0
    assert p10 < p50 < p90


def test_summarise_mad_trims_outlier() -> None:
    values = [10.0, 11.0, 9.0, 10.0, 11.0, 9.0, 10.0, 500.0]
    p10, p50, p90 = summarise(values)  # type: ignore[misc]
    assert p90 < 100.0  # the 500.0 outlier was trimmed out


# --------------------------------------------------------------------------
# learn()
# --------------------------------------------------------------------------


def _seed_atom(
    conn: object,
    atom_id: str,
    camera: str,
    start: float,
    end: float | None,
    now: float,
    *,
    encounter_id_hint: str | None = None,
) -> str:
    atom = Atom(
        atom_id=atom_id,
        camera=camera,
        start_time=start,
        end_time=end,
        labels=("person",),
        zones=(),
        event_ids=(f"ev-{atom_id}",),
        sub_labels=(),
        severity="detection",
    )
    reason = "new" if encounter_id_hint is None else "adjacent"
    decision = LinkDecision(encounter_id_hint, reason, 1.0)
    return upsert_atom(conn, atom, decision, now)  # type: ignore[arg-type]


def test_learn_writes_learned_config_default(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        # Give alley-wide/shed >= min_samples person samples (all ~5s gap).
        enc_id = None
        for i in range(4):
            base = now - 1000 + i * 100
            enc_id = _seed_atom(conn, f"a{i}", "alley-wide", base, base + 10, now)
            _seed_atom(conn, f"b{i}", "shed", base + 15, base + 25, now, encounter_id_hint=enc_id)

        cfg = TransitionConfig(
            min_samples=3,
            max_sample_s=180.0,
            default={"p10": 2.0, "p50": 15.0, "p90": 60.0},
            overrides={"shed>alley-wide": {"p10": 1.0, "p50": 2.0, "p90": 3.0}},
        )
        written = learn(conn, adjacency=_ADJ, cfg=cfg, window_s=86400.0, now=now)
        assert written > 0

        rows = {
            (r["cam_a"], r["cam_b"], r["family"]): r
            for r in conn.execute("SELECT * FROM camera_transitions").fetchall()
        }
        learned = rows[("alley-wide", "shed", "person")]
        assert learned["source"] == "learned"
        assert learned["samples"] == 4

        configured = rows[("shed", "alley-wide", "person")]
        assert configured["source"] == "config"
        assert configured["p50_s"] == 2.0

        default_row = rows[("alley-wide", "shed", "vehicle")]
        assert default_row["source"] == "default"
        assert default_row["p50_s"] == 15.0
    finally:
        conn.close()


def test_learn_idempotent(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = _seed_atom(conn, "a0", "alley-wide", now - 100, now - 90, now)
        _seed_atom(conn, "b0", "shed", now - 85, now - 75, now, encounter_id_hint=enc_id)

        key = lambda r: (r["cam_a"], r["cam_b"], r["family"])  # noqa: E731
        learn(conn, adjacency=_ADJ, cfg=_CFG, window_s=86400.0, now=now)
        rows = conn.execute("SELECT * FROM camera_transitions")
        first = sorted((dict(r) for r in rows), key=key)
        learn(conn, adjacency=_ADJ, cfg=_CFG, window_s=86400.0, now=now)
        rows2 = conn.execute("SELECT * FROM camera_transitions")
        second = sorted((dict(r) for r in rows2), key=key)
        assert first == second
    finally:
        conn.close()


def test_load_transitions_round_trip(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        enc_id = _seed_atom(conn, "a0", "alley-wide", now - 100, now - 90, now)
        _seed_atom(conn, "b0", "shed", now - 85, now - 75, now, encounter_id_hint=enc_id)
        learn(conn, adjacency=_ADJ, cfg=_CFG, window_s=86400.0, now=now)

        loaded = load_transitions(conn)
        assert ("alley-wide", "shed", "person") in loaded
        stats = loaded[("alley-wide", "shed", "person")]
        assert stats.samples == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Service throttling
# --------------------------------------------------------------------------


def test_service_throttles_learning_via_encounter_state(
    tmp_path: Path, frigate_db_path: Path
) -> None:
    cfg_path = tmp_path / "frigate-config.yml"
    cfg_path.write_text(
        "cameras:\n"
        "  alley-wide:\n"
        "    zones:\n"
        "      back_walkway:\n"
        "        coordinates: '0,0,1,0,1,1,0,1'\n"
        "  shed:\n"
        "    zones:\n"
        "      back_walkway:\n"
        "        coordinates: '0,0,1,0,1,1,0,1'\n"
    )
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg_path, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
        encounters=EncountersSection(
            enabled=True, transitions_enabled=True, transition_learn_interval_s=3600.0
        ),
    )
    now = time.time()
    service = EncounterService(settings, adjacency=_ADJ, now=lambda: now)

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        service._maybe_learn_transitions(conn, now)
        first_rows = conn.execute("SELECT COUNT(*) AS n FROM camera_transitions").fetchone()["n"]
        assert first_rows > 0

        conn.execute("DELETE FROM camera_transitions")
        conn.commit()
        # Second call within the throttle window must be a no-op.
        service._maybe_learn_transitions(conn, now + 1.0)
        second_rows = conn.execute("SELECT COUNT(*) AS n FROM camera_transitions").fetchone()["n"]
        assert second_rows == 0
    finally:
        conn.close()


def test_transition_config_from_settings() -> None:
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://x", config_path=Path("/nope"), db_path=Path("/nope")
        ),
        encounters=EncountersSection(transition_min_samples=5),
    )
    cfg = transition_config_from_settings(settings)
    assert cfg.min_samples == 5
    assert cfg.default["p50"] == 15.0


# --------------------------------------------------------------------------
# Overlap: count_concurrency / overlap_verdict / learn
# --------------------------------------------------------------------------

from marcellus.encounters.transitions import (  # noqa: E402
    OverlapStats,
    count_concurrency,
    load_overlap,
    overlap_verdict,
)

_TRIO = Adjacency(
    edges=frozenset(
        {
            frozenset({"stairway-wide", "alley-wide"}),
            frozenset({"stairway-wide", "stairway-tight"}),
            frozenset({"shed", "stairway-wide"}),
        }
    )
)
_OCFG = TransitionConfig(
    min_samples=8, max_sample_s=180.0, default={"p10": 2.0, "p50": 15.0, "p90": 60.0}
)


def _pair_members(
    cam1: str, cam2: str, gaps: list[float], *, prefix: str, t0: float = 1000.0
) -> list[MemberRow]:
    out: list[MemberRow] = []
    for i, gap in enumerate(gaps):
        base = t0 + i * 1000
        enc = f"{prefix}{i}"
        out.append(_row(f"{enc}a", cam1, base, base + 30, encounter_id=enc))
        out.append(_row(f"{enc}b", cam2, base + 30 + gap, base + 60 + gap, encounter_id=enc))
    return out


def test_trio_overlap_learned_true() -> None:
    gaps = [-23.0, 0.0, 0.3, -2.0, 0.0, 0.0, -9.0, 0.5, 0.0, 0.2, 1.0, 12.0]
    members = _pair_members("stairway-wide", "alley-wide", gaps, prefix="t")
    counts = count_concurrency(members, _TRIO, _OCFG)
    pair = frozenset({"stairway-wide", "alley-wide"})
    assert counts[pair] == (11, 12)
    v = overlap_verdict(pair, counts, _OCFG)
    assert v == OverlapStats(True, "learned", 11, 12)


def test_handoff_pair_not_overlap() -> None:
    fwd = _pair_members("shed", "stairway-wide", [1.1 + 0.02 * i for i in range(38)], prefix="f")
    back = _pair_members(
        "stairway-wide", "shed", [0.3 + 0.06 * i for i in range(11)], prefix="r", t0=5e6
    )
    counts = count_concurrency(fwd + back, _TRIO, _OCFG)
    pair = frozenset({"shed", "stairway-wide"})
    c, n = counts[pair]
    assert (c, n) == (11, 49)
    v = overlap_verdict(pair, counts, _OCFG)
    assert not v.overlap and v.source == "learned"


def test_below_min_samples_is_default() -> None:
    members = _pair_members("stairway-wide", "alley-wide", [0.0, 0.0, 0.0], prefix="d")
    counts = count_concurrency(members, _TRIO, _OCFG)
    v = overlap_verdict(frozenset({"stairway-wide", "alley-wide"}), counts, _OCFG)
    assert v == OverlapStats(False, "default", 3, 3)


def test_count_concurrency_counts_once_per_pair_not_family() -> None:
    members = [
        _row("a", "alley-wide", 0.0, 10.0, labels=("person", "car")),
        _row("b", "shed", 10.0, 20.0, labels=("person", "car")),
    ]
    counts = count_concurrency(members, _ADJ, _CFG)
    assert counts[frozenset({"alley-wide", "shed"})] == (1, 1)


def test_collect_samples_unchanged_for_big_overlap_drop() -> None:
    members = [
        _row("a", "alley-wide", 0.0, 100.0),
        _row("b", "shed", 10.0, 120.0),
    ]
    assert collect_samples(members, _ADJ, _CFG) == {}
    assert count_concurrency(members, _ADJ, _CFG)[frozenset({"alley-wide", "shed"})] == (1, 1)


def test_config_overrides_in_verdict() -> None:
    pair = frozenset({"alley-wide", "shed"})
    both = TransitionConfig(
        min_samples=3,
        max_sample_s=180.0,
        default={},
        overlap=frozenset({pair}),
        not_overlap=frozenset({pair}),
    )
    assert overlap_verdict(pair, {pair: (0, 10)}, both) == OverlapStats(False, "config", 0, 10)
    only = TransitionConfig(
        min_samples=3, max_sample_s=180.0, default={}, overlap=frozenset({pair})
    )
    assert overlap_verdict(pair, {}, only) == OverlapStats(True, "config", 0, 0)


def _seed_pairs(conn: object, gaps: list[float], now: float) -> None:
    for i, gap in enumerate(gaps):
        base = now - 5000 + i * 100
        enc = _seed_atom(conn, f"oa{i}", "alley-wide", base, base + 10, now)
        _seed_atom(
            conn, f"ob{i}", "shed", base + 10 + gap, base + 20 + gap, now, encounter_id_hint=enc
        )


def test_learn_overlap_excludes_concurrent_from_walking_times(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        _seed_pairs(conn, [0.0, 0.2, 0.5, 0.0, 0.1, 0.3, 2.0, 4.0, 0.0, 0.0], now)
        cfg = TransitionConfig(
            min_samples=3, max_sample_s=180.0, default={"p10": 2.0, "p50": 15.0, "p90": 60.0}
        )
        learn(conn, adjacency=_ADJ, cfg=cfg, window_s=86400.0, now=now)
        ov = load_overlap(conn)[frozenset({"alley-wide", "shed"})]
        assert ov == OverlapStats(True, "learned", 8, 10)
        row = conn.execute(
            "SELECT * FROM camera_transitions WHERE cam_a='alley-wide' AND cam_b='shed' "
            "AND family='person'"
        ).fetchone()
        assert row["samples"] == 2  # only the 2.0 s and 4.0 s samples
        assert row["source"] == "default"  # < min_samples after exclusion
        assert row["p50_s"] == 15.0
        stored = conn.execute("SELECT cam_a, cam_b, overlap FROM camera_overlap").fetchall()
        assert [(r["cam_a"], r["cam_b"], r["overlap"]) for r in stored] == [
            ("alley-wide", "shed", 1)
        ]
    finally:
        conn.close()


def test_learn_non_overlap_keeps_subsecond_sample(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        _seed_pairs(conn, [0.5, 3.0, 4.0, 5.0, 6.0], now)
        cfg = TransitionConfig(
            min_samples=3, max_sample_s=180.0, default={"p10": 2.0, "p50": 15.0, "p90": 60.0}
        )
        learn(conn, adjacency=_ADJ, cfg=cfg, window_s=86400.0, now=now)
        assert load_overlap(conn)[frozenset({"alley-wide", "shed"})].overlap is False
        row = conn.execute(
            "SELECT samples FROM camera_transitions WHERE cam_a='alley-wide' AND "
            "cam_b='shed' AND family='person'"
        ).fetchone()
        assert row["samples"] == 5
    finally:
        conn.close()


def test_learn_config_not_overlap_beats_overlap(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        now = time.time()
        _seed_pairs(conn, [0.0] * 6, now)
        pair = frozenset({"alley-wide", "shed"})
        cfg = TransitionConfig(
            min_samples=3,
            max_sample_s=180.0,
            default={},
            overlap=frozenset({pair}),
            not_overlap=frozenset({pair}),
        )
        learn(conn, adjacency=_ADJ, cfg=cfg, window_s=86400.0, now=now)
        assert load_overlap(conn)[pair] == OverlapStats(False, "config", 6, 6)
    finally:
        conn.close()


def test_transition_config_carries_overlap_settings() -> None:
    s = Settings(
        frigate=FrigateSection(base_url="http://f.test:5000"),
        encounters=EncountersSection(
            overlap=[["a", "b"]],
            not_overlap=[["c", "d"]],
            overlap_concurrent_max_s=2.0,
            overlap_min_fraction=0.5,
        ),
    )
    cfg = transition_config_from_settings(s)
    assert cfg.overlap == frozenset({frozenset({"a", "b"})})
    assert cfg.not_overlap == frozenset({frozenset({"c", "d"})})
    assert cfg.overlap_concurrent_max_s == 2.0
    assert cfg.overlap_min_fraction == 0.5
