from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse, Response

from marcellus import db, fmt
from marcellus.config import BernardoSourceConfig
from marcellus.external import store
from marcellus.external.bernardo import BernardoSource
from marcellus.models.wire import SightingDetail, SightingSummary
from marcellus.routes import sources

FIX = Path(__file__).parent / "fixtures" / "external" / "bernardo"
ROWS: list[dict[str, Any]] = json.loads((FIX / "api_events_list.json").read_text())
TEMPLATES = Path(sources.__file__).resolve().parent.parent / "templates"


class FakeService:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.calls: list[tuple[str, str]] = []

    def source_name(self, source_id: str) -> str | None:
        return "Wildlife camera" if source_id == "cam1" else None

    async def relay_media(
        self, source_id: str, upstream_path: str, headers: Mapping[str, str]
    ) -> Response:
        self.calls.append((source_id, upstream_path))
        return PlainTextResponse("ok")


@pytest.fixture
def env(tmp_path: Path) -> tuple[TestClient, FakeService, list[Any]]:
    path = str(tmp_path / "sidecar.db")
    src = BernardoSource(BernardoSourceConfig(id="cam1", base_url="http://pi:8000"))
    parsed = [src.parse_row(r) for r in ROWS]
    conn = db.open_sidecar(path)
    for p in parsed:
        store.upsert_sighting(conn, p, "poll", 1.0)
    conn.close()
    svc = FakeService(path)
    app = FastAPI()
    app.include_router(sources.v1_router)
    app.state.external_sources = svc
    return TestClient(app), svc, parsed


def test_list_ordering_limit_next_before_filter(env: Any) -> None:
    client, _svc, parsed = env
    body = client.get("/v1/sightings").json()
    starts = [s["start"] for s in body["sightings"]]
    assert starts == sorted(starts, reverse=True)
    assert len(starts) == len(parsed)
    assert body["next_before"] is None
    for s in body["sightings"]:
        SightingSummary.model_validate(s)

    page = client.get("/v1/sightings", params={"limit": 2}).json()
    assert len(page["sightings"]) == 2
    assert page["next_before"] == page["sightings"][-1]["start"]
    rest = client.get("/v1/sightings", params={"before": page["next_before"]}).json()
    assert len(rest["sightings"]) == len(parsed) - 2

    animals = client.get("/v1/sightings", params={"subject": "animal"}).json()["sightings"]
    assert animals and all(s["subject"] == "animal" for s in animals)
    assert client.get("/v1/sightings", params={"subject": "bogus"}).status_code == 422
    assert client.get("/v1/sightings", params={"limit": 0}).status_code == 422


def test_list_unknown_source(env: Any) -> None:
    client, _svc, _ = env
    r = client.get("/v1/sightings", params={"source": "nope"})
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "unknown_source"
    assert len(client.get("/v1/sightings", params={"source": "cam1"}).json()["sightings"]) == 3


def test_detail_shape_has_no_upstream_paths(env: Any) -> None:
    client, _svc, parsed = env
    s = parsed[0]
    r = client.get(f"/v1/sources/cam1/sightings/{s.native_key}")
    assert r.status_code == 200
    SightingDetail.model_validate(r.json())
    text = json.dumps(r.json())
    for bad in ("/snap/", "/media/", "/poster/2", "192.168"):
        assert bad not in text
    d = r.json()
    assert d["source_name"] == "Wildlife camera"
    assert d["streams"][0]["stills"][0].endswith("/stills/cam/0")
    assert d["boxes"] and d["boxes"][0]["still_url"] is not None

    assert client.get("/v1/sources/cam1/sightings/missing").json()["detail"]["error"] == "not_found"
    assert client.get("/v1/sources/zzz/sightings/x").status_code == 404


def test_media_relay(env: Any) -> None:
    client, svc, parsed = env
    s = parsed[0]
    base = f"/v1/sources/cam1/sightings/{s.native_key}"
    assert client.get(base + "/poster").status_code == 200
    assert client.get(base + "/stills/cam/1").status_code == 200
    clip_stream = s.preferred_clip_stream()
    assert clip_stream is not None
    assert client.get(base + f"/clip/{clip_stream.name}").status_code == 200
    assert svc.calls == [
        ("cam1", s.poster),
        ("cam1", s.streams[0].stills[1]),
        ("cam1", clip_stream.clip),
    ]
    r = client.get(base + "/stills/cam/999")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "no_media"
    r = client.get(base + "/clip/nostream")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "no_media"
    assert len(svc.calls) == 3


def test_service_absent() -> None:
    app = FastAPI()
    app.include_router(sources.v1_router)
    client = TestClient(app)
    for url in (
        "/v1/sightings",
        "/v1/sources/cam1/sightings/x",
        "/v1/sources/cam1/sightings/x/poster",
    ):
        r = client.get(url)
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "external_sources_disabled"


