"""Blind face-pair labelling: a real measure of face identity quality.

A person who knows the faces marks pairs of sightings as the same person or
different people, WITHOUT seeing what the model thinks. Two kinds of pair are
drawn:

* `split`: "is this ID a duplicate of another ID?" -- a cluster's best sighting
  against its nearest sighting in any other cluster. "Same" answers are
  duplicates the clustering missed.
* `join`: "is this sighting really this ID?" -- a sighting against another
  sighting of its own cluster (preferring a different visit). "Different"
  answers are wrong joins.

The tallies live in `face_pair_labels` (db.py). This module only ever writes
that table: it never touches `face_clusters`, `face_enrichments` or
`face_crops`, and it never merges anything. Pair rows are about EVENTS, so a
later merge never rewrites them; `results` follows `face_cluster_redirects`
to find where a labelled cluster lives now. Deleting or reaping an identity
removes its pairs (enrich._forget_pair_labels), so results can shrink over time.

Results are withheld until every drawn pair is labelled (`state`): running
rates would bias the labeller.
"""

from __future__ import annotations

import math
import random
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from marcellus.faces import enrich
from marcellus.faces.handles import handle_for

#: Two sightings on one camera this close in time are one visit; a join pair
#: prefers a partner further away than this.
VISIT_GAP_S = 3600.0

#: Face crops shown per side.
MAX_CROPS = 3

LABELS = ("same", "different", "unsure")

#: Half-open distance bands [lo, hi) for the results table.
BAND_EDGES = (0.0, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 2.0)


@dataclass(frozen=True)
class _Sighting:
    event_id: str
    camera: str
    ts: float
    cluster: int
    quality: float
    vec: list[float]  # L2-normalized


@dataclass(frozen=True)
class _Pair:
    kind: str
    a: _Sighting
    b: _Sighting
    distance: float


