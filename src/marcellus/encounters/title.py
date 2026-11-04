"""Human titles for encounters ("who + route"). Pure: plain data in, a string
out -- no DB, no network, no config -- so the whole rule set is unit-testable
and the routes compute a title at read time (nothing is stored).

    Person and dog · Sidewalk → Front Garden → Front Door
    Amazon near Front Door
    Person · Gate Face camera
    Activity

**Who** is the encounter's recognised identities (names, then delivery
brands) followed by the subjects from its labels, in a fixed order -- people,
animals, vehicles, packages, then anything else (bins, doors) -- joined "A",
"A and B", "A, B and C". An identity only *replaces* the subject it names: a
personal name replaces "person"; a delivery brand replaces "person" and the
motor vehicles (Frigate hangs the brand sub-label on the van or its driver).
Every other subject stays, so "Chris" walking a dog reads "Chris and dog".

**Route** is the encounter's members in time order, each reduced to one place
(its first zone's display name, else the camera) with consecutive duplicates
collapsed. One place reads like a push title ("{Who} near {Zone}" /
"{Who} · {Camera} camera"); two to four are listed with arrows; more than four
keep the first two, an ellipsis and the last.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass

from marcellus.encounters.linker import KNOWN_LABELS, LABEL_FAMILIES, normalise_labels
from marcellus.encounters.visits import (
    MemberStop,
    humanise,
    member_zone,
    order_members,
    zone_label,
)
from marcellus.push.live_activities import BIN_LABELS, OPENING_LABELS

#: Delivery brands, as Frigate classification sub_labels (lower-case) -> how
#: the brand is written.
BRANDS: dict[str, str] = {
    "amazon": "Amazon",
    "ups": "UPS",
    "fedex": "FedEx",
    "usps": "USPS",
    "dhl": "DHL",
}

#: Strings that show up in an encounter's `identities` without being one.
#: `identities` is every member `sub_labels` entry, and the linker also folds
#: label *qualifiers* in there (`linker.normalise_labels`): Frigate lists a
#: recognised object as `person-verified` in `objects`, which leaves the
#: literal string "verified" behind as a "sub_label"; "unknown" is Frigate's
#: placeholder for a face it could not match. Anything else that merely
#: repeats one of the encounter's own labels/qualifiers is skipped in `who`.
NON_IDENTITY_SUB_LABELS = frozenset({"verified", "unknown"})

#: Labels a delivery brand replaces ("Amazon", not "Amazon and car"). Bicycles
#: are deliberately absent: a brand does not ride one.
_BRAND_REPLACES_LABELS = frozenset({"car", "truck", "bus", "motorcycle"})

#: Subject order: people, animals, vehicles, packages, everything else.
_RANK_PERSON, _RANK_ANIMAL, _RANK_VEHICLE, _RANK_PACKAGE, _RANK_OTHER = range(5)

_FAMILY_RANK = {
    "person": _RANK_PERSON,
    "animal": _RANK_ANIMAL,
    "vehicle": _RANK_VEHICLE,
    "package": _RANK_PACKAGE,
}
_LABEL_RANK: dict[str, int] = {
    label: _FAMILY_RANK[family] for family, labels in LABEL_FAMILIES.items() for label in labels
}
#: Source adapters (sightings) classify with the family words themselves.
_LABEL_RANK.setdefault("animal", _RANK_ANIMAL)
_LABEL_RANK.setdefault("vehicle", _RANK_VEHICLE)
for _label in BIN_LABELS | OPENING_LABELS:
    _LABEL_RANK.setdefault(_label, _RANK_OTHER)

_ELLIPSIS = "…"
_ARROW = " → "
_DOT = " · "
_MAX_LISTED_PLACES = 4


@dataclass(frozen=True)
class Place:
    """One stop on the route: a zone's display name, or (when the member had
    no zone) a prettified camera name."""

    name: str
    is_zone: bool


def pretty_camera(camera: str) -> str:
    """"gate-face" / "gate_face" -> "Gate Face" (same as the push/doorbell copy)."""
    return humanise(camera, "_-")


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _identity_words(
    identities: Iterable[str], skip: frozenset[str]
) -> tuple[list[str], list[str]]:
    """(names, brands). Names are as stored with the first letter capitalised;
    brands are written canonically. Junk (`NON_IDENTITY_SUB_LABELS`, anything
    in `skip`) and duplicates are dropped."""
    names: list[str] = []
    brands: list[str] = []
    seen: set[str] = set()
    for raw in identities:
        text = (raw or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        brand = BRANDS.get(key)
        if brand is None and (key in NON_IDENTITY_SUB_LABELS or key in skip):
            continue
        seen.add(key)
        if brand is not None:
            brands.append(brand)
        else:
            names.append(_upper_first(text))
    return names, brands


def who(
    labels: Iterable[str],
    identities: Iterable[str],
    known_labels: Collection[str] = KNOWN_LABELS,
) -> str:
    """"Chris and dog", "Person and dog", "Amazon and package", ... or
    "Activity" when nothing is recognisable. See the module docstring."""
    base, qualifiers = normalise_labels(list(labels), known_labels)
    own = frozenset(x.lower() for x in (*base, *qualifiers))
    names, brands = _identity_words([*identities, *qualifiers], own)

    subjects: list[tuple[int, str]] = []
    for label in dict.fromkeys(base):
        rank = _LABEL_RANK.get(label, _RANK_OTHER)
        if (names or brands) and rank == _RANK_PERSON:
            continue
        if brands and label in _BRAND_REPLACES_LABELS:
            continue
        subjects.append((rank, label.replace("_", " ").lower()))
    subjects.sort(key=lambda s: s[0])  # stable: input order within a rank

    words = [*names, *brands, *(w for _, w in subjects)]
    if not words:
        return "Activity"
    return _upper_first(_join(words))


def route_places(
    stops: Iterable[MemberStop], zone_display: Callable[[str], str | None]
) -> list[Place]:
    """Members in start-time order -> places, consecutive duplicates collapsed.

    `zone_display` maps a raw zone key to its configured display name (or
    None); an unconfigured zone is humanised the way push copy does
    ("front_door" -> "Front Door").
    """
    places: list[Place] = []
    for stop in order_members(stops):
        zone = member_zone(stop)
        if zone:
            place = Place(zone_label(zone, zone_display), True)
        elif stop.camera:
            place = Place(pretty_camera(stop.camera), False)
        else:
            continue
        if places and places[-1].name == place.name:
            continue
        places.append(place)
    return places


def encounter_title(
    labels: Iterable[str], identities: Iterable[str], places: Sequence[Place]
) -> str:
    """The full "who + route" title; see the module docstring."""
    subject = who(labels, identities)
    if not places:
        return subject
    if len(places) == 1:
        only = places[0]
        if only.is_zone:
            return f"{subject} near {only.name}"
        return f"{subject}{_DOT}{only.name} camera"
    names = [p.name for p in places]
    if len(names) > _MAX_LISTED_PLACES:
        names = [*names[:2], _ELLIPSIS, names[-1]]
    return f"{subject}{_DOT}{_ARROW.join(names)}"