def _render(ctx: Any) -> str:
    t = Jinja2Templates(directory=str(TEMPLATES))
    t.env.globals["fmt_ts"] = fmt.fmt_ts
    return t.env.get_template("_external_sightings.html").render(external_sightings=ctx)


NAMES = {"cam1": "Wildlife camera"}


def _page_ctx(env: Any, limit: int = 300) -> dict[str, Any]:
    _client, svc, parsed = env
    conn = db.open_sidecar(svc.db_path)
    ctx = sources.sightings_page_context(
        conn, since=0.0, now=max(p.start for p in parsed) + 60, source_names=NAMES, limit=limit
    )
    conn.close()
    assert ctx is not None
    return ctx


def test_partial_renders(env: Any) -> None:
    ctx = _page_ctx(env)
    html = _render(ctx)
    assert "Wildlife camera" in html
    for key in ("wildlife", "people", "empty", "all"):
        assert f'data-filter="{key}"' in html
    assert f'<span class="n">{ctx["totals"]["all"]}</span>' in html
    assert 'data-class="' in html and "ws-cap" in html and "ws-time" in html
    assert "ws-lightbox" in html and "ws-dayhead" in html
    assert 'preload="none"' in html and " muted " in html
    for bad in ("/snap/", "/media/", "/poster/2", "192.168", "pi:8000"):
        assert bad not in html
    assert "<script" not in html


def test_partial_empty() -> None:
    assert _render(None).strip() == ""
    assert _render([]).strip() == ""
    assert _render({}).strip() == ""


def test_page_context_none_when_empty(env: Any) -> None:
    _client, svc, _ = env
    conn = db.open_sidecar(svc.db_path)
    assert sources.sightings_page_context(conn, since=0.0, now=1e10, source_names={}) is None
    conn.close()


def test_page_items_primary_stream_first(env: Any) -> None:
    _client, svc, parsed = env
    conn = db.open_sidecar(svc.db_path)
    items = sources.sightings_for_page(conn, since=0.0, limit=300, source_names=NAMES)
    conn.close()
    by_key = {p.native_key: p for p in parsed}
    seen_multi = False
    for it in items:
        s = by_key[it["key"]]
        pref = s.preferred_clip_stream()
        assert it["hover_clip_url"] == it["clip_url"]
        assert it["time_label"] and len(it["time_label"]) == 5
        if pref is None:
            assert it["streams"] == [] and it["hover_clip_url"] is None
            continue
        assert it["streams"][0]["name"] == pref.name
        assert it["hover_clip_url"] == it["streams"][0]["clip_url"]
        assert all(st["label"] in ("Colour", "IR", st["name"]) for st in it["streams"])
        for st in it["streams"]:
            if st["role"] == "color":
                assert st["label"] == "Colour"
            if st["role"] == "ir":
                assert st["label"] == "IR"
        seen_multi = seen_multi or len(it["streams"]) > 1
        assert "192.168" not in json.dumps(it)


def _item(start: float, subject: str) -> dict[str, Any]:
    return {"start": start, "subject": subject}


def test_group_by_day_labels_order_and_counts() -> None:
    from datetime import datetime

    now = datetime(2026, 10, 14, 15, 0).timestamp()  # a Wednesday
    mk = lambda d, h, subj: _item(datetime(2026, 10, d, h, 0).timestamp(), subj)  # noqa: E731
    items = [
        mk(12, 9, "animal"),
        mk(14, 8, "animal"),
        mk(14, 13, "animal"),
        mk(13, 10, "person"),
        mk(14, 11, "unknown"),
        mk(13, 12, "person"),
        mk(13, 14, "person"),
        mk(13, 15, "none"),
        mk(14, 12, "vehicle"),
    ]
    groups = sources.group_sightings_by_day(items, now=now)
    assert [g["label"] for g in groups] == ["Today", "Yesterday", "Monday 12 Oct"]
    assert [g["key"] for g in groups] == ["2026-10-14", "2026-10-13", "2026-10-12"]
    today = groups[0]
    assert [i["start"] for i in today["items"]] == sorted(
        (i["start"] for i in today["items"]), reverse=True
    )
    assert today["summary"] == "2 animals \u00b7 1 vehicle \u00b7 1 not yet classified"
    assert today["counts"] == {"wildlife": 4, "people": 0, "empty": 0, "all": 4}
    assert groups[1]["summary"] == "3 people \u00b7 1 empty"
    assert groups[1]["counts"] == {"wildlife": 0, "people": 3, "empty": 1, "all": 4}
    assert groups[2]["summary"] == "1 animal"
    assert groups[2]["counts"]["wildlife"] == 1
    solo = sources.group_sightings_by_day([mk(14, 8, "person")], now=now)
    assert solo[0]["summary"] == "1 person"
