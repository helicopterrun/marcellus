"""`/v1/people`: the app-facing People API (handoff/sidecar-people-api.md).

A person is a face cluster with a permanent integer id and an optional name.
Reads are computed by `faces/people.py`; writes reuse the web UI's logic in
`faces/enrich.py` (rename, merge, soft-exclude, delete) so both surfaces have
the same effects. Ids merged away keep resolving to their survivor through
`face_cluster_redirects`. Names never go to the log.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import tzinfo
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from marcellus import db
from marcellus.errors import error_detail
from marcellus.faces import enrich, people
from marcellus.models.wire import (
    PeopleOkResponse,
    PeopleResponse,
    PersonDetail,
    PersonForEventResponse,
    PersonSightingPatchResponse,
    PersonSightingsResponse,
    SimilarPeopleResponse,
)
from marcellus.models.wire import Person as PersonOut
from marcellus.routes._deps import settings_of as _settings
from marcellus.routes.enrich import _crop_response

v1_router = APIRouter(prefix="/v1", tags=["v1"])

_T = TypeVar("_T")

_NAME_MAX = 100
_NOTE_MAX = 200


def _tz(name: str) -> tzinfo:
    zone = people.load_tz(name)
    if zone is None:
        raise HTTPException(
            status_code=422, detail=error_detail("invalid_tz", f"unknown time zone {name!r}")
        )
    return zone


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail=error_detail("person_not_found", "no such person")
    )


def _live_person(conn: sqlite3.Connection, cluster_id: int, tz: tzinfo) -> people.Person:
    """The person for a resolved cluster id, or 404 when it has no live sightings."""
    found = people.load_people(conn, tz, person_id=cluster_id).get(cluster_id)
    if found is None:
        raise _not_found()
    return found


def _resolve(conn: sqlite3.Connection, person_id: int) -> int:
    cid = people.resolve_id(conn, person_id)
    if cid is None:
        raise _not_found()
    return cid


async def _run(request: Request, fn: Callable[[sqlite3.Connection], _T]) -> _T:
    return await db.with_sidecar(_settings(request).sidecar.db_path, fn)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


@v1_router.get("/people", response_model=PeopleResponse)
async def list_people(
    request: Request,
    filter: Literal["all", "named", "regular", "new", "followed"] = "all",
    sort: Literal["recent", "days"] = "recent",
    tz: str = "UTC",
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> tuple[int, list[dict[str, Any]]]:
        rows = people.select(people.load_people(conn, zone), filter=filter, sort=sort)
        return len(rows), [p.wire() for p in rows[offset : offset + limit]]

    total, page = await _run(request, _do)
    return {"t": time.time(), "total": total, "people": page}


@v1_router.get("/people/for-event/{event_id}", response_model=PersonForEventResponse)
async def person_for_event(event_id: str, request: Request, tz: str = "UTC") -> dict[str, Any]:
    """The live person recognized on one event, else null (never a 404)."""
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute(
            "SELECT cluster_id FROM face_enrichments "
            "WHERE event_id = ? AND cluster_id IS NOT NULL AND excluded_at IS NULL",
            (event_id,),
        ).fetchone()
        if row is None:
            return {"person": None}
        found = people.load_people(conn, zone, person_id=int(row["cluster_id"])).get(
            int(row["cluster_id"])
        )
        return {"person": found.wire() if found else None}

    return await _run(request, _do)


@v1_router.get("/people/{person_id}", response_model=PersonDetail)
async def get_person(person_id: int, request: Request, tz: str = "UTC") -> dict[str, Any]:
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        found = people.load_people(conn, zone, person_id=cid, with_rhythm=True).get(cid)
        if found is None:
            raise _not_found()
        return {**found.wire(), "rhythm": found.rhythm}

    return await _run(request, _do)


@v1_router.get("/people/{person_id}/sightings", response_model=PersonSightingsResponse)
async def person_sightings(
    person_id: int,
    request: Request,
    before: float | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        cid = _resolve(conn, person_id)
        _live_person(conn, cid, _tz("UTC"))
        return people.sightings_page(conn, cid, before=before, limit=limit)

    return {"t": time.time(), "sightings": await _run(request, _do)}


@v1_router.get("/people/{person_id}/similar", response_model=SimilarPeopleResponse)
async def similar_people(person_id: int, request: Request, tz: str = "UTC") -> dict[str, Any]:
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        cid = _resolve(conn, person_id)
        everyone = people.load_people(conn, zone)
        if cid not in everyone:
            raise _not_found()
        near = people.similar(conn, cid, set(everyone))
        return [{**everyone[o].wire(), "distance": d} for o, d in near]

    return {"t": time.time(), "people": await _run(request, _do)}


# --------------------------------------------------------------------------
# Images (sync: FileResponse + the shared path-containment check)
# --------------------------------------------------------------------------


@v1_router.get("/people/{person_id}/avatar.jpg", response_class=FileResponse)
def person_avatar(person_id: int, request: Request) -> FileResponse:
    """The crop from the live sighting with the highest `best_quality`; a merged
    id serves its survivor's. One query resolves the redirect in SQL."""
    return _crop_response(
        _settings(request),
        "SELECT f.crop_path AS rel FROM face_enrichments e "
        "  JOIN face_crops f ON f.event_id = e.event_id AND f.crop_path IS NOT NULL "
        " WHERE e.excluded_at IS NULL AND e.cluster_id = "
        "       COALESCE((SELECT new_id FROM face_cluster_redirects WHERE old_id = ?), ?) "
        " ORDER BY e.best_quality DESC, e.event_start_ts DESC, f.rank LIMIT 1",
        (person_id, person_id),
    )


