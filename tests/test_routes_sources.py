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


def _render(rows: Any) -> str:
    t = Jinja2Templates(directory=str(TEMPLATES))
    t.env.globals["fmt_ts"] = fmt.fmt_ts
    return t.env.get_template("_external_sightings.html").render(external_sightings=rows)


def test_partial_renders(env: Any) -> None:
    _client, svc, _ = env
    conn = db.open_sidecar(svc.db_path)
    rows = sources.sightings_for_page(
        conn, since=0.0, limit=2, source_names={"cam1": "Wildlife camera"}
    )
    conn.close()
    assert len(rows) == 2
    html = _render(rows)
    assert "Wildlife camera" in html
    assert 'loading="lazy"' in html and "Clip" in html
    assert rows[0]["subject"] in html
    for bad in ("/snap/", "/media/", "/poster/2", "192.168", "pi:8000"):
        assert bad not in html
    assert "<script" not in html

    mixed = [dict(rows[0]), dict(rows[1], source_name="Other")]
    assert "External sources" in _render(mixed)


def test_partial_empty() -> None:
    assert _render([]).strip() == ""
    assert _render(None).strip() == ""
