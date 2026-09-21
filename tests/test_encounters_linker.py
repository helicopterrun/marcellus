"""Pure-Python linker tests (docs/encounters.md "Tests" section)."""

from __future__ import annotations

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import (
    Atom,
    LinkerConfig,
    OpenEncounter,
    decide,
    family_of,
    fold,
    normalise_labels,
)
from marcellus.encounters.types import TransitionStats

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)

ADJ = Adjacency(
    edges=frozenset(
        {
            frozenset({"alley-wide", "shed"}),
            frozenset({"shed", "stairway-wide"}),
        }
    )
)

# A reference "now" for tests whose atoms all carry explicit end times (and
# so don't care what `now` is, as long as it's at or after every atom's own
# span) -- keeps call sites uniform without every test picking its own value.
NOW = 1_000_000.0


def _atom(
    atom_id: str,
    camera: str,
    start: float,
    end: float | None = None,
    labels: tuple[str, ...] = ("raccoon",),
    zones: tuple[str, ...] = (),
    sub_labels: tuple[str, ...] = (),
    severity: str = "detection",
) -> Atom:
    return Atom(
        atom_id=atom_id,
        camera=camera,
        start_time=start,
        end_time=end,
        labels=labels,
        zones=zones,
        event_ids=(),
        sub_labels=sub_labels,
        severity=severity,
    )


def test_raccoon_chain_joins_one_encounter() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "shed", 150.0, 180.0),
        _atom("a3", "stairway-wide", 330.0, 360.0),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2", "a3"]
    assert encs[0].cameras == ["alley-wide", "shed", "stairway-wide"]


def test_same_raccoon_after_long_gap_on_non_adjacent_camera_starts_new() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "street", 630.0, 660.0),  # 10 min later, not adjacent
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 2
    assert {tuple(e.atom_ids) for e in encs} == {("a1",), ("a2",)}


def test_person_non_adjacent_no_shared_zone_does_not_join() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0, labels=("person",)),
        _atom("a2", "street", 30.0, 40.0, labels=("person",)),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 2


def test_identity_match_joins_despite_no_adjacency() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=30.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities={"rex"},
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "street", 500.0, 520.0, sub_labels=("rex",))
    decision = decide(atom, [enc], ADJ, CFG, now=NOW)
    assert decision.encounter_id == "e1"
    assert decision.reason == "identity"
    assert decision.confidence == 0.95


def test_contradictory_sub_labels_split() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=30.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities={"fido"},
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "alley-wide", 40.0, 50.0, sub_labels=("rex",))
    decision = decide(atom, [enc], ADJ, CFG, now=NOW)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_companion_beats_adjacent_when_both_eligible() -> None:
    # person+dog on camera A; dog alone shows up on the adjacent camera B
    # 5s after A's segment started, while A is still ongoing -- overlapping
    # spans make this a companionship match, which outranks the plain
    # "adjacent" continuity match (0.7 > 0.6).
    atoms = [
        _atom("a1", "alley-wide", 0.0, 20.0, labels=("person", "dog")),
        _atom("a2", "shed", 5.0, 25.0, labels=("dog",)),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 1
    decision = decide(
        _atom("a2", "shed", 5.0, 25.0, labels=("dog",)),
        [
            OpenEncounter(
                encounter_id="e1",
                start_time=0.0,
                last_end=20.0,
                cameras=["alley-wide"],
                labels={"person", "dog"},
                identities=set(),
                zones=set(),
                atom_ids=["a1"],
                peak_severity="detection",
            )
        ],
        ADJ,
        CFG,
        now=NOW,
    )
    assert decision.reason == "companion"
    assert decision.confidence == 0.7


def test_sealed_encounters_are_not_candidates() -> None:
    # A "sealed" encounter is simply one the caller doesn't pass in -- store.py
    # filters those out via load_open(). Simulate that here: an encounter
    # that would obviously match is simply absent from open_encounters.
    atom = _atom("a2", "alley-wide", 40.0, 50.0)
    decision = decide(atom, [], ADJ, CFG, now=NOW)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_pin_and_split_decisions_honoured() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0),
        _atom("a2", "street", 15.0, 25.0),  # would otherwise start a new encounter
    ]
    open_encounters = fold(atoms, ADJ, CFG, now=NOW)
    assert len(open_encounters) == 2
    e1 = open_encounters[0].encounter_id
    e2 = open_encounters[1].encounter_id

    # Pin forces a3 onto e1 even though nothing would naturally link it.
    pin_atom = _atom("a3", "faraway", 9999.0, 10000.0)
    decision = decide(pin_atom, open_encounters, ADJ, CFG, now=NOW, pinned_to=e1)
    assert decision.encounter_id == e1
    assert decision.reason == "pinned"

    # Split excludes e1 from candidacy even for an atom that would join it.
    split_atom = _atom("a4", "alley-wide", 12.0, 20.0)
    decision = decide(split_atom, open_encounters, ADJ, CFG, now=NOW, split_from=frozenset({e1}))
    assert decision.encounter_id != e1
    # Without e1 in play there's nothing else to join (e2 is a different
    # camera+time entirely), so this correctly starts a new encounter.
    assert decision.encounter_id is None
    assert e2 != e1  # sanity: the two folded atoms really are separate encounters


