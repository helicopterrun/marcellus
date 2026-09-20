from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from marcellus import db
from marcellus.analysis import (
    motion_active,
    motion_rate,
    pull_events,
    score_histogram,
    zone_hits,
)
from marcellus.triage import recorder


def test_motion_active_aggregate_bounds_both_ends() -> None:
    def _hour(motion: int, events: int) -> dict[str, int]:
        return {"duration": 3600, "motion": motion, "events": events, "objects": events}

    days_data = [
        {"day": "2026-08-01", "hours": [_hour(10, 1)]},
        {"day": "2026-08-02", "hours": [_hour(20, 2)]},
        {"day": "2026-08-03", "hours": [_hour(40, 4)]},
    ]
    agg = motion_active._aggregate(days_data, "2026-08-02", "2026-08-02")
    assert agg["motion"] == 20
    assert agg["hours_with_data"] == 1


def test_pull_events_emits_within_window(frigate_db_path: Path) -> None:
    rows = list(pull_events.pull(frigate_db=frigate_db_path, days=7))
    ids = {r["id"] for r in rows}
    assert ids == {"e1", "e2", "e3", "e4"}
    # e5 is 30 days old; absent from the 7-day window.
    assert "e5" not in ids


def test_pull_events_camera_filter(frigate_db_path: Path) -> None:
    rows = list(pull_events.pull(frigate_db=frigate_db_path, days=7, camera="alley-overview"))
    assert {r["camera"] for r in rows} == {"alley-overview"}


def test_motion_rate_smoke(frigate_db_path: Path) -> None:
    rows = motion_rate.analyze(frigate_db=frigate_db_path, days=7)
    # Each fixture camera that has events in the window appears.
    cams = {r["camera"] for r in rows}
    assert "alley-overview" in cams
    for r in rows:
        assert "suggestion" in r
        assert r["events_total"] >= 1


def test_score_histogram_sparse_then_with_triage(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # No triage labels; fixture has <10 events per (camera,label) -> sparse rows.
    result = score_histogram.analyze(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        days=7,
    )
    assert all(r["confidence"] == "sparse" for r in result["rows"])


def test_zone_hits_groups_by_zone(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    result = zone_hits.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7
    )
    zones = {r["zone"] for r in result["hits"]}
    # Fixture seeded events span parking_area_people, alley, back_walkway, 49th_street.
    assert "alley" in zones
    assert "49th_street" in zones


def test_zone_hits_mask_candidates_empty_without_clusters(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    result = zone_hits.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7
    )
    # Fewer than 5 events per (cam,label) cluster -> no mask candidates.
    assert result["mask_candidates"] == []


class _FlakyConn:
    """Wraps a real sqlite3 connection, raising `database is locked` on the
    first calls whose SQL contains `match`, then delegating for real.

    `sqlite3.Connection` is a C type -- its `execute` can't be monkeypatched
    directly (`cannot set 'execute' attribute of immutable type`), so the
    module-level `open_frigate_ro`/`open_joined` name each analysis module
    imports is swapped for one returning this wrapper instead.
    """

    def __init__(self, real: sqlite3.Connection, calls: dict[str, int], match: str) -> None:
        self._real = real
        self._calls = calls
        self._match = match

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        if self._match in sql:
            self._calls["n"] += 1
            if self._calls["n"] < 2:
                raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)  # type: ignore[arg-type]

    def close(self) -> None:
        self._real.close()


def test_pull_events_retries_a_transient_lock_then_succeeds(
    frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        pull_events,
        "open_frigate_ro",
        lambda path: _FlakyConn(db.open_frigate_ro(path), calls, "FROM event WHERE"),
    )
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)  # no real delay in tests

    rows = list(pull_events.pull(frigate_db=frigate_db_path, days=7))
    assert {r["id"] for r in rows} == {"e1", "e2", "e3", "e4"}
    assert calls["n"] == 2


def test_pull_events_raises_db_locked_error_when_always_locked(
    frigate_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_locked(_path: Path) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(pull_events, "open_frigate_ro", always_locked)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    with pytest.raises(db.DBLockedError):
        list(pull_events.pull(frigate_db=frigate_db_path, days=7))


def test_zone_hits_retries_a_transient_lock_then_succeeds(
    frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        zone_hits,
        "open_joined",
        lambda *a, **kw: _FlakyConn(db.open_joined(*a, **kw), calls, "FROM event e"),
    )
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    result = zone_hits.analyze(frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7)
    assert result["hits"]
    assert calls["n"] == 2


def test_score_histogram_uses_triage_subset(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # Label e1 as tp. With <10 tp samples, branch still falls back to whole-set.
    recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="tp",
    )
    result = score_histogram.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7,
    )
    # alley-overview/person should report n_tp >= 1.
    rows = [r for r in result["rows"] if r["camera"] == "alley-overview" and r["label"] == "person"]
    assert rows
    assert rows[0]["n_tp"] >= 1
