"""Generated handles ("Alder Wren"): derivation, injectivity, and the freeze."""

from __future__ import annotations

import hashlib

import pytest

from marcellus.faces import handles
from marcellus.faces.handles import handle_for

# Sha256 over the two word tuples and the multiplier. If this fails, someone
# edited a frozen list: every handle already shown to a user would change.
_FROZEN_SHA256 = "22334b5becb86e78cec93cdff36bcb95af91b381ba000a082f4c5dbb7d6ff5d5"

# Words that must never appear (colours, ages, name-like, insulting or jokey).
_BANNED = {
    "Black", "White", "Brown", "Red", "Old", "Young", "Wild", "Fair", "Dark", "Blue", "Gray",
    "Grey", "Green", "Gold", "Tit", "Booby", "Shag", "Cock", "Coot", "Loon", "Cuckoo",
    "Turkey", "Vulture", "Buzzard", "Crow", "Raven", "Hooter", "Dodo", "Goose", "Gull",
    "Pigeon", "Parrot", "Nuthatch", "Bustard", "Weed", "Poppy", "Hemp", "Pansy", "Nettle",
    "Thorn", "Poison", "Heather", "Holly", "Hazel", "Ivy", "Laurel", "Rose", "Lily", "Daisy",
    "Jay", "Robin", "Martin", "Phoebe", "Willow", "Juniper", "Rowan", "Reed", "Glen", "Brook",
}


def test_frozen_lists_and_multiplier() -> None:
    digest = hashlib.sha256(
        (
            "|".join(handles._FIRST)
            + "#"
            + "|".join(handles._SECOND)
            + "#"
            + str(handles._MULTIPLIER)
        ).encode()
    ).hexdigest()
    assert digest == _FROZEN_SHA256


def test_pinned_mappings() -> None:
    n = handles._N
    assert handle_for(11) == "Shoal Gadwall"
    assert handle_for(651) == "Walnut Shearwater"
    assert handle_for(0) == "Alder Albatross"
    assert handle_for(1) == "Pear Fieldfare"
    assert handle_for(n - 1) == "Hollow Swan"
    assert handle_for(n) == "Alder Albatross 2"
    assert handle_for(n + 1) == "Pear Fieldfare 2"
    assert handle_for(2 * n) == "Alder Albatross 3"


def test_word_rules() -> None:
    words = list(handles._FIRST) + list(handles._SECOND)
    assert len(words) == len(set(words)), "duplicate word"
    for w in words:
        assert w.isascii() and w.isalpha() and w[0].isupper() and w[1:].islower(), w
    assert not set(words) & _BANNED
    assert 90 <= len(handles._FIRST) <= 130 and 90 <= len(handles._SECOND) <= 130


def test_multiplier_coprime_to_pair_space() -> None:
    from math import gcd

    assert gcd(handles._MULTIPLIER, handles._N) == 1


def test_injective_across_cycle_boundaries() -> None:
    n = handles._N
    seen: dict[str, int] = {}
    for i in range(3 * n + 5):
        h = handle_for(i)
        assert h not in seen, f"{i} collides with {seen[h]}"
        seen[h] = i
    # Suffix scheme: none, then 2, 3, ...
    assert not handle_for(n - 1)[-1].isdigit()
    assert handle_for(n).endswith(" 2") and handle_for(2 * n).endswith(" 3")
    assert handle_for(3 * n).endswith(" 4")


def test_neighbouring_ids_do_not_look_alike() -> None:
    for i in range(1, 1001):
        a, b = handle_for(i).split(), handle_for(i + 1).split()
        assert a[0] != b[0] and a[1] != b[1]


def test_negative_id_rejected() -> None:
    with pytest.raises(ValueError):
        handle_for(-1)
