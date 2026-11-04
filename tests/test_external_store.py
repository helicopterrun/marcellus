from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from marcellus import db
from marcellus.external import store
from marcellus.external.types import Sighting, SightingBox, SightingStream


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.open_sidecar(tmp_path / "s.db")
    try:
        yield c
    finally:
        c.close()


def mk(key: str = "k1", start: float = 100.0, **kw: object) -> Sighting:
    return Sighting(source_id="src", native_key=key, start=start, **kw)  # type: ignore[arg-type]


STREAM = SightingStream(name="a", role="color", stills=("s0.jpg",), clip="c.mp4")


def test_new_updated_unchanged(conn: sqlite3.Connection) -> None:
    assert store.upsert_sighting(conn, mk(), "poll", 1.0) == "new"
    assert store.upsert_sighting(conn, mk(), "poll", 2.0) == "unchanged"
    assert store.upsert_sighting(conn, mk(subject="animal"), "poll", 3.0) == "updated"
    got = store.get_sighting(conn, "src", "k1")
    assert got is not None and got.subject == "animal"
    assert store.get_sighting(conn, "src", "nope") is None


def test_unchanged_does_not_touch_updated_at(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(), "poll", 1.0)
    store.upsert_sighting(conn, mk(), "poll", 9.0)
    row = conn.execute("SELECT updated_at FROM external_sightings").fetchone()
    assert row["updated_at"] == 1.0


def test_hint_after_poll_ignored(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(subject="person"), "poll", 1.0)
    assert store.upsert_sighting(conn, mk(subject="animal"), "mqtt", 2.0) == "unchanged"
    got = store.get_sighting(conn, "src", "k1")
    assert got is not None and got.subject == "person"
    assert store.get_origin(conn, "src", "k1") == "poll"


def test_poll_after_hint_replaces(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(), "mqtt", 1.0)
    assert store.get_origin(conn, "src", "k1") == "mqtt"
    assert store.upsert_sighting(conn, mk(subject="animal"), "poll", 2.0) == "updated"
    assert store.get_origin(conn, "src", "k1") == "poll"
    # Same content, origin flips only: still an update.
    store.upsert_sighting(conn, mk(key="k2"), "mqtt", 1.0)
    assert store.upsert_sighting(conn, mk(key="k2"), "poll", 2.0) == "updated"
    assert store.get_origin(conn, "src", "k2") == "poll"
    assert store.get_origin(conn, "src", "zzz") is None


def test_mqtt_over_mqtt_stays_mqtt(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(), "mqtt", 1.0)
    store.upsert_sighting(conn, mk(subject="animal"), "mqtt", 2.0)
    assert store.get_origin(conn, "src", "k1") == "mqtt"


def test_final_never_downgrades(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(final=True, status="complete"), "poll", 1.0)
    assert (
        store.upsert_sighting(conn, mk(final=False, subject="animal"), "poll", 2.0) == "unchanged"
    )
    got = store.get_sighting(conn, "src", "k1")
    assert got is not None and got.final and got.status == "complete"


def test_empty_media_keeps_existing(conn: sqlite3.Connection) -> None:
    box = SightingBox(label="cat", conf=0.9, box=(0.1, 0.1, 0.5, 0.5), still="s0.jpg")
    store.upsert_sighting(conn, mk(poster="p.jpg", streams=(STREAM,), boxes=(box,)), "poll", 1.0)
    assert store.upsert_sighting(conn, mk(subject="animal"), "poll", 2.0) == "updated"
    got = store.get_sighting(conn, "src", "k1")
    assert got is not None
    assert got.subject == "animal"
    assert got.poster == "p.jpg"
    assert got.streams == (STREAM,)
    assert got.boxes == (box,)


def test_first_seen_preserved_and_raw_roundtrip(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk(raw={"a": 1}), "poll", 1.0)
    store.upsert_sighting(conn, mk(subject="animal", raw={"a": 2}), "poll", 5.0)
    row = conn.execute("SELECT first_seen, updated_at FROM external_sightings").fetchone()
    assert row["first_seen"] == 1.0 and row["updated_at"] == 5.0
    got = store.get_sighting(conn, "src", "k1")
    assert got is not None and dict(got.raw) == {"a": 2}


