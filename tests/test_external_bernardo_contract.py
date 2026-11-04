"""Bernardo's vendored golden contract fixtures against Marcellus's adapter.

The fixtures under `tests/fixtures/external/bernardo/contract/` are copied
verbatim from github.com/helicopterrun/wildlife-cam (`fixtures/contract/`) by
`tools/sync-bernardo-fixtures.sh`, which records upstream commit + sha256 per
file in `VENDORED.json`. These tests pin that the real payloads still parse
the way the adapter and journal expect.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from marcellus import db
from marcellus.config import BernardoSourceConfig
from marcellus.external import store
from marcellus.external.bernardo import BernardoSource
from marcellus.external.types import Sighting, SightingHint, StateReport

FIX = Path(__file__).parent / "fixtures" / "external" / "bernardo" / "contract"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def make(clock: float) -> BernardoSource:
    cfg = BernardoSourceConfig(id="cam1", base_url="http://pi:8000/")
    return BernardoSource(cfg, clock=lambda: clock)


ANIMAL_ROW: dict[str, Any] = _load("api_event_animal_complete.json")
PENDING_ROW: dict[str, Any] = _load("api_event_media_pending.json")
MQTT_ANIMAL: bytes = (FIX / "mqtt_event_animal.json").read_bytes()


def settled(row: dict[str, Any], src: BernardoSource | None = None) -> float:
    s = src or make(0)
    return float(row["start_time"]) + s.settle_after_s + 60


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = db.open_sidecar(tmp_path / "s.db")
    try:
        yield c
    finally:
        c.close()


# ---- vendoring integrity ---------------------------------------------------


def test_vendored_manifest_matches_files() -> None:
    meta = _load("VENDORED.json")
    assert meta["repo"] == "helicopterrun/wildlife-cam"
    assert meta["path"] == "fixtures/contract"
    assert len(meta["ref"]) == 40
    on_disk = {p.name for p in FIX.iterdir() if p.is_file() and p.name != "VENDORED.json"}
    assert set(meta["files"]) == on_disk
    for name, digest in meta["files"].items():
        assert hashlib.sha256((FIX / name).read_bytes()).hexdigest() == digest, name


# ---- HTTP rows -------------------------------------------------------------


def test_animal_complete_row() -> None:
    manifest = _load("manifest_animal_complete.json")
    s = make(settled(ANIMAL_ROW)).parse_row(ANIMAL_ROW)
    assert s.subject == "animal"
    assert s.subject_conf == 0.927
    assert s.status == "complete"
    assert s.final is True
    assert s.native_key == manifest["event_key"]
    assert s.native_id == "2685"
    assert [st.name for st in s.streams] == ["cam", "cam2"]
    assert [st.role for st in s.streams] == ["ir", "color"]
    assert all(st.clip for st in s.streams)
    assert s.poster
    assert s.boxes
    all_stills = {p for st in s.streams for p in st.stills}
    for box in s.boxes:
        assert box.still in all_stills


def test_media_pending_row() -> None:
    start = float(PENDING_ROW["start_time"])
    early = make(start + 30).parse_row(PENDING_ROW)
    assert early.status == "pending"
    assert early.final is False
    assert all(st.clip is None for st in early.streams)
    assert not any(st.has_audio for st in early.streams)
    data = json.loads(PENDING_ROW["data"])
    assert [st.stills for st in early.streams] == [
        tuple(data["streams"][st.name]["stills"]) for st in early.streams
    ]

    src = make(0)
    late = make(start + src.settle_after_s + 1).parse_row(PENDING_ROW)
    assert late.status == "failed"
    assert late.final is True


# ---- journal interplay -----------------------------------------------------


def test_journal_final_never_downgrades(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pending = make(float(PENDING_ROW["start_time"]) + 30).parse_row(PENDING_ROW)
    complete = make(settled(ANIMAL_ROW)).parse_row(ANIMAL_ROW)
    assert store.upsert_sighting(conn, pending, "poll", 1.0) == "new"
    assert store.upsert_sighting(conn, complete, "poll", 2.0) == "new"

    # Synthetic: the pending poll and the complete poll are the same event.
    repoll = dataclasses.replace(
        pending, native_key=complete.native_key, native_id=complete.native_id
    )
    assert store.upsert_sighting(conn, repoll, "poll", 3.0) == "unchanged"

    # Fresh journal: pending first, then complete under the same key.
    first = dataclasses.replace(
        pending, native_key=complete.native_key, native_id=complete.native_id
    )
    c2 = db.open_sidecar(tmp_path / "t.db")
    try:
        assert store.upsert_sighting(c2, first, "poll", 1.0) == "new"
        assert store.upsert_sighting(c2, complete, "poll", 2.0) == "updated"
        got = store.get_sighting(c2, complete.source_id, complete.native_key)
        assert got is not None
        assert got.final is True and got.status == "complete"
        assert any(st.clip for st in got.streams)
        assert store.upsert_sighting(c2, first, "poll", 3.0) == "unchanged"
        got = store.get_sighting(c2, complete.source_id, complete.native_key)
        assert got is not None and got.final is True and got.status == "complete"

        hint = make(0).parse_mqtt("bernardo/event", MQTT_ANIMAL)
        assert isinstance(hint, SightingHint) and hint.sighting is not None
        assert store.upsert_sighting(c2, hint.sighting, "mqtt", 4.0) == "unchanged"
    finally:
        c2.close()


# ---- MQTT ------------------------------------------------------------------


def test_mqtt_event_animal() -> None:
    hint = make(0).parse_mqtt("bernardo/event", MQTT_ANIMAL)
    assert isinstance(hint, SightingHint)
    assert hint.native_key == "20261015_210520"
    s = hint.sighting
    assert isinstance(s, Sighting)
    assert s.subject == "animal"
    assert s.subject_conf == 0.927
    payload = json.loads(MQTT_ANIMAL)
    assert s.start == datetime.fromisoformat(payload["time"]).timestamp()
    api = make(settled(ANIMAL_ROW)).parse_row(ANIMAL_ROW)
    assert abs(s.start - api.start) < 1.0
    assert s.native_key == api.native_key
    assert s.poster is None
    assert s.streams == ()
    assert not any(k.endswith("_url") for k in s.raw)
    assert "192.168" not in json.dumps(dict(s.raw))


def test_mqtt_event_empty() -> None:
    hint = make(0).parse_mqtt("bernardo/event", (FIX / "mqtt_event_empty.json").read_bytes())
    assert isinstance(hint, SightingHint) and hint.sighting is not None
    assert hint.sighting.subject == "none"


def test_mqtt_state() -> None:
    raw = (FIX / "mqtt_state.json").read_bytes()
    sig = make(0).parse_mqtt("bernardo/state", raw)
    assert isinstance(sig, StateReport)
    assert sig.problems == tuple(str(p) for p in json.loads(raw)["problems"])
    assert sig.payload["mode"] == "night"


# ---- health ----------------------------------------------------------------


async def test_probe_health() -> None:
    body = (FIX / "api_health.json").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/health"
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://pi:8000"
    ) as client:
        assert await make(0).probe(client) is True
