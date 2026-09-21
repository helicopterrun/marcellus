"""encounters/observations.py: pure direction derivation (docs/encounters.md
"Observations" / M1 spec)."""

from __future__ import annotations

import json

from marcellus.encounters.observations import derive_direction


def _event(
    zones: object = None, box: object = None, path_data: object = None, **extra: object
) -> dict:
    data: dict[str, object] = {}
    if box is not None:
        data["box"] = box
    if path_data is not None:
        data["path_data"] = path_data
    data.update(extra)
    return {
        "id": "ev1",
        "zones": json.dumps(zones) if zones is not None else None,
        "data": json.dumps(data),
    }


def test_two_zones_yields_out_last_zone() -> None:
    d = derive_direction([_event(zones=["front_garden", "shed"])])
    assert d.first_zone == "front_garden"
    assert d.last_zone == "shed"
    assert d.direction == "out:shed"
    assert d.source == "zones"


def test_single_zone_falls_through_to_path_heading() -> None:
    path = [[0.1, 0.5, 0.0], [0.3, 0.5, 1.0], [0.5, 0.5, 2.0], [0.7, 0.5, 3.0]]
    d = derive_direction([_event(zones=["front_garden"], path_data=path)])
    assert d.first_zone == "front_garden"
    assert d.last_zone == "front_garden"
    assert d.source == "path"
    assert d.direction == "l2r"
    assert d.heading_deg is not None


def test_too_few_path_points_falls_to_box() -> None:
    ev1 = _event(box=[0.1, 0.1, 0.2, 0.2], path_data=[[0.1, 0.1, 0.0]])
    ev2 = _event(box=[0.6, 0.1, 0.7, 0.2], path_data=[[0.6, 0.1, 1.0]])
    d = derive_direction([ev1, ev2])
    assert d.source == "box"
    assert d.direction == "l2r"


def test_box_area_growth_prefers_toward() -> None:
    ev1 = _event(box=[0.4, 0.4, 0.5, 0.5])  # small box, area .01
    ev2 = _event(box=[0.35, 0.35, 0.65, 0.65])  # much bigger, area .09, centroid barely moves
    d = derive_direction([ev1, ev2])
    assert d.source == "box"
    assert d.direction == "toward"


def test_nothing_yields_empty_direction() -> None:
    d = derive_direction([])
    assert d.first_zone == ""
    assert d.last_zone == ""
    assert d.direction == ""
    assert d.heading_deg is None
    assert d.source == ""

    d2 = derive_direction([_event()])
    assert d2.direction == ""
    assert d2.source == ""


def test_malformed_json_never_raises() -> None:
    bad_rows = [
        {"id": "e1", "zones": "{not json", "data": "also not json"},
        {"id": "e2", "zones": None, "data": None},
        {},
    ]
    d = derive_direction(bad_rows)
    assert d.direction == ""
    assert d.source == ""