@v1_router.get("/faces/{event_id}/{rank}.jpg", response_class=FileResponse)
def face_crop(event_id: str, rank: int, request: Request) -> FileResponse:
    return _crop_response(
        _settings(request),
        "SELECT crop_path AS rel FROM face_crops WHERE event_id = ? AND rank = ?",
        (event_id, rank),
    )


# --------------------------------------------------------------------------
# Writes. Each follows merge redirects and returns the updated Person (the
# list shape). `tz` is optional on writes because `days` depends on it.
# --------------------------------------------------------------------------


class NameBody(BaseModel):
    name: str


class MergeBody(BaseModel):
    into: int


class FollowBody(BaseModel):
    followed: bool


class NoteBody(BaseModel):
    note: str | None = None


class SightingPatchBody(BaseModel):
    excluded: bool


@v1_router.post("/people/{person_id}/name", response_model=PersonOut)
async def name_person(
    person_id: int, body: NameBody, request: Request, tz: str = "UTC"
) -> dict[str, Any]:
    name = body.name.strip()
    if not name or len(name) > _NAME_MAX:
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_name", "name must be 1-100 chars")
        )
    zone = _tz(tz)
    settings = _settings(request)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        _live_person(conn, cid, zone)
        enrich.rename_cluster(conn, cid, name)
        conn.commit()
        enrich.retro_label(conn, settings, cid, name)
        conn.commit()
        return _live_person(conn, cid, zone).wire()

    return await _run(request, _do)


@v1_router.post("/people/{person_id}/merge", response_model=PersonOut)
async def merge_person(
    person_id: int, body: MergeBody, request: Request, tz: str = "UTC"
) -> dict[str, Any]:
    zone = _tz(tz)
    settings = _settings(request)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        source = _resolve(conn, person_id)
        target = _resolve(conn, body.into)
        if source == target:
            raise HTTPException(
                status_code=400,
                detail=error_detail("invalid_merge", "cannot merge a person into themselves"),
            )
        _live_person(conn, source, zone)
        _live_person(conn, target, zone)
        try:
            result = enrich.merge_clusters(conn, source, target, strict_names=True)
        except enrich.NameConflict:
            raise HTTPException(
                status_code=409,
                detail=error_detail("name_conflict", "both people already have different names"),
            ) from None
        conn.commit()
        if result.carried_name:
            enrich.retro_label(conn, settings, target, result.carried_name)
            conn.commit()
        return _live_person(conn, target, zone).wire()

    return await _run(request, _do)


