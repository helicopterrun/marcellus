from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from marcellus.config import BernardoSourceConfig
from marcellus.external.bernardo import BernardoSource
from marcellus.external.types import (
    AlertSignal,
    Availability,
    ExternalEventSource,
    SightingHint,
    StateReport,
)

FIX = Path(__file__).parent / "fixtures" / "external" / "bernardo"


def _load(name: str) -> Any:
    return json.loads((FIX / name).read_text())


ROWS: list[dict[str, Any]] = _load("api_events_list.json")
NOW = 1792123600.0


def make(clock: float = NOW) -> BernardoSource:
    cfg = BernardoSourceConfig(id="cam1", base_url="http://pi:8000/")
    return BernardoSource(cfg, clock=lambda: clock)


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://pi:8000")


def test_protocol_and_attrs() -> None:
    src = make()
    assert isinstance(src, ExternalEventSource)
    assert src.base_url == "http://pi:8000"
    assert src.kind == "bernardo"
    assert src.mqtt_topics() == [
        "bernardo/availability",
        "bernardo/state",
        "bernardo/event",
        "bernardo/alert",
    ]


def test_parse_fixture_rows() -> None:
    src = make(ROWS[0]["start_time"] + 60)
    parsed = [src.parse_row(r) for r in ROWS]
    assert [p.subject for p in parsed] == ["animal", "person", "none"]
    for p in parsed:
        assert [s.name for s in p.streams] == ["cam", "cam2"]
        assert [s.role for s in p.streams] == ["ir", "color"]
        assert [len(s.stills) for s in p.streams] == [12, 8]
        assert all(s.clip for s in p.streams)
        assert p.final and p.status == "complete"
        assert p.poster and p.poster.startswith("/poster/")
        assert p.trigger == "pir"
    assert all(s.has_audio for s in parsed[0].streams)
    assert not any(s.has_audio for s in parsed[1].streams)
    assert parsed[0].boxes and parsed[0].subject_conf == 0.927
    assert parsed[0].native_id == "2685"
    assert isinstance(parsed[0].raw["data"], dict)


def _row(**over: Any) -> dict[str, Any]:
    row = dict(ROWS[0])
    data = json.loads(row["data"])
    data.pop("classification", None)
    row["data"] = json.dumps(data)
    row.update(over)
    return row


def test_lifecycle() -> None:
    start = ROWS[0]["start_time"]
    fresh = make(start + 30)
    # terminal, unclassified, recent -> pending
    p = fresh.parse_row(_row())
    assert p.status == "pending" and not p.final and p.subject == "unknown"
    # past settle -> final
    late = make(start + 2000)
    p = late.parse_row(_row())
    assert p.final and p.status == "complete"
    p = late.parse_row(_row(has_clip=0))
    assert p.final and p.status == "failed"
    # terminal, classified, no clip -> failed
    r = dict(ROWS[0], has_clip=0)
    p = fresh.parse_row(r)
    assert p.final and p.status == "failed"


def test_non_terminal_row() -> None:
    start = ROWS[0]["start_time"]
    p = make(start + 30).parse_row(_row(end_time=None))
    assert p.status == "pending" and not p.final and p.end is None
    p = make(start + 2000).parse_row(_row(end_time=None))
    assert p.status == "failed" and p.final


def test_malformed_raises_and_fetch_recent_skips() -> None:
    bad = dict(ROWS[1], data="{not json")
    src = make()
    with pytest.raises(ValueError):
        src.parse_row(bad)

    async def go() -> list[Any]:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/events"
            assert req.url.params["limit"] == "5"
            return httpx.Response(200, json=[ROWS[0], bad, ROWS[2]])

        async with client_for(handler) as c:
            return await src.fetch_recent(c, 5)

    import asyncio

    got = asyncio.run(go())
    assert [s.native_id for s in got] == ["2685", "2679"]


async def test_fetch_recent_http_error_propagates() -> None:
    src = make()
    async with client_for(lambda r: httpx.Response(500)) as c:
        with pytest.raises(httpx.HTTPError):
            await src.fetch_recent(c, 5)


async def test_fetch_one() -> None:
    src = make()
    one = _load("api_event_one.json")

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/api/events/{one['id']}":
            return httpx.Response(200, json=one)
        return httpx.Response(404)

    async with client_for(handler) as c:
        got = await src.fetch_one(c, str(one["id"]))
        assert got is not None and got.native_id == str(one["id"])
        assert await src.fetch_one(c, "999999") is None