def test_fold_is_idempotent() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "shed", 150.0, 180.0),
        _atom("a3", "street", 900.0, 920.0),
    ]
    first = fold(atoms, ADJ, CFG, now=NOW)
    second = fold(atoms, ADJ, CFG, now=NOW)
    first_groups = {frozenset(e.atom_ids) for e in first}
    second_groups = {frozenset(e.atom_ids) for e in second}
    assert first_groups == second_groups


def test_max_duration_cap_starts_new_encounter() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=10.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities=set(),
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "alley-wide", 2000.0, 2010.0)  # 2000s > max_duration_s
    decision = decide(atom, [enc], ADJ, CFG, now=NOW)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_open_ended_atom_extends_to_now_not_its_own_start() -> None:
    """A raccoon has been open (review segment still live, no "end" message
    yet) on alley-wide for 4 minutes when it shows up on the adjacent shed
    camera. The bug: substituting `start_time` for a None `end_time` made
    the open atom look instantaneous at t=0, so the gap to the shed atom
    computed as 245s (> gap_s["animal"]=180) and it wrongly started a new
    encounter. Fixed: an open atom extends to `now`, so the gap is ~0."""
    now = 245.0
    atoms = [
        _atom("a1", "alley-wide", 0.0, None),  # still open, no end message
        _atom("a2", "shed", 245.0, 250.0),
    ]
    encs = fold(atoms, ADJ, CFG, now=now)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2"]


def test_normalise_labels_plain_known_label() -> None:
    labels, qualifiers = normalise_labels(["person"])
    assert labels == ("person",)
    assert qualifiers == ()


def test_normalise_labels_hyphen_qualified_known_label() -> None:
    labels, qualifiers = normalise_labels(["person", "person-verified"])
    assert labels == ("person",)
    assert qualifiers == ("verified",)


def test_normalise_labels_unknown_object_is_qualifier_only() -> None:
    # A bare promoted sub_label (e.g. Frigate's "amazon") with no matching
    # base label in `objects` -- never added to labels.
    labels, qualifiers = normalise_labels(["person", "amazon"])
    assert labels == ("person",)
    assert qualifiers == ("amazon",)


def test_normalise_labels_hyphen_prefix_not_a_known_label_is_qualifier() -> None:
    # "amazon-driver" splits on the first hyphen to base "amazon", which
    # isn't a known label -- the whole string falls through to qualifier.
    labels, qualifiers = normalise_labels(["amazon-driver"])
    assert labels == ()
    assert qualifiers == ("amazon-driver",)


def test_normalise_labels_dedupes_and_preserves_first_appearance_order() -> None:
    labels, qualifiers = normalise_labels(
        ["car", "person", "person-verified", "person", "car-blue", "person-verified"]
    )
    assert labels == ("car", "person")
    assert qualifiers == ("verified", "blue")


def test_normalise_labels_splits_only_on_first_hyphen() -> None:
    labels, qualifiers = normalise_labels(["car-two-door"])
    assert labels == ("car",)
    assert qualifiers == ("two-door",)


def test_companion_joins_while_first_subject_still_on_camera() -> None:
    """A person is still on alley-wide (review open, no end message) when a
    dog appears on the adjacent shed camera 10s later. The bug: the open
    person atom was treated as ending at its own start (t=0), so its
    overlap with the dog atom's span computed as negative and companionship
    never matched. Fixed: the open atom extends to `now`, giving a real
    overlap."""
    now = 15.0
    atoms = [
        _atom("a1", "alley-wide", 0.0, None, labels=("person",)),  # still there
        _atom("a2", "shed", 10.0, 15.0, labels=("dog",)),
    ]
    encs = fold(atoms, ADJ, CFG, now=now)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2"]


def test_family_of_known_label_unchanged() -> None:
    assert family_of("person") == "person"