def _load(conn: sqlite3.Connection) -> list[_Sighting]:
    """Eligible sightings, in event_id order, all of one embedding dimension."""
    raw = conn.execute(
        "SELECT event_id, camera, event_start_ts, cluster_id, best_quality, embedding "
        "  FROM face_enrichments "
        " WHERE status = 'enriched' AND cluster_id IS NOT NULL AND embedding IS NOT NULL "
        "   AND excluded_at IS NULL AND best_crop_path IS NOT NULL "
        " ORDER BY event_id"
    ).fetchall()
    if not raw:
        return []
    dim = Counter(len(r["embedding"]) // 4 for r in raw).most_common(1)[0][0]
    out = []
    for r in raw:
        if len(r["embedding"]) // 4 != dim or dim == 0:
            continue
        out.append(
            _Sighting(
                event_id=str(r["event_id"]),
                camera=str(r["camera"]),
                ts=float(r["event_start_ts"]),
                cluster=int(r["cluster_id"]),
                quality=float(r["best_quality"]) if r["best_quality"] is not None else -math.inf,
                vec=enrich.l2_normalize(enrich.unpack_embedding(r["embedding"])),
            )
        )
    return out


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _existing_keys(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {
        _key(str(r["event_a"]), str(r["event_b"]))
        for r in conn.execute("SELECT event_a, event_b FROM face_pair_labels")
    }


def _split_pairs(
    sights: list[_Sighting], taken: set[tuple[str, str]], rng: random.Random
) -> Iterator[_Pair]:
    """Each cluster's best sighting against its nearest sighting elsewhere,
    clusters in uniformly shuffled order."""
    by_cluster: dict[int, list[int]] = {}
    for i, s in enumerate(sights):
        by_cluster.setdefault(s.cluster, []).append(i)
    if len(by_cluster) < 2:
        return
    cluster_ids = sorted(by_cluster)
    rng.shuffle(cluster_ids)
    cluster_of = [s.cluster for s in sights]
    try:
        import numpy as np
    except ImportError:
        np = None
    mat = np.array([s.vec for s in sights], dtype=np.float64) if np is not None else None
    for cid in cluster_ids:
        members = by_cluster[cid]
        # Highest quality; ties go to the smallest event_id (sights are
        # event_id-sorted, so the first of equals wins).
        best = max(members, key=lambda i: (sights[i].quality, -i))
        if mat is not None:
            row = 1.0 - mat @ mat[best]
            row[[i for i, c in enumerate(cluster_of) if c == cid]] = np.inf
            j = int(row.argmin())
            d = float(row[j])
        else:
            j, d = -1, math.inf
            for i, s in enumerate(sights):
                if s.cluster == cid:
                    continue
                dist = enrich.cosine_distance(sights[best].vec, s.vec)
                if dist < d:
                    j, d = i, dist
        if j < 0 or not math.isfinite(d):
            continue
        if _key(sights[best].event_id, sights[j].event_id) in taken:
            continue
        yield _Pair("split", sights[best], sights[j], d)


def _join_pairs(
    sights: list[_Sighting], taken: set[tuple[str, str]], rng: random.Random
) -> Iterator[_Pair]:
    """Each sighting against the member of its own cluster closest to the
    cluster mean, preferring a different visit; sightings in uniform order."""
    by_cluster: dict[int, list[_Sighting]] = {}
    for s in sights:
        by_cluster.setdefault(s.cluster, []).append(s)
    pool = [s for s in sights if len(by_cluster[s.cluster]) >= 2]
    rng.shuffle(pool)
    means: dict[int, list[float]] = {}
    for s in pool:
        members = by_cluster[s.cluster]
        mean = means.get(s.cluster)
        if mean is None:
            dim = len(members[0].vec)
            mean = enrich.l2_normalize([sum(m.vec[k] for m in members) for k in range(dim)])
            means[s.cluster] = mean
        others = [m for m in members if m.event_id != s.event_id]
        far = [m for m in others if abs(m.ts - s.ts) > VISIT_GAP_S]
        partner = min(
            far or others, key=lambda m: (enrich.cosine_distance(m.vec, mean), m.event_id)
        )
        if _key(s.event_id, partner.event_id) in taken:
            continue
        yield _Pair("join", s, partner, enrich.cosine_distance(s.vec, partner.vec))


def draw_pairs(conn: sqlite3.Connection, *, count: int, rng: random.Random, now: float) -> int:
    """Insert up to `count` new pairs (half split, half join; a short pool is
    topped up from the other) and return how many. No unordered event pair is
    ever inserted twice, in any kind. The caller commits."""
    if count <= 0:
        return 0
    sights = _load(conn)
    taken = _existing_keys(conn)
    split_it = _split_pairs(sights, taken, rng)
    join_it = _join_pairs(sights, taken, rng)
    chosen: list[_Pair] = []

    def take(it: Iterator[_Pair], n: int) -> int:
        got = 0
        while got < n:
            p = next(it, None)
            if p is None:
                break
            k = _key(p.a.event_id, p.b.event_id)
            if k in taken:
                continue
            taken.add(k)
            chosen.append(p)
            got += 1
        return got

    want_split = count // 2
    want_join = count - want_split
    got_split = take(split_it, want_split)
    got_join = take(join_it, want_join)
    if got_split < want_split:
        take(join_it, want_split - got_split)
    elif got_join < want_join:
        take(split_it, want_join - got_join)

    rng.shuffle(chosen)
    for p in chosen:
        a, b = (p.a, p.b) if rng.random() < 0.5 else (p.b, p.a)
        same_visit = a.camera == b.camera and abs(a.ts - b.ts) <= VISIT_GAP_S
        conn.execute(
            "INSERT INTO face_pair_labels "
            "(kind, event_a, event_b, cluster_a, cluster_b, distance, same_visit, drawn_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p.kind,
                a.event_id,
                b.event_id,
                a.cluster,
                b.cluster,
                p.distance,
                int(same_visit),
                now,
            ),
        )
    return len(chosen)


def _on_disk(crops_dir: Path | str, rel: Any) -> bool:
    """Same resolution as routes/enrich.py `_crop_response`: inside the crops
    dir and an existing file."""
    if not rel:
        return False
    root = Path(crops_dir).resolve()
    try:
        path = (root / str(rel)).resolve()
    except OSError:
        return False
    return root in path.parents and path.is_file()


def _crop_urls(conn: sqlite3.Connection, crops_dir: Path | str, event_id: str) -> list[str]:
    urls = [
        f"/enrich/face/{event_id}/{int(r['rank'])}.jpg"
        for r in conn.execute(
            "SELECT rank, crop_path FROM face_crops "
            # Only faces that fed the embedding: a stack can also hold whoever
            # else was in frame, and that would muddle the question.
            " WHERE event_id = ? AND crop_path IS NOT NULL AND used = 1 ORDER BY rank",
            (event_id,),
        )
        if _on_disk(crops_dir, r["crop_path"])
    ][:MAX_CROPS]
    if urls:
        return urls
    row = conn.execute(
        "SELECT best_crop_path FROM face_enrichments WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is not None and _on_disk(crops_dir, row["best_crop_path"]):
        return [f"/enrich/face/{event_id}.jpg"]
    return []


def next_pair(
    conn: sqlite3.Connection, crops_dir: Path | str, now: float | None = None
) -> dict[str, Any] | None:
    """The oldest unlabelled pair, in its blind wire form. A pair with a side
    whose crop file is gone is marked 'gone' and skipped. The caller commits."""
    stamp = time.time() if now is None else now
    while True:
        row = conn.execute(
            "SELECT pair_id, event_a, event_b FROM face_pair_labels "
            " WHERE label IS NULL ORDER BY pair_id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        a = _crop_urls(conn, crops_dir, str(row["event_a"]))
        b = _crop_urls(conn, crops_dir, str(row["event_b"]))
        if a and b:
            return {
                "pair_id": int(row["pair_id"]),
                "a": {"crops": a},
                "b": {"crops": b},
            }
        conn.execute(
            "UPDATE face_pair_labels SET label = 'gone', labeled_at = ? WHERE pair_id = ?",
            (stamp, int(row["pair_id"])),
        )


def set_label(conn: sqlite3.Connection, pair_id: int, label: str | None, now: float) -> bool:
    """Record (or, with None, clear) a pair's label. False for an unknown pair.
    The caller commits."""
    if label is not None and label not in LABELS:
        raise ValueError(f"bad label: {label!r}")
    cur = conn.execute(
        "UPDATE face_pair_labels SET label = ?, labeled_at = ? WHERE pair_id = ?",
        (label, None if label is None else now, pair_id),
    )
    return cur.rowcount > 0


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for hits/n; (0.0, 0.0) when n is 0 (callers show
    "no data" for n == 0 rather than this)."""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _rate(hits: int, n: int) -> dict[str, Any]:
    if n <= 0:
        return {"hits": hits, "n": n, "rate": None, "low": None, "high": None}
    low, high = wilson(hits, n)
    return {"hits": hits, "n": n, "rate": hits / n, "low": low, "high": high}


def results(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT kind, cluster_a, cluster_b, event_a, event_b, distance, label "
        "  FROM face_pair_labels WHERE label IS NOT NULL ORDER BY pair_id"
    ).fetchall()
    counts: Counter[tuple[str, str]] = Counter((str(r["kind"]), str(r["label"])) for r in rows)
    gone = sum(1 for r in rows if r["label"] == "gone")
    unsure = sum(1 for r in rows if r["label"] == "unsure")
    labeled = sum(1 for r in rows if r["label"] in LABELS)

    split_same, split_diff = counts[("split", "same")], counts[("split", "different")]
    join_same, join_diff = counts[("join", "same")], counts[("join", "different")]

    bands = [
        {"lo": lo, "hi": hi, "same": 0, "different": 0, "unsure": 0}
        for lo, hi in zip(BAND_EDGES, BAND_EDGES[1:], strict=False)
    ]
    for r in rows:
        if r["label"] not in LABELS:
            continue
        d = float(r["distance"])
        for band in bands:
            if band["lo"] <= d < band["hi"]:
                band[str(r["label"])] += 1
                break

    redirects = {
        int(r["old_id"]): int(r["new_id"])
        for r in conn.execute("SELECT old_id, new_id FROM face_cluster_redirects")
    }
    live = {int(r["cluster_id"]) for r in conn.execute("SELECT cluster_id FROM face_clusters")}

    def resolve(cid: int) -> int | None:
        cid = redirects.get(cid, cid)
        return cid if cid in live else None

    duplicates = []
    wrong_joins = []
    for r in rows:
        d = round(float(r["distance"]), 3)
        if r["kind"] == "split" and r["label"] == "same":
            a, b = resolve(int(r["cluster_a"])), resolve(int(r["cluster_b"]))
            if a is None or b is None or a == b:
                continue
            duplicates.append(
                {
                    "a": a,
                    "a_handle": handle_for(a),
                    "b": b,
                    "b_handle": handle_for(b),
                    "distance": d,
                }
            )
        elif r["kind"] == "join" and r["label"] == "different":
            c = resolve(int(r["cluster_a"]))
            if c is None:
                continue
            wrong_joins.append(
                {
                    "cluster": c,
                    "handle": handle_for(c),
                    "event_ids": [str(r["event_a"]), str(r["event_b"])],
                    "distance": d,
                }
            )
    duplicates.sort(key=lambda x: (x["distance"], x["a"], x["b"]))
    wrong_joins.sort(key=lambda x: (x["distance"], x["cluster"]))

    return {
        "labeled": labeled,
        "unsure": unsure,
        "gone": gone,
        "split": _rate(split_same, split_same + split_diff),
        "join": _rate(join_diff, join_same + join_diff),
        "bands": bands,
        "duplicates": duplicates,
        "wrong_joins": wrong_joins,
    }


def state(conn: sqlite3.Connection, crops_dir: Path | str) -> dict[str, Any]:
    """What the page needs. `results` stays None until every drawn pair is
    labelled: showing running rates would bias the labeller. The caller
    commits (next_pair may mark vanished pairs 'gone')."""
    pair = next_pair(conn, crops_dir)
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(label IS NOT NULL), 0) AS done "
        "  FROM face_pair_labels WHERE label IS NULL OR label != 'gone'"
    ).fetchone()
    total, done = int(row["total"]), int(row["done"])
    return {
        "total": total,
        "done": done,
        "pair": pair,
        "results": results(conn) if pair is None and total > 0 else None,
    }
