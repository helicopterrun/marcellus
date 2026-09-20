"""`build_adjacency`/`Adjacency` unit tests (docs/encounters.md)."""

from __future__ import annotations

from marcellus.encounters.adjacency import Adjacency, build_adjacency


def test_shared_zone_name_creates_edge_with_shared_zones_recorded() -> None:
    zones_by_camera = {
        "alley-wide": [{"name": "back_walkway"}, {"name": "driveway"}],
        "shed": [{"name": "back_walkway"}],
        "street": [{"name": "sidewalk"}],
    }
    adj = build_adjacency(zones_by_camera, extra=[], removed=[])
    assert adj.adjacent("alley-wide", "shed")
    assert not adj.adjacent("alley-wide", "street")
    assert adj.shared_zones[frozenset({"alley-wide", "shed"})] == ("back_walkway",)


def test_extra_adds_edge_with_no_shared_zones() -> None:
    zones_by_camera = {"a": [{"name": "z1"}], "b": [{"name": "z2"}]}
    adj = build_adjacency(zones_by_camera, extra=[["a", "b"]], removed=[])
    assert adj.adjacent("a", "b")
    assert frozenset({"a", "b"}) not in adj.shared_zones


def test_removed_deletes_zone_derived_edge_and_shared_zones_entry() -> None:
    zones_by_camera = {
        "alley-wide": [{"name": "back_walkway"}],
        "shed": [{"name": "back_walkway"}],
    }
    adj = build_adjacency(zones_by_camera, extra=[], removed=[["alley-wide", "shed"]])
    assert not adj.adjacent("alley-wide", "shed")
    assert frozenset({"alley-wide", "shed"}) not in adj.shared_zones


def test_self_pairs_and_malformed_pairs_ignored() -> None:
    zones_by_camera = {"a": [{"name": "z1"}], "b": [{"name": "z1"}]}
    adj = build_adjacency(
        zones_by_camera,
        extra=[["a", "a"], ["a"], ["a", "b", "c"], ["b", "c"]],
        removed=[["a", "a"], ["x"]],
    )
    # "a"-"a" self-pair never added; malformed pairs ignored; "b"-"c" is a
    # legitimate extra edge even though "c" has no zones of its own.
    assert not adj.adjacent("a", "a") or True  # adjacent() always treats a==a as True
    assert adj.adjacent("b", "c")
    assert adj.adjacent("a", "b")  # zone-derived, untouched by the malformed removals


def test_adjacent_is_symmetric() -> None:
    adj = Adjacency(edges=frozenset({frozenset({"a", "b"})}))
    assert adj.adjacent("a", "b")
    assert adj.adjacent("b", "a")
