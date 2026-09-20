"""Pure-Python linker tests (docs/encounters.md "Tests" section)."""

from __future__ import annotations

from marcellus.encounters.adjacency import Adjacency
from marcellus.encounters.linker import (
    Atom,
    LinkerConfig,
    OpenEncounter,
    decide,
    fold,
    normalise_labels,
)

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
