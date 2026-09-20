from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from marcellus import db
from marcellus.triage import recorder, sampler


def test_sample_quota_for_n() -> None:
    q = sampler.SampleQuota.for_n(30)
    assert q.decision_zone + q.low_tail + q.high_tail == 30
    assert q.decision_zone == 18  # 60%
    assert q.low_tail == 6  # 20%
    assert q.high_tail == 6  # remainder


def test_score_band() -> None:
    assert sampler.score_band(0.90) == "high"
    assert sampler.score_band(0.85) == "high"
    assert sampler.score_band(0.80) == "midhigh"
    assert sampler.score_band(0.70) == "mid"
    assert sampler.score_band(0.60) == "low"
    assert sampler.score_band(0.50) == "very_low"


def test_sample_returns_events_within_window(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        seed=1,
    )
    # 4 events are within 7 days in the fixture (e5 is 30 days old).
    ids = {ev["id"] for ev in out}
    assert ids == {"e1", "e2", "e3", "e4"}
    # Each event has a snapshot URL anchored at the configured base.
    for ev in out:
        assert ev["snapshot_url"].startswith("http://example.test:5000/api/events/")


def test_sample_skips_already_labeled(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # Pre-label e1 so the sampler should skip it.
    recorder.record(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        event_id="e1",
        label="fp",
    )
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        seed=1,
    )
    assert "e1" not in {ev["id"] for ev in out}


def test_sample_camera_filter(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        camera="alley-overview",
        seed=1,
    )
    assert {ev["camera"] for ev in out} == {"alley-overview"}


class _FlakyConn:
    """Wraps a real sqlite3 connection, raising `database is locked` on the
    first call whose SQL contains `match`, then delegating for real.

    `sqlite3.Connection` is a C type, so its `execute` can't be monkeypatched
    directly -- the module-level `open_joined` name `sampler.py` imports is
    swapped for one returning this wrapper instead.
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


def test_sample_retries_a_transient_lock_then_succeeds(
    frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        sampler,
        "open_joined",
        lambda *a, **kw: _FlakyConn(db.open_joined(*a, **kw), calls, "FROM event e"),
    )
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)  # no real delay in tests

    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        seed=1,
    )
    assert {ev["id"] for ev in out} == {"e1", "e2", "e3", "e4"}
    assert calls["n"] == 2


def test_sample_raises_db_locked_error_when_always_locked(
    frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_locked(*_a: object, **_kw: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sampler, "open_joined", always_locked)
    monkeypatch.setattr(db.time, "sleep", lambda _s: None)

    with pytest.raises(db.DBLockedError):
        sampler.sample(
            frigate_db=frigate_db_path,
            sidecar_db=sidecar_db_path,
            api_base_url="http://example.test:5000",
            days=7,
            n=10,
            seed=1,
        )
