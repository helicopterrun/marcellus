"""External event sources (Bernardo wildlife camera, ...): the `/v1` JSON
read surface for sightings plus the media relay routes.

Upstream media paths live only in the journal. Clients address media by
(kind, stream, index) and these routes resolve that against the stored
sighting via `media_upstream_path`; an upstream path never appears in any
JSON body. The service object (`app.state.external_sources`) is absent when
the feature is disabled."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, Protocol
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.responses import Response

from marcellus import db
from marcellus.errors import error_detail
from marcellus.external import store
from marcellus.external.types import MediaRef, Sighting, media_upstream_path
from marcellus.models.wire import SightingDetail, SightingsResponse

v1_router = APIRouter(prefix="/v1", tags=["v1"])


class _Service(Protocol):
    db_path: str

    def source_name(self, source_id: str) -> str | None: ...

    async def relay_media(
        self, source_id: str, upstream_path: str, headers: Mapping[str, str]
    ) -> Response: ...


def _service(request: Request) -> _Service:
    service: _Service | None = getattr(request.app.state, "external_sources", None)
    if service is None:
        raise HTTPException(
            404,
            error_detail("external_sources_disabled", "External sources are not enabled."),
        )
    return service


def _q(segment: str) -> str:
    return quote(segment, safe="")


def _detail_url(s: Sighting) -> str:
    return f"/v1/sources/{_q(s.source_id)}/sightings/{_q(s.native_key)}"


def sighting_summary(s: Sighting) -> dict[str, Any]:
    base = _detail_url(s)
    clip_stream = s.preferred_clip_stream()
    return {
        "id": s.wire_id,
        "source": s.source_id,
        "key": s.native_key,
        "start": s.start,
        "end": s.end,
        "status": s.status,
        "final": s.final,
        "subject": s.subject,
        "subject_conf": s.subject_conf,
        "labels": list(s.labels),
        "trigger": s.trigger,
        "is_dark": s.is_dark,
        "poster_url": base + "/poster" if s.poster else None,
        "clip_url": base + f"/clip/{_q(clip_stream.name)}" if clip_stream else None,
        "detail_url": base,
    }


def sighting_detail(s: Sighting, source_name: str) -> dict[str, Any]:
    base = _detail_url(s)
    still_urls: dict[str, str] = {}
    streams: list[dict[str, Any]] = []
    for st in s.streams:
        urls = []
        for i, path in enumerate(st.stills):
            url = base + f"/stills/{_q(st.name)}/{i}"
            urls.append(url)
            still_urls.setdefault(path, url)
        streams.append(
            {
                "name": st.name,
                "role": st.role,
                "stills": urls,
                "clip_url": base + f"/clip/{_q(st.name)}" if st.clip else None,
                "has_audio": st.has_audio,
            }
        )
    boxes = [
        {
            "label": b.label,
            "conf": b.conf,
            "box": list(b.box),
            "still_url": still_urls.get(b.still) if b.still else None,
        }
        for b in s.boxes
    ]
    out = sighting_summary(s)
    out.update(
        {
            "source_name": source_name,
            "model": s.model,
            "streams": streams,
            "boxes": boxes,
        }
    )
    return out


_STREAM_LABELS = {"color": "Colour", "ir": "IR"}


def _page_item(s: Sighting, name: str) -> dict[str, Any]:
    d = sighting_summary(s)
    base = _detail_url(s)
    primary = s.preferred_clip_stream()
    ordered = sorted(
        (st for st in s.streams if st.clip), key=lambda st: primary is None or st is not primary
    )
    d["source_name"] = name
    d["conf_pct"] = None if s.subject_conf is None else int(round(s.subject_conf * 100))
    d["time_label"] = datetime.fromtimestamp(s.start).strftime("%H:%M")
    d["hover_clip_url"] = d["clip_url"]
    d["streams"] = [
        {
            "name": st.name,
            "role": st.role,
            "label": _STREAM_LABELS.get(st.role, st.name),
            "clip_url": base + f"/clip/{_q(st.name)}",
        }
        for st in ordered
    ]
    return d


def sightings_for_page(
    conn: sqlite3.Connection,
    *,
    since: float,
    limit: int = 50,
    source_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Recent sightings for the Encounters HTML page: summary dicts plus
    `source_name` and the gallery keys (`conf_pct`, `time_label`,
    `hover_clip_url`, `streams`). Rows from sources not in `source_names`
    are dropped."""
    rows = store.list_sightings(conn, since=since, limit=limit)
    out: list[dict[str, Any]] = []
    for s in rows:
        name = source_names.get(s.source_id)
        if name is None:
            continue
        out.append(_page_item(s, name))
    return out


def _filter_class(subject: str) -> str:
    if subject == "person":
        return "people"
    if subject == "none":
        return "empty"
    return "wildlife"


