"""SpeciesNet class strings -> a verdict and a Frigate-style label suggestion.

SpeciesNet's ~2500 classes are taxonomy strings,
``uuid;class;order;family;genus;species;common name``, with a few special
non-taxa (``;;;;;;blank``, ``;;;;;;vehicle``) and coarse fallbacks
(``;mammalia;;;;;mammal``, ``;aves;;;;;bird``).

Two decisions are made from one crop's top-k probabilities, and both sum
probability mass over *groups* rather than trusting the single top class:

- **Verdict.** "blank" mass decides whether the box is scenery at all, and is
  the finder's false-positive filter. Human and vehicle mass is rejected the
  same way, since Frigate already tracks those.
- **Suggestion.** The animal mass is rolled up to the labels Frigate+ trains
  (squirrel, rodent, raccoon, possum, ...) by family or order. This matters on
  IR footage: SpeciesNet's species-level calls are unreliable there (it called
  a rabbit a "gambian rat" in the benchmark), and a global model spreads
  probability over many look-alike species from other continents. Summing by
  family turns "0.3 eastern gray squirrel + 0.2 collie's squirrel + ..." into a
  confident "squirrel" without any geofencing.

Pure Python; no model or numpy imports.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# (class, order, family, genus, species) prefix -> suggestion. First match wins,
# so more specific rows go first. Empty strings in a rule are wildcards.
_RULES: tuple[tuple[tuple[str, str, str, str, str], str], ...] = (
    (("mammalia", "", "canidae", "canis", "familiaris"), "dog"),
    (("mammalia", "", "canidae", "vulpes", ""), "fox"),
    (("mammalia", "", "canidae", "urocyon", ""), "fox"),
    (("mammalia", "", "canidae", "canis", "latrans"), "coyote"),
    (("mammalia", "", "canidae", "", ""), "canid"),
    (("mammalia", "rodentia", "sciuridae", "", ""), "squirrel"),
    (("mammalia", "rodentia", "", "", ""), "rodent"),
    (("mammalia", "", "procyonidae", "", ""), "raccoon"),
    (("mammalia", "didelphimorphia", "", "", ""), "possum"),
    (("mammalia", "", "felidae", "", ""), "cat"),
    (("mammalia", "lagomorpha", "", "", ""), "rabbit"),
    (("mammalia", "", "mephitidae", "", ""), "skunk"),
    (("mammalia", "", "cervidae", "", ""), "deer"),
    (("mammalia", "", "mustelidae", "", ""), "weasel"),
    (("mammalia", "", "ursidae", "", ""), "bear"),
    (("aves", "", "", "", ""), "bird"),
    (("reptilia", "", "", "", ""), "reptile"),
    (("amphibia", "", "", "", ""), "amphibian"),
)


@dataclass(frozen=True)
class Taxon:
    klass: str
    order: str
    family: str
    genus: str
    species: str
    common: str


def parse(label: str) -> Taxon:
    """Split a SpeciesNet class string. Tolerates a missing uuid field."""
    parts = label.split(";")
    if len(parts) == 6:
        parts = ["", *parts]
    parts = (parts + [""] * 7)[:7]
    return Taxon(*(p.strip().lower() for p in parts[1:7]))


def is_blank(t: Taxon) -> bool:
    return t.common == "blank" and not t.klass


def is_vehicle(t: Taxon) -> bool:
    return t.common == "vehicle" and not t.klass


def is_human(t: Taxon) -> bool:
    return t.genus == "homo" or t.common == "human"


def suggestion_for(t: Taxon) -> str:
    """The Frigate-style label for one animal taxon; "animal" if nothing fits."""
    have = (t.klass, t.order, t.family, t.genus, t.species)
    for rule, label in _RULES:
        if all(r == "" or r == h for r, h in zip(rule, have, strict=True)):
            return label
    return "animal"


def display_name(t: Taxon) -> str:
    return t.common or t.species or t.genus or t.family or t.order or t.klass or "?"


@dataclass(frozen=True)
class Verdict:
    """What one classified crop is.

    `verdict` is "animal", "blank", "human" or "vehicle". `reject_score` is the
    largest of the blank/human/vehicle masses -- how sure the classifier is
    that this is NOT an animal -- kept for every box so the threshold can be
    re-tuned from stored data. `suggestion` is the rolled-up animal label and
    `suggestion_score` its summed probability.
    """

    verdict: str
    reject_score: float
    suggestion: str
    suggestion_score: float
    top_class: str
    top_score: float


def decide(top: Sequence[tuple[str, float]], *, reject_score: float) -> Verdict:
    """Verdict + suggestion from one crop's top-k `(class string, probability)`."""
    blank = human = vehicle = 0.0
    animal: dict[str, float] = {}
    best_name, best_p = "?", 0.0
    for label, p in top:
        t = parse(label)
        if p > best_p:
            best_name, best_p = display_name(t), float(p)
        if is_blank(t):
            blank += p
        elif is_human(t):
            human += p
        elif is_vehicle(t):
            vehicle += p
        else:
            key = suggestion_for(t)
            animal[key] = animal.get(key, 0.0) + p

    rejects = {"blank": blank, "human": human, "vehicle": vehicle}
    worst = max(rejects, key=lambda k: rejects[k])
    sug, sug_p = ("animal", 0.0)
    if animal:
        sug = max(animal, key=lambda k: animal[k])
        sug_p = animal[sug]
    verdict = worst if rejects[worst] >= reject_score else "animal"
    return Verdict(
        verdict=verdict,
        reject_score=round(rejects[worst], 4),
        suggestion=sug,
        suggestion_score=round(sug_p, 4),
        top_class=best_name,
        top_score=round(best_p, 4),
    )
