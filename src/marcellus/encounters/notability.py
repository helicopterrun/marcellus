"""Encounter notability: is this visit a passer-by ("background") or worth a
second look ("notable")?

Pure functions only -- no I/O, no DB, no clock reads. `store.recompute`
calls a *stamper* (see `stamper_for`) every time an encounter's members
change and writes the result into `encounters.tag/place/outcome/tag_reason`,
so the answer is fixed when the visit is recorded and a later settings change
never rewrites history. Nothing recomputes it at read time.

Rules, first match wins (docs/guide `encounters.md` has the user-facing
version):

1. two or more distinct cameras            -> notable, ``multi_camera``
2. reached a place other than the street   -> notable, ``left_public``
3. alert ladder says more than "log"       -> notable, ``alerted``
4. anyone/anything was recognised          -> notable, ``recognised``
5. an animal was seen (dog-walker aside)   -> notable, ``animal``
6. stayed at least `linger_s`              -> notable, ``lingered``
7. it happened at night                    -> notable, ``night``
8. otherwise                               -> background, ``passer_by``
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from marcellus.push import delivery_wire, ladder, ladder_policy, policy_settings

if TYPE_CHECKING:
    from marcellus.config import Settings

TAG_BACKGROUND = "background"
TAG_NOTABLE = "notable"

#: Fallback "night" window (server-local clock) when no latitude/longitude is
#: configured: from NIGHT_START_HOUR (inclusive) to NIGHT_END_HOUR (exclusive).
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6

#: Sun centre below this altitude (degrees) counts as night -- the standard
#: sunrise/sunset definition (refraction + solar radius).
_NIGHT_ELEVATION_DEG = -0.833

#: Outcomes that do not, on their own, make an encounter notable.
_QUIET_OUTCOMES = ("off", "log")

#: Engine levels loudest-last, with `off` (suppressed) below them all.
_LEVEL_ORDER = ("off", *ladder_policy.LEVELS)

_DOG = "dog"
_PERSON = "person"

#: Labels that make an encounter "animal": what delivery_wire treats as an
#: animal plus the dangerous ones the ladder reclassifies.
_ANIMAL_LABELS = frozenset(delivery_wire._ANIMAL_LABELS) | frozenset(
    ladder_policy.DANGEROUS_ANIMAL_LABELS
)


@dataclass(frozen=True)
class Stamp:
    tag: str  # "background" | "notable"
    place: str  # one of policy_settings.PLACES
    outcome: str  # one of policy_settings.OUTCOMES
    reason: str  # see module docstring


#: What `store.recompute` calls with an encounter's member dicts.
Stamper = Callable[[list[dict[str, Any]]], Stamp]


# --------------------------------------------------------------------------
# Sun
# --------------------------------------------------------------------------


def solar_elevation_deg(ts: float, latitude: float, longitude: float) -> float:
    """Elevation of the sun's centre above the horizon, in degrees, at UTC
    epoch `ts` for a point on the ground (north-positive latitude,
    east-positive longitude). NOAA's general solar position approximation
    (accurate to a fraction of a degree), no refraction correction."""
    jd = ts / 86400.0 + 2440587.5
    t = (jd - 2451545.0) / 36525.0

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anom)
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = mean_long + centre
    omega = math.radians(125.04 - 1934.136 * t)
    app_long = math.radians(true_long - 0.00569 - 0.00478 * math.sin(omega))

    arcsec = 21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))
    mean_obliq = 23.0 + (26.0 + arcsec / 60.0) / 60.0
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(app_long))

    var_y = math.tan(obliq / 2.0) ** 2
    l0 = math.radians(mean_long)
    eq_time_min = 4.0 * math.degrees(
        var_y * math.sin(2 * l0)
        - 2.0 * ecc * math.sin(m)
        + 4.0 * ecc * var_y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * var_y * var_y * math.sin(4 * l0)
        - 1.25 * ecc * ecc * math.sin(2 * m)
    )

    utc_min = (ts % 86400.0) / 60.0
    solar_min = (utc_min + eq_time_min + 4.0 * longitude) % 1440.0
    hour_angle = math.radians(solar_min / 4.0 - 180.0)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(
        hour_angle
    )
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    return 90.0 - math.degrees(math.acos(cos_zenith))


def is_night(ts: float, latitude: float | None, longitude: float | None) -> bool:
    """True when the sun is down at `ts`. Without a configured location,
    falls back to the server's local clock: 22:00-06:00."""
    if latitude is None or longitude is None:
        hour = datetime.fromtimestamp(ts).hour
        return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR
    return solar_elevation_deg(ts, latitude, longitude) < _NIGHT_ELEVATION_DEG


# --------------------------------------------------------------------------
# Stamp
# --------------------------------------------------------------------------


