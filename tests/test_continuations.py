"""encounters/continuations.py: factor isolation, bucket boundaries, the
never-confirmed-from-score rule, unlearned-stats cap, direction
renormalisation, and predict_window defaults (docs/encounters.md "Suggested
continuations")."""

from __future__ import annotations

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.continuations import (
    ContinuationConfig,
    ContinuationWeights,
    Source,
    bucket,
    predict_window,
    score_candidate,
)
from marcellus.encounters.transitions import TransitionStats

_CFG = ContinuationConfig()

_SOURCE = Source(
    camera="alley-wide",
    end_time=1000.0,
    labels=("person",),
    last_zone="back_walkway",
    encounter_id="e1",
    atom_id="a1",
)

_ADJ_WITH_ZONES = Adjacency(
    edges=frozenset({frozenset({"alley-wide", "shed"})}),
    shared_zones={frozenset({"alley-wide", "shed"}): ("back_walkway",)},
)

_ADJ_CONFIG_ONLY = Adjacency(edges=frozenset({frozenset({"alley-wide", "shed"})}))

_ADJ_NONE = Adjacency(edges=frozenset())

_LEARNED = TransitionStats(p10=10.0, p50=20.0, p90=30.0, samples=20, source="learned")


# --------------------------------------------------------------------------
# class (C) factor
# --------------------------------------------------------------------------