def test_family_of_unknown_label_is_itself_not_shared_default() -> None:
    # waste_bin and garage are both unnamed (not in LABEL_FAMILIES), but
    # each is its own family now -- they must not collide on "default".
    assert family_of("waste_bin") == "waste_bin"
    assert family_of("garage") == "garage"
    assert family_of("waste_bin") != family_of("garage")


def test_waste_bin_does_not_continue_garage_encounter() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0, labels=("garage",)),
        _atom("a2", "alley-wide", 15.0, 20.0, labels=("waste_bin",)),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    # No shared family, no companionship overlap (spans don't overlap) ->
    # two separate encounters.
    assert len(encs) == 2


def test_waste_bin_continues_another_waste_bin_atom() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0, labels=("waste_bin",)),
        _atom("a2", "alley-wide", 15.0, 20.0, labels=("waste_bin",)),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2"]


def test_shared_family_gap_uses_shared_allowance_not_extra_label() -> None:
    # Encounter is person-only. A later atom carries both person and car
    # labels -- the shared family is "person" (gap_s 90s), not "vehicle"
    # (gap_s 45s). A gap of 60s is within the person allowance but would
    # exceed the vehicle-only allowance, so this must still link.
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0, labels=("person",)),
        _atom("a2", "alley-wide", 70.0, 80.0, labels=("person", "car")),
    ]
    encs = fold(atoms, ADJ, CFG, now=NOW)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2"]


def _open_enc(
    *,
    encounter_id: str = "e1",
    start_time: float = 0.0,
    last_end: float = 30.0,
    cameras: list[str] | None = None,
    labels: set[str] | None = None,
    identities: set[str] | None = None,
    zones: set[str] | None = None,
    atom_ids: list[str] | None = None,
) -> OpenEncounter:
    return OpenEncounter(
        encounter_id=encounter_id,
        start_time=start_time,
        last_end=last_end,
        cameras=cameras if cameras is not None else ["alley-wide"],
        labels=labels if labels is not None else {"raccoon"},
        identities=identities if identities is not None else set(),
        zones=zones if zones is not None else set(),
        atom_ids=atom_ids if atom_ids is not None else ["a1"],
        peak_severity="detection",
    )


def _golden_scenarios() -> list[tuple[Atom, list[OpenEncounter]]]:
    """One scenario per `decide` reason, reused to assert `transitions=None`
    and an all-"default"-source `transitions` map behave identically."""
    return [
        # pinned
        (
            _atom("a2", "faraway", 9999.0, 10000.0),
            [_open_enc()],
        ),
        # identity
        (
            _atom("a2", "street", 500.0, 520.0, sub_labels=("rex",)),
            [_open_enc(identities={"rex"})],
        ),
        # same_camera
        (
            _atom("a2", "alley-wide", 40.0, 50.0),
            [_open_enc()],
        ),
        # shared_zone
        (
            _atom("a2", "street", 40.0, 50.0, zones=("front-yard",)),
            [_open_enc(zones={"front-yard"})],
        ),
        # adjacent
        (
            _atom("a2", "shed", 40.0, 50.0),
            [_open_enc()],
        ),
        # companion (overlapping spans, no shared family)
        (
            _atom("a2", "shed", 5.0, 25.0, labels=("dog",)),
            [_open_enc(labels={"person", "dog"})],
        ),
        # new (gap too large)
        (
            _atom("a2", "alley-wide", 5000.0, 5010.0),
            [_open_enc()],
        ),
        # new (contradictory identity, hard reject)
        (
            _atom("a2", "alley-wide", 40.0, 50.0, sub_labels=("rex",)),
            [_open_enc(identities={"fido"})],
        ),
    ]


def test_golden_table_matches_with_no_and_default_only_transitions() -> None:
    default_transitions = {
        ("alley-wide", "shed", "animal"): TransitionStats(
            p10=1.0, p50=5.0, p90=20.0, samples=2, source="default"
        ),
        ("alley-wide", "alley-wide", "animal"): TransitionStats(
            p10=1.0, p50=5.0, p90=20.0, samples=2, source="default"
        ),
    }
    cfg_none = CFG
    cfg_default = LinkerConfig(
        gap_s=CFG.gap_s,
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions=default_transitions,
    )
    for atom, encs in _golden_scenarios():
        pinned_to = "e1" if atom.atom_id == "a2" and atom.camera == "faraway" else None
        d_none = decide(atom, encs, ADJ, cfg_none, now=NOW, pinned_to=pinned_to)
        d_default = decide(atom, encs, ADJ, cfg_default, now=NOW, pinned_to=pinned_to)
        assert d_none == d_default, atom.atom_id


