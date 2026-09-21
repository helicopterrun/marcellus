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
        edges=frozenset(
            {frozenset({"alley-wide", "shed"}), frozenset({"alley-wide", "porch"})}
        )
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