async def test_probe() -> None:
    src = make()
    health = _load("api_health.json")
    async with client_for(lambda r: httpx.Response(200, json=health)) as c:
        assert await src.probe(c) is True
    async with client_for(lambda r: httpx.Response(500)) as c:
        assert await src.probe(c) is False
    async with client_for(lambda r: httpx.Response(200, text="nope")) as c:
        assert await src.probe(c) is False

    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with client_for(boom) as c:
        assert await src.probe(c) is False


def test_mqtt_simple_signals() -> None:
    src = make()
    sig = src.parse_mqtt("bernardo/availability", b"online")
    assert sig == Availability(source_id="cam1", online=True)
    sig = src.parse_mqtt("bernardo/availability", b"offline")
    assert sig == Availability(source_id="cam1", online=False)
    assert src.parse_mqtt("bernardo/availability", b"huh") is None
    assert src.parse_mqtt("bernardo/availability", b"\xff\xfe") is None

    sig = src.parse_mqtt("bernardo/state", b'{"problems": ["cam2 down", 3]}')
    assert isinstance(sig, StateReport)
    assert sig.problems == ("cam2 down", "3")

    sig = src.parse_mqtt("bernardo/alert", b'{"code": "disk", "active": true, "message": "low"}')
    assert sig == AlertSignal(source_id="cam1", code="disk", active=True, message="low")
    assert src.parse_mqtt("bernardo/alert", b"{}") is None
    assert src.parse_mqtt("bernardo/state", b"not json") is None
    assert src.parse_mqtt("bernardo/daily", b"{}") is None
    assert src.parse_mqtt("homeassistant/x", b"{}") is None


def _event(**over: Any) -> bytes:
    p: dict[str, Any] = {
        "event_type": "unclassified",
        "key": "20261015_185239",
        "time": "2026-10-15T18:52:39",
        "status": "complete",
        "sensor": "pir",
        "confidence": None,
        "poster_url": "http://192.168.1.37:8000/poster/20261015_185239",
        "clip_url": "http://192.168.1.37:8000/clip",
        "page_url": "http://192.168.1.37:8000/event",
    }
    p.update(over)
    return json.dumps(p).encode()


def _assert_bare(sig: Any) -> None:
    assert isinstance(sig, SightingHint)
    s = sig.sighting
    assert s is not None
    assert s.poster is None and s.streams == ()
    assert s.status == "pending" and not s.final
    assert not any(k.endswith("_url") for k in s.raw)


def test_mqtt_event() -> None:
    from datetime import datetime

    src = make()
    sig = src.parse_mqtt("bernardo/event", _event())
    _assert_bare(sig)
    assert isinstance(sig, SightingHint) and sig.sighting is not None
    assert sig.native_key == "20261015_185239" and sig.native_id is None
    assert sig.sighting.start == datetime.fromisoformat("2026-10-15T18:52:39").timestamp()
    assert sig.sighting.subject == "unknown" and sig.sighting.trigger == "pir"

    sig = src.parse_mqtt("bernardo/event", _event(event_type="animal", confidence=0.9))
    assert isinstance(sig, SightingHint) and sig.sighting is not None
    assert sig.sighting.subject == "animal" and sig.sighting.subject_conf == 0.9

    sig = src.parse_mqtt("bernardo/event", _event(time=None))
    _assert_bare(sig)
    assert isinstance(sig, SightingHint) and sig.sighting is not None
    assert sig.sighting.start == datetime(2026, 10, 15, 18, 52, 39).timestamp()

    sig = src.parse_mqtt("bernardo/event", _event(time="garbage"))
    assert isinstance(sig, SightingHint) and sig.sighting is not None
    assert sig.sighting.start == datetime(2026, 10, 15, 18, 52, 39).timestamp()

    sig = src.parse_mqtt("bernardo/event", _event(time="2026-10-15T18:52:39+00:00"))
    assert isinstance(sig, SightingHint) and sig.sighting is not None
    assert sig.sighting.start == 1792090359.0

    sig = src.parse_mqtt("bernardo/event", _event(key=None))
    assert isinstance(sig, SightingHint)
    assert sig.native_key is None and sig.sighting is None

    assert src.parse_mqtt("bernardo/event", b"{bad") is None
