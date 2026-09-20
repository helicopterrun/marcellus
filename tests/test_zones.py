"""Tests for zone geometry (zones.py).

`event.data.box` is Frigate's normalized **[x, y, w, h]** (what
`to_relative_box` writes) -- the same reading `analysis/zone_hits.py` has
always used. Treating it as [x1, y1, x2, y2] put the zone test point somewhere
unrelated to the object.
"""

from __future__ import annotations

from pathlib import Path

from marcellus.zones import (
    configured_camera_names,
    load_camera_zones,
    zones_containing_box,
)

# A square covering the bottom-left quadrant.
BOTTOM_LEFT = {
    "name": "bottom_left",
    "coords": [(0.0, 0.5), (0.5, 0.5), (0.5, 1.0), (0.0, 1.0)],
}
# A square covering the top-right quadrant.
TOP_RIGHT = {
    "name": "top_right",
    "coords": [(0.5, 0.0), (1.0, 0.0), (1.0, 0.5), (0.5, 0.5)],
}
ZONES = [BOTTOM_LEFT, TOP_RIGHT]


def test_bottom_center_of_an_xywh_box_selects_the_zone_it_stands_in() -> None:
    # x=0.1, y=0.6, w=0.2, h=0.3 -> bottom-center (0.2, 0.9): bottom-left zone.
    assert zones_containing_box(ZONES, [0.1, 0.6, 0.2, 0.3]) == {"bottom_left"}


def test_object_in_the_other_quadrant_selects_the_other_zone() -> None:
    # x=0.6, y=0.1, w=0.2, h=0.2 -> bottom-center (0.7, 0.3): top-right zone.
    assert zones_containing_box(ZONES, [0.6, 0.1, 0.2, 0.2]) == {"top_right"}


def test_no_zone_when_the_feet_are_outside_every_polygon() -> None:
    # bottom-center (0.7, 0.9): bottom-right quadrant, which has no zone.
    assert zones_containing_box(ZONES, [0.6, 0.6, 0.2, 0.3]) == set()


def test_missing_or_malformed_box_is_not_an_error() -> None:
    assert zones_containing_box(ZONES, None) == set()
    assert zones_containing_box(ZONES, [0.1, 0.2]) == set()


def test_malformed_config_yaml_yields_no_zones_instead_of_raising(tmp_path: Path) -> None:
    """A broken Frigate config must not 500 the triage detail page."""
    bad = tmp_path / "config.yml"
    bad.write_text("cameras: {\n  unclosed: [1, 2\n")
    assert load_camera_zones(bad) == {}


def test_non_mapping_config_yields_no_zones(tmp_path: Path) -> None:
    scalar = tmp_path / "config.yml"
    scalar.write_text("just-a-string\n")
    assert load_camera_zones(scalar) == {}


def test_missing_config_yields_no_zones(tmp_path: Path) -> None:
    assert load_camera_zones(tmp_path / "nope.yml") == {}


def test_zones_are_parsed_from_a_real_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "cameras:\n"
        "  doorbell:\n"
        "    zones:\n"
        "      porch:\n"
        "        coordinates: 0,0.5,0.5,0.5,0.5,1,0,1\n"
    )
    zones = load_camera_zones(cfg)
    assert list(zones) == ["doorbell"]
    assert zones["doorbell"][0]["name"] == "porch"
    assert len(zones["doorbell"][0]["coords"]) == 4


def test_configured_camera_names_from_a_real_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yml"
    cfg.write_text("cameras:\n  doorbell: {}\n  gate-face:\n    zones: {}\n")
    assert configured_camera_names(cfg) == {"doorbell", "gate-face"}


def test_configured_camera_names_unreadable_or_empty_means_no_filter(tmp_path: Path) -> None:
    # Missing file, unparseable YAML, and an empty camera map all mean
    # "can't tell" -- callers must not filter on those.
    assert configured_camera_names(tmp_path / "nope.yml") is None
    bad = tmp_path / "bad.yml"
    bad.write_text("cameras: [unclosed\n")
    assert configured_camera_names(bad) is None
    empty = tmp_path / "empty.yml"
    empty.write_text("cameras: {}\n")
    assert configured_camera_names(empty) is None