def _strs(member: Mapping[str, Any], key: str) -> list[str]:
    """A member's JSON list column, whether already decoded (`labels`) or
    raw (`labels_json`)."""
    value = member.get(key)
    if value is None:
        value = member.get(f"{key}_json")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x) for x in value]


def _loudest(outcomes: Sequence[str]) -> str:
    """Loudest of several members' outcome words, by the v3 engine order
    (`notify` -- Quiet -- sits below `glance` now)."""
    order = ("off", "log", "notify", "glance", "alarm")
    return max(outcomes, key=order.index, default="off")


def _member_outcome(
    labels: Sequence[str], zones: Sequence[str], zone_classes: dict[str, str]
) -> str:
    """The alert ladder's answer for one member: the loudest outcome across
    the ladder subjects present in its labels, every modifier at default."""
    subjects: dict[str, str] = {}  # subject -> a label that maps to it
    for label in labels:
        subjects.setdefault(delivery_wire.subject_for_labels([label]), label)
    levels: list[str] = []
    for subject, label in subjects.items():
        zone, place = delivery_wire.most_severe_zone(
            zones, subject=subject, zone_classes=zone_classes
        )
        level = ladder.evaluate_ladder(
            ladder.Snapshot(subject=subject, place=place, zone=zone, label=label)
        )
        levels.append("off" if level == ladder.SUPPRESSED else level)
    # Loudest by the v3 engine order, then named in the outcome vocabulary
    # the encounters wire has always carried (ladder v3 §5.6:
    # `LEVEL_TO_OUTCOME`, where Quiet -- `silent` -- reads as "notify").
    loudest = max(levels, key=_LEVEL_ORDER.index, default="off")
    return "off" if loudest == "off" else policy_settings.LEVEL_TO_OUTCOME[loudest]


def stamp(
    members: Sequence[Mapping[str, Any]],
    *,
    zone_classes: Mapping[str, str],
    linger_s: float,
    latitude: float | None,
    longitude: float | None,
) -> Stamp:
    """Classify an encounter from its members (dicts carrying `camera`,
    `start_time`, `end_time`, and `labels`/`zones`/`sub_labels` as lists or
    as the raw `*_json` strings)."""
    if not members:
        return Stamp(TAG_NOTABLE, "street", "off", "empty")

    classes = dict(zone_classes)
    all_labels: set[str] = set()
    for m in members:
        all_labels.update(_strs(m, "labels"))
    # Dog-walker: a person with a dog is one passer-by, not an animal visit.
    drop_dog = _PERSON in all_labels and _DOG in all_labels

    place_order = policy_settings.PLACES
    place_idx = 0
    outcomes: list[str] = []
    animal = False
    recognised = False
    for m in members:
        zones = [z for z in _strs(m, "zones") if z]
        labels = [lb for lb in _strs(m, "labels") if not (drop_dog and lb == _DOG)]
        if zones:
            for z in zones:
                place_idx = max(place_idx, place_order.index(delivery_wire.zone_place(z, classes)))
        # A member with no zones counts as street (index 0): nothing to raise.
        outcomes.append(_member_outcome(labels, zones, classes))
        animal = animal or any(lb in _ANIMAL_LABELS for lb in labels)
        recognised = recognised or any(s.strip() for s in _strs(m, "sub_labels"))

    place = place_order[place_idx]
    outcome = _loudest(outcomes)

    cameras = {m.get("camera") for m in members}
    if len(cameras) >= 2:
        return Stamp(TAG_NOTABLE, place, outcome, "multi_camera")
    if place != "street":
        return Stamp(TAG_NOTABLE, place, outcome, "left_public")
    if outcome not in _QUIET_OUTCOMES:
        return Stamp(TAG_NOTABLE, place, outcome, "alerted")
    if recognised:
        return Stamp(TAG_NOTABLE, place, outcome, "recognised")
    if animal:
        return Stamp(TAG_NOTABLE, place, outcome, "animal")

    first = min(float(m["start_time"]) for m in members)
    last = max(
        float(m["end_time"]) if m.get("end_time") is not None else float(m["start_time"])
        for m in members
    )
    if last - first >= linger_s:
        return Stamp(TAG_NOTABLE, place, outcome, "lingered")
    if is_night(first, latitude, longitude):
        return Stamp(TAG_NOTABLE, place, outcome, "night")
    return Stamp(TAG_BACKGROUND, place, outcome, "passer_by")


def stamper_for(settings: Settings) -> Stamper:
    """The production stamper: closes over `settings.encounters` and reads
    the live zone -> place assignments at call time, so a settings change
    affects only encounters stamped afterwards."""

    def _stamper(members: list[dict[str, Any]]) -> Stamp:
        enc = settings.encounters
        zone_classes = policy_settings.get_active().get("zone_classes") or {}
        return stamp(
            members,
            zone_classes=zone_classes,
            linger_s=enc.linger_s,
            latitude=enc.latitude,
            longitude=enc.longitude,
        )

    return _stamper