@v1_router.put("/people/{person_id}/follow", response_model=PersonOut)
async def follow_person(
    person_id: int, body: FollowBody, request: Request, tz: str = "UTC"
) -> dict[str, Any]:
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        _live_person(conn, cid, zone)
        if body.followed:
            conn.execute(
                "UPDATE face_clusters SET followed_at = COALESCE(followed_at, ?) "
                "WHERE cluster_id = ?",
                (time.time(), cid),
            )
        else:
            conn.execute("UPDATE face_clusters SET followed_at = NULL WHERE cluster_id = ?", (cid,))
        conn.commit()
        return _live_person(conn, cid, zone).wire()

    return await _run(request, _do)


@v1_router.put("/people/{person_id}/note", response_model=PersonOut)
async def note_person(
    person_id: int, body: NoteBody, request: Request, tz: str = "UTC"
) -> dict[str, Any]:
    note = (body.note or "").strip()
    if len(note) > _NOTE_MAX:
        raise HTTPException(
            status_code=400, detail=error_detail("invalid_note", "note must be at most 200 chars")
        )
    zone = _tz(tz)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        _live_person(conn, cid, zone)
        conn.execute(
            "UPDATE face_clusters SET note = ? WHERE cluster_id = ?", (note or None, cid)
        )
        conn.commit()
        return _live_person(conn, cid, zone).wire()

    return await _run(request, _do)


@v1_router.patch(
    "/people/{person_id}/sightings/{event_id}", response_model=PersonSightingPatchResponse
)
async def patch_person_sighting(
    person_id: int,
    event_id: str,
    body: SightingPatchBody,
    request: Request,
    tz: str = "UTC",
) -> dict[str, Any]:
    zone = _tz(tz)
    settings = _settings(request)

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        row = conn.execute(
            "SELECT cluster_id, sub_label_written FROM face_enrichments WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None or row["cluster_id"] != cid:
            raise HTTPException(
                status_code=404,
                detail=error_detail("sighting_not_found", "no such sighting for this person"),
            )
        enrich.set_sighting_excluded(conn, event_id, body.excluded)
        conn.commit()
        # Keep Frigate's label in step, best-effort, with no transaction open.
        if body.excluded and row["sub_label_written"]:
            if enrich.write_sub_labels(settings, [event_id], ""):
                conn.execute(
                    "UPDATE face_enrichments SET sub_label_written = NULL WHERE event_id = ?",
                    (event_id,),
                )
                conn.commit()
        elif not body.excluded:
            named = conn.execute(
                "SELECT name FROM face_clusters WHERE cluster_id = ?", (cid,)
            ).fetchone()
            if named is not None and named["name"]:
                label = str(named["name"])
                if enrich.write_sub_labels(settings, [event_id], label):
                    conn.execute(
                        "UPDATE face_enrichments SET sub_label_written = ? WHERE event_id = ?",
                        (label, event_id),
                    )
                    conn.commit()
        found = people.load_people(conn, zone, person_id=cid).get(cid)
        return {"person": found.wire() if found else None}

    return await _run(request, _do)


@v1_router.delete("/people/{person_id}", response_model=PeopleOkResponse)
async def delete_person(person_id: int, request: Request) -> dict[str, Any]:
    settings = _settings(request)
    crops_dir = settings.face_enrich.crops_dir

    def _do(conn: sqlite3.Connection) -> dict[str, Any]:
        cid = _resolve(conn, person_id)
        labeled = [
            str(r["event_id"])
            for r in conn.execute(
                "SELECT event_id FROM face_enrichments "
                "WHERE cluster_id = ? AND sub_label_written IS NOT NULL",
                (cid,),
            )
        ]
        row = conn.execute("SELECT name FROM face_clusters WHERE cluster_id = ?", (cid,)).fetchone()
        name = str(row["name"]) if row is not None and row["name"] else None
        enrich.delete_cluster(conn, crops_dir, cid)
        if labeled:
            conn.executemany(
                "UPDATE face_enrichments SET sub_label_written = NULL WHERE event_id = ?",
                [(e,) for e in labeled],
            )
        if name is not None and not conn.execute(
            "SELECT 1 FROM face_clusters WHERE name = ?", (name,)
        ).fetchone():
            enrich.erase_name_everywhere(conn, name)
        conn.commit()
        enrich.write_sub_labels(settings, labeled, "")
        return {"ok": True}

    return await _run(request, _do)