def test_class_factor_same_label() -> None:
    score, why = score_candidate(
        _SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    assert any("same label" in w for w in why)


def test_class_factor_same_family_different_label_scores_lower() -> None:
    same_label, _ = score_candidate(
        _SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    diff_label, _ = score_candidate(
        _SOURCE,
        "shed",
        "car",
        15.0,
        _LEARNED,
        _ADJ_WITH_ZONES,
        _CFG,
    )
    assert diff_label < same_label


# --------------------------------------------------------------------------
# topology (T) factor
# --------------------------------------------------------------------------


def test_topo_edge_with_shared_zones_scores_full() -> None:
    score, why = score_candidate(
        _SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    assert score > 0
    assert any("edge" in w for w in why)


def test_topo_learned_only_edge_scores_but_lower_than_adjacency_edge() -> None:
    adjacent_score, _ = score_candidate(
        _SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    learned_only_score, why = score_candidate(
        _SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_NONE, _CFG
    )
    assert learned_only_score < adjacent_score
    assert any("learned transition" in w for w in why)


def test_topo_non_edge_without_learned_row_is_not_a_candidate() -> None:
    score, why = score_candidate(_SOURCE, "shed", "person", 15.0, None, _ADJ_NONE, _CFG)
    assert score == 0.0
    assert why == []


# --------------------------------------------------------------------------
# elapsed (E) factor
# --------------------------------------------------------------------------


def test_elapsed_none_is_a_prediction_with_full_time_factor() -> None:
    score, why = score_candidate(_SOURCE, "shed", "person", None, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert score > 0
    assert any("prediction" in w or "typical" in w for w in why)


def test_elapsed_inside_p10_p90_scores_full_time_factor() -> None:
    inside, _ = score_candidate(_SOURCE, "shed", "person", 20.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    early, _ = score_candidate(_SOURCE, "shed", "person", 0.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert inside > early


def test_elapsed_late_decays_to_zero_at_p90_times_late_factor() -> None:
    late_edge = _LEARNED.p90 * _CFG.late_factor
    score_at_edge, _ = score_candidate(
        _SOURCE, "shed", "person", late_edge, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    score_before_edge, _ = score_candidate(
        _SOURCE, "shed", "person", _LEARNED.p90 + 1.0, _LEARNED, _ADJ_WITH_ZONES, _CFG
    )
    assert score_at_edge < score_before_edge


def test_unlearned_stats_cap_the_time_factor() -> None:
    default_stats = TransitionStats(p10=10.0, p50=20.0, p90=30.0, samples=1, source="default")
    capped, _ = score_candidate(
        _SOURCE, "shed", "person", 20.0, default_stats, _ADJ_WITH_ZONES, _CFG
    )
    uncapped, _ = score_candidate(_SOURCE, "shed", "person", 20.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert capped < uncapped


def test_stats_none_also_caps_the_time_factor() -> None:
    capped, _ = score_candidate(_SOURCE, "shed", "person", 20.0, None, _ADJ_WITH_ZONES, _CFG)
    uncapped, _ = score_candidate(_SOURCE, "shed", "person", 20.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert capped <= uncapped


# --------------------------------------------------------------------------
# direction (D) factor
# --------------------------------------------------------------------------


def test_direction_shared_zone_match_scores_full() -> None:
    matching = Source(
        camera="alley-wide",
        end_time=1000.0,
        labels=("person",),
        last_zone="back_walkway",
        encounter_id="e1",
        atom_id="a1",
    )
    score, why = score_candidate(matching, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    other = Source(
        camera="alley-wide",
        end_time=1000.0,
        labels=("person",),
        last_zone="front_yard",
        encounter_id="e1",
        atom_id="a1",
    )
    other_score, _ = score_candidate(other, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert score > other_score
    assert any("exit zone back_walkway" in w for w in why)


def test_direction_unknown_last_zone_scores_unknown_direction_weight() -> None:
    unknown = Source(
        camera="alley-wide",
        end_time=1000.0,
        labels=("person",),
        last_zone="",
        encounter_id="e1",
        atom_id="a1",
    )
    _score, why = score_candidate(unknown, "shed", "person", 15.0, _LEARNED, _ADJ_WITH_ZONES, _CFG)
    assert any("exit zone unknown" in w for w in why)


def test_direction_renormalises_weights_on_config_only_edge() -> None:
    """A config-only edge (no shared-zone info) drops the direction factor
    entirely rather than treating it as a penalty."""
    score, why = score_candidate(_SOURCE, "shed", "person", 15.0, _LEARNED, _ADJ_CONFIG_ONLY, _CFG)
    assert not any("exit zone" in w for w in why)
    assert 0.0 < score <= 1.0


# --------------------------------------------------------------------------
# bucket()
# --------------------------------------------------------------------------


def test_bucket_boundaries() -> None:
    assert bucket(0.5, _CFG, existing_same_encounter=False, pinned=False) == "likely"
    assert bucket(0.49, _CFG, existing_same_encounter=False, pinned=False) == "possible"
    assert bucket(0.25, _CFG, existing_same_encounter=False, pinned=False) == "possible"
    assert bucket(0.24, _CFG, existing_same_encounter=False, pinned=False) is None
    assert bucket(0.0, _CFG, existing_same_encounter=False, pinned=False) is None


def test_machine_prediction_never_confirmed_regardless_of_score() -> None:
    assert bucket(1.0, _CFG, existing_same_encounter=False, pinned=False) != "confirmed"


def test_existing_same_encounter_is_always_confirmed() -> None:
    assert bucket(0.0, _CFG, existing_same_encounter=True, pinned=False) == "confirmed"


def test_pinned_is_always_confirmed() -> None:
    assert bucket(0.0, _CFG, existing_same_encounter=False, pinned=True) == "confirmed"


# --------------------------------------------------------------------------
# predict_window()
# --------------------------------------------------------------------------


def test_predict_window_uses_p10_and_p90_times_max_window_factor() -> None:
    t0, t1 = predict_window(1000.0, _LEARNED, _CFG)
    assert t0 == 1000.0 + _LEARNED.p10
    assert t1 == 1000.0 + _LEARNED.p90 * _CFG.max_window_factor


def test_predict_window_defaults_to_zero_stats_when_none() -> None:
    t0, t1 = predict_window(1000.0, None, _CFG)
    assert t0 == 1000.0
    assert t1 == 1000.0


def test_weights_dataclass_defaults() -> None:
    w = ContinuationWeights()
    assert (w.topo, w.time, w.direction, w.klass) == (0.30, 0.30, 0.20, 0.20)