def test_list_ordering_and_filters(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk("a", 10.0, subject="animal"), "poll", 1.0)
    store.upsert_sighting(conn, mk("b", 20.0, subject="person"), "poll", 1.0)
    store.upsert_sighting(conn, mk("c", 20.0, subject="animal"), "poll", 1.0)
    store.upsert_sighting(
        conn, Sighting(source_id="other", native_key="z", start=30.0, subject="animal"), "poll", 1.0
    )

    keys = [s.native_key for s in store.list_sightings(conn)]
    assert keys == ["z", "c", "b", "a"]
    assert [s.native_key for s in store.list_sightings(conn, source_id="src")] == ["c", "b", "a"]
    assert [s.native_key for s in store.list_sightings(conn, subject="person")] == ["b"]
    assert [s.native_key for s in store.list_sightings(conn, source_id="src", since=20.0)] == [
        "c",
        "b",
    ]
    assert [s.native_key for s in store.list_sightings(conn, source_id="src", limit=1)] == ["c"]


def test_before_is_exclusive(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk("a", 10.0), "poll", 1.0)
    store.upsert_sighting(conn, mk("b", 20.0), "poll", 1.0)
    assert [s.native_key for s in store.list_sightings(conn, before=20.0)] == ["a"]
    assert [s.native_key for s in store.list_sightings(conn, since=10.0, before=20.0)] == ["a"]


def test_limit_clamped(conn: sqlite3.Connection) -> None:
    for i in range(3):
        store.upsert_sighting(conn, mk(f"k{i}", float(i)), "poll", 1.0)
    assert len(store.list_sightings(conn, limit=0)) == 1
    assert len(store.list_sightings(conn, limit=-5)) == 1
    assert len(store.list_sightings(conn, limit=10_000)) == 3


def test_open_sightings_only_non_final(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk("a", 10.0, native_id="1"), "poll", 1.0)
    store.upsert_sighting(conn, mk("b", 20.0, native_id="2", final=True), "poll", 1.0)
    store.upsert_sighting(conn, mk("c", 30.0), "poll", 1.0)
    store.upsert_sighting(conn, mk("d", 5.0), "poll", 1.0)
    assert store.open_sightings(conn, "src", since=10.0) == [("a", "1"), ("c", None)]
    assert store.open_sightings(conn, "nobody", since=0.0) == []


def test_newest_start_and_count_since(conn: sqlite3.Connection) -> None:
    assert store.newest_start(conn, "src") is None
    assert store.count_since(conn, "src", 0.0) == 0
    store.upsert_sighting(conn, mk("a", 10.0), "poll", 1.0)
    store.upsert_sighting(conn, mk("b", 20.0), "poll", 1.0)
    assert store.newest_start(conn, "src") == 20.0
    assert store.count_since(conn, "src", 15.0) == 1
    assert store.count_since(conn, "src", 10.0) == 2


def test_prune(conn: sqlite3.Connection) -> None:
    store.upsert_sighting(conn, mk("a", 10.0), "poll", 1.0)
    store.upsert_sighting(conn, mk("b", 20.0), "poll", 1.0)
    assert store.prune(conn, 20.0) == 1
    assert store.get_sighting(conn, "src", "a") is None
    assert store.get_sighting(conn, "src", "b") is not None
    assert store.prune(conn, 20.0) == 0


def test_state_roundtrip(conn: sqlite3.Connection) -> None:
    assert store.get_state(conn, "src") == {}
    store.set_state(conn, "src", "state", {"temp": 3, "ok": True}, 1.0)
    store.set_state(conn, "src", "availability", "online", 2.0)
    store.set_state(conn, "other", "availability", "offline", 2.0)
    assert store.get_state(conn, "src") == {
        "state": ({"temp": 3, "ok": True}, 1.0),
        "availability": ("online", 2.0),
    }
    store.set_state(conn, "src", "availability", "offline", 3.0)
    assert store.get_state(conn, "src")["availability"] == ("offline", 3.0)
    store.delete_state(conn, "src", "availability")
    assert "availability" not in store.get_state(conn, "src")
    assert "availability" in store.get_state(conn, "other")
    store.delete_state(conn, "src", "availability")  # no-op