def _counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    c = {"wildlife": 0, "people": 0, "empty": 0, "all": len(items)}
    for it in items:
        c[_filter_class(it["subject"])] += 1
    return c


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _day_summary(items: Sequence[Mapping[str, Any]]) -> str:
    by: dict[str, int] = {}
    for it in items:
        by[it["subject"]] = by.get(it["subject"], 0) + 1
    parts = (
        (by.get("animal", 0), "animal", "animals"),
        (by.get("person", 0), "person", "people"),
        (by.get("vehicle", 0), "vehicle", "vehicles"),
        (by.get("none", 0), "empty", "empty"),
        (by.get("unknown", 0), "not yet classified", "not yet classified"),
    )
    return " \u00b7 ".join(_plural(n, a, b) for n, a, b in parts if n)


def group_sightings_by_day(
    items: Sequence[Mapping[str, Any]], *, now: float
) -> list[dict[str, Any]]:
    """Group page items by local calendar day, newest day first, items newest
    first. Local time matches `fmt.fmt_ts`."""
    today = datetime.fromtimestamp(now).date()
    buckets: dict[Any, list[dict[str, Any]]] = {}
    for it in sorted(items, key=lambda i: i["start"], reverse=True):
        day = datetime.fromtimestamp(it["start"]).date()
        buckets.setdefault(day, []).append(dict(it))
    groups: list[dict[str, Any]] = []
    for day in sorted(buckets, reverse=True):
        delta = (today - day).days
        if delta == 0:
            label = "Today"
        elif delta == 1:
            label = "Yesterday"
        else:
            label = f"{day:%A} {day.day} {day:%b}"
        day_items = buckets[day]
        for it in day_items:
            it["day_label"] = label
        groups.append(
            {
                "key": day.isoformat(),
                "label": label,
                "summary": _day_summary(day_items),
                "counts": _counts(day_items),
                "items": day_items,
            }
        )
    return groups


def sightings_page_context(
    conn: sqlite3.Connection,
    *,
    since: float,
    now: float,
    source_names: Mapping[str, str],
    limit: int = 300,
) -> dict[str, Any] | None:
    items = sightings_for_page(conn, since=since, limit=limit, source_names=source_names)
    if not items:
        return None
    names = sorted({i["source_name"] for i in items})
    return {
        "source_name": names[0] if len(names) == 1 else "External sources",
        "groups": group_sightings_by_day(items, now=now),
        "totals": _counts(items),
    }


@v1_router.get("/sightings", response_model=SightingsResponse)
async def list_sightings(
    request: Request,
    source: str | None = None,
    subject: Literal["animal", "person", "vehicle", "none", "unknown"] | None = None,
    since: float | None = None,
    before: float | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    service = _service(request)
    if source is not None and service.source_name(source) is None:
        raise HTTPException(404, error_detail("unknown_source", f"Unknown source {source!r}."))
    rows = await db.with_sidecar(
        service.db_path,
        lambda conn: store.list_sightings(
            conn, source_id=source, subject=subject, since=since, before=before, limit=limit
        ),
    )
    kept = [s for s in rows if service.source_name(s.source_id) is not None]
    next_before = rows[-1].start if len(rows) == limit else None
    return {"sightings": [sighting_summary(s) for s in kept], "next_before": next_before}


async def _load(service: _Service, source_id: str, key: str) -> Sighting:
    if service.source_name(source_id) is None:
        raise HTTPException(404, error_detail("not_found", "Sighting not found."))
    s = await db.with_sidecar(
        service.db_path, lambda conn: store.get_sighting(conn, source_id, key)
    )
    if s is None:
        raise HTTPException(404, error_detail("not_found", "Sighting not found."))
    return s


@v1_router.get("/sources/{source_id}/sightings/{key}", response_model=SightingDetail)
async def sighting_detail_route(request: Request, source_id: str, key: str) -> dict[str, Any]:
    service = _service(request)
    s = await _load(service, source_id, key)
    name = service.source_name(source_id) or source_id
    return sighting_detail(s, name)


async def _relay(request: Request, source_id: str, key: str, ref: MediaRef) -> Response:
    service = _service(request)
    s = await _load(service, source_id, key)
    path = media_upstream_path(s, ref)
    if path is None:
        raise HTTPException(404, error_detail("no_media", "No such media for this sighting."))
    return await service.relay_media(source_id, path, request.headers)


@v1_router.get("/sources/{source_id}/sightings/{key}/poster")
async def sighting_poster(request: Request, source_id: str, key: str) -> Response:
    return await _relay(request, source_id, key, MediaRef("poster"))


@v1_router.get("/sources/{source_id}/sightings/{key}/stills/{stream}/{index}")
async def sighting_still(
    request: Request, source_id: str, key: str, stream: str, index: int
) -> Response:
    return await _relay(request, source_id, key, MediaRef("still", stream, index))


@v1_router.get("/sources/{source_id}/sightings/{key}/clip/{stream}")
async def sighting_clip(request: Request, source_id: str, key: str, stream: str) -> Response:
    return await _relay(request, source_id, key, MediaRef("clip", stream))