def test_learned_row_widens_gap_beyond_flat_allowance() -> None:
    # Flat "animal" gap_s is 90s -- well past a 220s gap. A learned p90 of
    # 100 with slack 1.5 allows 150, which still doesn't cover 220s, so use
    # a smaller gap that the learned allowance covers but the flat one
    # (90s) would reject: 120s.
    enc = _open_enc(last_end=0.0)
    atom = _atom("a2", "shed", 120.0, 130.0)
    cfg = LinkerConfig(
        gap_s={"animal": 90.0, "default": 60.0},
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions={
            ("alley-wide", "shed", "animal"): TransitionStats(
                p10=10.0, p50=50.0, p90=100.0, samples=20, source="learned"
            )
        },
        transition_slack=1.5,
    )
    decision = decide(atom, [enc], ADJ, cfg, now=NOW)
    assert decision.encounter_id == "e1"
    assert decision.reason == "adjacent"
    assert decision.confidence == 0.55


def test_learned_row_narrows_gap_even_though_flat_gap_allows_it() -> None:
    # gap_s allows a 60s gap, but the learned p90 (20) * slack (1.5) = 30
    # does not -- the learned row must win, rejecting the match entirely
    # (no falling back to the flat allowance).
    enc = _open_enc(last_end=0.0)
    atom = _atom("a2", "shed", 60.0, 70.0)
    cfg = LinkerConfig(
        gap_s={"animal": 90.0, "default": 60.0},
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions={
            ("alley-wide", "shed", "animal"): TransitionStats(
                p10=2.0, p50=10.0, p90=20.0, samples=20, source="learned"
            )
        },
        transition_slack=1.5,
    )
    decision = decide(atom, [enc], ADJ, cfg, now=NOW)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_learned_row_gap_inside_p10_p90_gets_higher_confidence() -> None:
    enc = _open_enc(last_end=0.0)
    atom = _atom("a2", "shed", 50.0, 60.0)
    cfg = LinkerConfig(
        gap_s={"animal": 90.0, "default": 60.0},
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions={
            ("alley-wide", "shed", "animal"): TransitionStats(
                p10=10.0, p50=50.0, p90=100.0, samples=20, source="learned"
            )
        },
        transition_slack=1.5,
    )
    decision = decide(atom, [enc], ADJ, cfg, now=NOW)
    assert decision.reason == "adjacent"
    assert decision.confidence == 0.65


def test_same_camera_still_uses_flat_allowance_when_learned_row_exists() -> None:
    # A learned row exists for this (camera, camera, family) key with a
    # tight p90 -- same_camera must still use the flat gap_s allowance, not
    # the learned one, since same_camera isn't a handoff.
    enc = _open_enc(last_end=0.0)
    atom = _atom("a2", "alley-wide", 80.0, 90.0)
    cfg = LinkerConfig(
        gap_s={"animal": 90.0, "default": 60.0},
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions={
            ("alley-wide", "alley-wide", "animal"): TransitionStats(
                p10=1.0, p50=2.0, p90=5.0, samples=20, source="learned"
            )
        },
        transition_slack=1.5,
    )
    decision = decide(atom, [enc], ADJ, cfg, now=NOW)
    assert decision.reason == "same_camera"
    assert decision.confidence == 0.9


def test_learned_row_identity_match_still_multiplies_by_three() -> None:
    # Flat "animal" gap_s (10s) x3 for identity_match = 30 -- a 70s gap
    # fails that, so the flat identity path doesn't fire and falls through
    # to the learned "adjacent" path (which never assigns reason
    # "identity" -- that's only decided in the flat-gate block above it).
    # There, the learned p90 (20) x slack (1.5) x3 (identity_match, same
    # multiplier as the flat path) = 90, which does cover the 70s gap --
    # without that x3 (20*1.5=30) it would not.
    enc = _open_enc(last_end=0.0, identities={"rex"})
    atom = _atom("a2", "shed", 70.0, 80.0, sub_labels=("rex",))
    cfg = LinkerConfig(
        gap_s={"animal": 10.0, "default": 60.0},
        max_duration_s=CFG.max_duration_s,
        recent_cameras=CFG.recent_cameras,
        min_copresence_s=CFG.min_copresence_s,
        transitions={
            ("alley-wide", "shed", "animal"): TransitionStats(
                p10=2.0, p50=10.0, p90=20.0, samples=20, source="learned"
            )
        },
        transition_slack=1.5,
    )
    decision = decide(atom, [enc], ADJ, cfg, now=NOW)
    assert decision.encounter_id == "e1"
    assert decision.reason == "adjacent"
    assert decision.confidence == 0.55
