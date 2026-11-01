"""Read logic behind `/v1/people` (docs: handoff/sidecar-people-api.md).

A *person* is a `face_clusters` row; a *sighting* is one non-excluded
`face_enrichments` row with a `cluster_id`. Days and the weekday/hour rhythm are
counted in the caller's timezone in Python from one query of those sightings --
at this scale (hundreds of clusters, a few thousand rows) that is cheaper to
reason about than SQL date arithmetic and avoids SQLite version differences.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from marcellus.faces import enrich

FILTERS = ("all", "named", "regular", "new", "followed")
SORTS = ("recent", "days")
SIMILAR_LIMIT = 5


def load_tz(name: str) -> tzinfo | None:
    """The IANA zone `name`, or None when it is not one this host knows."""
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        return None


def familiarity(name: str | None, days: int) -> str:
    """`named` beats everything; else `regular` (2+ local days) or `new`."""
    if name:
        return "named"
    return "regular" if days >= 2 else "new"


@dataclass
class Person:
    id: int
    name: str | None
    note: str | None
    followed: bool
    sightings: int = 0
    first_seen: float | None = None
    last_seen: float | None = None
    last_camera: str | None = None
    #: (best_quality or -inf, start, -rank) of the chosen avatar crop, and the crop.
    avatar_key: tuple[float, float, int] | None = None
    avatar_crop: tuple[str, int] | None = None
    day_set: set[Any] = field(default_factory=set)
    rhythm: list[list[int]] | None = None

    @property
    def days(self) -> int:
        return len(self.day_set)

    def wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "note": self.note,
            "followed": self.followed,
            "familiarity": familiarity(self.name, self.days),
            "sightings": self.sightings,
            "days": self.days,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_camera": self.last_camera,
            # The crop's own path, so it changes whenever the best crop does
            # (clients cache images by path).
            "avatar_path": (
                f"/v1/faces/{self.avatar_crop[0]}/{self.avatar_crop[1]}.jpg"
                if self.avatar_crop
                else None
            ),
        }


def resolve_id(conn: sqlite3.Connection, person_id: int) -> int | None:
    """The live cluster `person_id` stands for: itself, or its merge survivor.
    None when it never existed, was deleted, or expired."""
    if conn.execute(
        "SELECT 1 FROM face_clusters WHERE cluster_id = ?", (person_id,)
    ).fetchone():
        return person_id
    row = conn.execute(
        "SELECT c.cluster_id FROM face_cluster_redirects r "
        "JOIN face_clusters c ON c.cluster_id = r.new_id WHERE r.old_id = ?",
        (person_id,),
    ).fetchone()
    return int(row["cluster_id"]) if row else None


def load_people(
    conn: sqlite3.Connection,
    tz: tzinfo,
    *,
    person_id: int | None = None,
    with_rhythm: bool = False,
) -> dict[int, Person]:
    """Every person with at least one live sighting (or just `person_id`),
    keyed by cluster id. A cluster whose sightings are all excluded is absent."""
    where = "" if person_id is None else "AND e.cluster_id = ? "
    args: tuple[Any, ...] = () if person_id is None else (person_id,)
    people: dict[int, Person] = {}
    last_start: dict[int, float] = {}
    for r in conn.execute(
        "SELECT e.cluster_id, e.event_id, e.event_start_ts, e.camera, e.best_quality, "
        "       c.name, c.note, c.followed_at, "
        "       (SELECT MIN(f.rank) FROM face_crops f "
        "         WHERE f.event_id = e.event_id AND f.crop_path IS NOT NULL) AS crop_rank "
        "  FROM face_enrichments e JOIN face_clusters c ON c.cluster_id = e.cluster_id "
        " WHERE e.excluded_at IS NULL " + where + "ORDER BY e.event_start_ts",
        args,
    ):
        cid = int(r["cluster_id"])
        p = people.get(cid)
        if p is None:
            p = people[cid] = Person(
                id=cid,
                name=r["name"],
                note=r["note"],
                followed=r["followed_at"] is not None,
            )
            if with_rhythm:
                p.rhythm = [[0] * 24 for _ in range(7)]
        ts = float(r["event_start_ts"])
        local = datetime.fromtimestamp(ts, tz)
        p.sightings += 1
        p.day_set.add(local.date())
        if p.first_seen is None or ts < p.first_seen:
            p.first_seen = ts
        if ts >= last_start.get(cid, float("-inf")):
            last_start[cid] = ts
            p.last_seen = ts
            p.last_camera = r["camera"]
        if r["crop_rank"] is not None:
            # Same choice as the avatar route: best_quality (NULL last), then
            # newest start, then lowest rank.
            quality = r["best_quality"]
            key = (float("-inf") if quality is None else float(quality), ts, -int(r["crop_rank"]))
            if p.avatar_key is None or key > p.avatar_key:
                p.avatar_key = key
                p.avatar_crop = (str(r["event_id"]), int(r["crop_rank"]))
        if p.rhythm is not None:
            p.rhythm[local.weekday()][local.hour] += 1
    return people


def select(
    people: dict[int, Person], *, filter: str, sort: str
) -> list[Person]:
    """Apply a list filter and sort order."""

    def keep(p: Person) -> bool:
        if filter == "all":
            return True
        if filter == "followed":
            return p.followed
        return familiarity(p.name, p.days) == filter

    rows = [p for p in people.values() if keep(p)]
    if sort == "days":
        rows.sort(key=lambda p: (p.days, p.last_seen or 0.0, p.id), reverse=True)
    else:
        rows.sort(key=lambda p: (p.last_seen or 0.0, p.id), reverse=True)
    return rows


def similar(
    conn: sqlite3.Connection, cluster_id: int, candidates: set[int], limit: int = SIMILAR_LIMIT
) -> list[tuple[int, float]]:
    """Up to `limit` other clusters in `candidates` whose centroids are within
    the web UI's merge-hint threshold of `cluster_id`'s, closest first."""
    mine = conn.execute(
        "SELECT centroid FROM face_clusters WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    if mine is None:
        return []
    own = enrich.l2_normalize(enrich.unpack_embedding(mine["centroid"]))
    found: list[tuple[int, float]] = []
    for r in conn.execute("SELECT cluster_id, centroid FROM face_clusters"):
        cid = int(r["cluster_id"])
        if cid == cluster_id or cid not in candidates:
            continue
        other = enrich.l2_normalize(enrich.unpack_embedding(r["centroid"]))
        if len(other) != len(own):
            continue
        d = enrich.cosine_distance(own, other)
        if d <= enrich.SUGGEST_THRESHOLD:
            found.append((cid, round(d, 3)))
    found.sort(key=lambda t: (t[1], t[0]))
    return found[:limit]


def sightings_page(
    conn: sqlite3.Connection, cluster_id: int, *, before: float | None, limit: int
) -> list[dict[str, Any]]:
    """Live sightings newest first (start < `before`), with rendered crop paths."""
    sql = (
        "SELECT event_id, camera, event_start_ts, best_frame_ts, distance "
        "  FROM face_enrichments WHERE cluster_id = ? AND excluded_at IS NULL "
    )
    args: list[Any] = [cluster_id]
    if before is not None:
        sql += "AND event_start_ts < ? "
        args.append(before)
    sql += "ORDER BY event_start_ts DESC, event_id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        eid = str(r["event_id"])
        ranks = [
            int(f["rank"])
            for f in conn.execute(
                "SELECT rank FROM face_crops WHERE event_id = ? AND crop_path IS NOT NULL "
                "ORDER BY rank",
                (eid,),
            )
        ]
        out.append(
            {
                "event_id": eid,
                "camera": r["camera"],
                "start": float(r["event_start_ts"]),
                "face_time": r["best_frame_ts"],
                "distance": r["distance"],
                "crop_paths": [f"/v1/faces/{eid}/{k}.jpg" for k in ranks],
            }
        )
    return out
