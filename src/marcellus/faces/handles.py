"""Generated handles for face IDs: a stable, friendly label like "Alder Wren".

A handle is derived from the cluster id alone (no schema, no storage). It is
NOT a name: it never changes `familiarity`, is never written to Frigate, and
never appears in sub_labels, encounters or pushes. Clients show a person's
name, then their handle, then `#id`.

FROZEN. The two word tuples and `_MULTIPLIER` define every handle ever shown
to a user. Appending a word is NOT safe: it changes N, which changes the
mapping for every existing id. To add words, build a new scheme under a new
function, do not edit these. `tests/test_people_handles.py` pins a sha256 of
the lists and the multiplier, plus specific mappings, so an edit fails loudly.

Mapping: `index = (id * M) % N` with N = len(first) * len(second) and M prime
and larger than N (so coprime to it): that is a bijection on `id % N`, which
scatters neighbouring ids across the pair space. `cycle = id // N` extends
past the pair space: cycle 0 has no suffix, cycle k >= 1 appends `k + 1`.
(id % N, id // N) determines id, so the mapping is injective for all ids >= 0.

Word rules: one capitalized ASCII word each, no duplicates, neutral and
pleasant. No colours or words describing a person's appearance, age or
character; no slang, insults or innuendo; nothing that reads as a common first
name (so a handle is never mistaken for a real name).
"""

from __future__ import annotations

_FIRST: tuple[str, ...] = (
    "Alder", "Almond", "Anemone", "Apricot", "Aster", "Bamboo", "Beech", "Birch",
    "Bracken", "Bramble", "Briar", "Canyon", "Cascade", "Cavern", "Cedar", "Copse",
    "Cove", "Crag", "Creek", "Cypress", "Delta", "Dune", "Elm", "Fennel",
    "Fig", "Fir", "Fjord", "Foxglove", "Gentian", "Glade", "Gorge", "Gorse",
    "Grotto", "Grove", "Guava", "Hickory", "Hillock", "Hollow", "Hornbeam", "Isle",
    "Knoll", "Lagoon", "Larch", "Lotus", "Lupine", "Maple", "Marigold", "Marsh",
    "Meadow", "Mere", "Mesa", "Mint", "Moor", "Moss", "Oak", "Orchard",
    "Orchid", "Papaya", "Pasture", "Peak", "Pear", "Pecan", "Pine", "Plateau",
    "Prairie", "Primrose", "Quince", "Rapids", "Ravine", "Reef", "Ridge", "Savanna",
    "Sedge", "Sequoia", "Shoal", "Shore", "Spruce", "Steppe", "Strand", "Summit",
    "Sumac", "Sycamore", "Taiga", "Tamarack", "Tarn", "Teak", "Thicket", "Thistle",
    "Thyme", "Tundra", "Valley", "Walnut", "Wisteria", "Yarrow", "Yew", "Fen",
    "Lychee", "Pomelo",
)

_SECOND: tuple[str, ...] = (
    "Albatross", "Avocet", "Bittern", "Bobolink", "Bunting", "Canary", "Cardinal", "Catbird",
    "Chickadee", "Chiffchaff", "Condor", "Cormorant", "Crane", "Crossbill", "Curlew", "Dipper",
    "Dunlin", "Eagle", "Egret", "Eider", "Emu", "Falcon", "Fantail", "Fieldfare",
    "Finch", "Flamingo", "Flicker", "Flycatcher", "Fulmar", "Gadwall", "Gannet", "Godwit",
    "Grebe", "Grosbeak", "Grouse", "Harrier", "Hawk", "Heron", "Hoopoe", "Hornbill",
    "Hummingbird", "Ibis", "Kestrel", "Killdeer", "Kingfisher", "Kinglet", "Kite", "Kittiwake",
    "Kookaburra", "Lapwing", "Lark", "Linnet", "Lyrebird", "Macaw", "Magpie", "Mallard",
    "Merganser", "Mockingbird", "Nightjar", "Oriole", "Osprey", "Owl", "Oystercatcher", "Partridge",
    "Pelican", "Petrel", "Pheasant", "Phalarope", "Pipit", "Plover", "Puffin", "Quail",
    "Rail", "Roadrunner", "Sanderling", "Sandpiper", "Scaup", "Shearwater", "Shrike", "Siskin",
    "Skylark", "Snipe", "Sparrow", "Spoonbill", "Starling", "Stilt", "Stork", "Sunbird",
    "Swallow", "Swan", "Swift", "Tanager", "Tern", "Thrasher", "Thrush", "Toucan",
    "Towhee", "Trogon", "Turnstone", "Veery", "Vireo", "Wagtail", "Warbler", "Waxwing",
    "Weaver", "Wheatear", "Whimbrel", "Wigeon", "Woodlark", "Woodpecker", "Wren", "Wryneck",
)

#: Prime larger than N, so coprime to it. Chosen so M % N is near 0.618 * N and
#: sequential ids step ~60 words in the first list and 23 in the second: neighbours
#: never share a word.
_MULTIPLIER = 2654431607

_N = len(_FIRST) * len(_SECOND)


def handle_for(person_id: int) -> str:
    """The handle for `person_id` (>= 0): stable forever, unique per id."""
    if person_id < 0:
        raise ValueError("person id must be non-negative")
    cycle, rest = divmod(person_id, _N)
    index = (rest * _MULTIPLIER) % _N
    first, second = divmod(index, len(_SECOND))
    text = f"{_FIRST[first]} {_SECOND[second]}"
    return text if cycle == 0 else f"{text} {cycle + 1}"
