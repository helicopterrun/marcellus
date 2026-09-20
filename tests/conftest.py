"""Pytest fixtures shared across the sidecar test suite."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

# Approximation of Frigate's `event` table — enough columns for our queries.
FRIGATE_EVENT_SCHEMA = """
CREATE TABLE event (
    id           TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    sub_label    TEXT,
    start_time   REAL NOT NULL,
    end_time     REAL,
    has_clip     INTEGER NOT NULL DEFAULT 0,
    has_snapshot INTEGER NOT NULL DEFAULT 0,
    score        REAL,
    top_score    REAL,
    area         REAL,
    ratio        REAL,
    zones        TEXT,
    data         TEXT,
    false_positive INTEGER NOT NULL DEFAULT 0,
    plus_id      TEXT
);
"""


@pytest.fixture
def frigate_db_path(tmp_path: Path) -> Path:
    """A minimal stand-in for Frigate's DB with a handful of seeded events."""
    p = tmp_path / "frigate.db"
    conn = sqlite3.connect(p)
    conn.executescript(FRIGATE_EVENT_SCHEMA)
    now = time.time()

    rows = [
        # zones value matches Frigate's storage: a JSON-encoded list.
        # id, camera, label, dt-seconds, score, zones, top_score, has_clip, has_snapshot
        ("e1", "alley-overview", "person", -300, 0.92, ["parking_area_people"], 0.94, 1, 1),
        ("e2", "alley-overview", "person", -600, 0.61, ["alley"], 0.65, 1, 1),
        ("e3", "alley-east", "dog", -900, 0.78, ["back_walkway"], 0.80, 1, 1),
        ("e4", "street-overview", "car", -1200, 0.55, ["49th_street"], 0.58, 0, 1),
        ("e5", "alley-overview", "person", -86400 * 30, 0.72, ["alley"], 0.75, 1, 1),  # old
    ]
    for eid, cam, label, dt, score, zones_list, top, has_clip, has_snap in rows:
        data = json.dumps(
            {"score": score, "top_score": top, "box": [0.1, 0.2, 0.3, 0.4], "type": "object"}
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, score, top_score, "
            "area, ratio, zones, data, has_clip, has_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid, cam, label, now + dt, now + dt + 30, score, top,
                5000.0, 1.5, json.dumps(zones_list), data, has_clip, has_snap,
            ),
        )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def sidecar_db_path(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path / "marcellus.db"


@pytest.fixture(autouse=True)
def _reset_ladder_policy() -> Iterator[None]:
    """Elsinore Phase 4 (`push/policy_settings.py`) made `ladder_policy.TABLE`
    and `ladder_policy.ZONE_OVERRIDES` mutable, process-wide globals so the
    routing engine can pick up a user-edited table/override without
    restarting -- `ladder.py` reads both as bare module attributes,
    unchanged, per that phase's design. That mutability has to stop at the
    test boundary: without this, a test that calls
    `policy_settings.apply_settings`/`ladder_policy.set_table` would leak
    its table into every test that runs afterward in the same process,
    including `test_push_ladder.py`'s own golden-fixture suite. Snapshotting
    and restoring the actual attributes (not a fixed constant) means this
    works regardless of what any given test starts from or which other
    tests already mutated it this session.
    """
    from marcellus.push import ladder_policy, policy_settings

    original_table = {subject: dict(row) for subject, row in ladder_policy.TABLE.items()}
    original_overrides = {
        zone: dict(row) for zone, row in ladder_policy.ZONE_OVERRIDES.items()
    }
    original_off_cells = set(ladder_policy.OFF_CELLS)
    yield
    ladder_policy.set_table(original_table)
    ladder_policy.set_zone_overrides(original_overrides)
    ladder_policy.set_off_cells(original_off_cells)
    policy_settings.reset_for_tests()


@pytest.fixture(autouse=True)
def _default_frigate_reachable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default `/healthz`'s frigate probe to reachable ("ok").

    `_probe_frigate` (routes/health.py) is async and goes through
    `get_stream_client(app)` (the same pool `routes/proxy.py` uses against
    `settings.frigate.proxy_base_url`). Without this default, a test that
    hits `/healthz` incidentally would make a real connect attempt to the
    fixture's fake `frigate.test:*` host and see "unreachable" every time
    (informational now, but still noise, and slow).

    Patching `httpx.AsyncClient` process-wide would be wrong here -- plenty
    of *other* tests (test_frigate_api, test_push_mqtt, test_auth, ...)
    construct their own real `httpx.AsyncClient` with a mock transport and
    expect its `.get`/request methods to actually run. So this replaces only
    `get_stream_client` as imported into `routes.health`'s own module
    namespace with a stand-in that always answers 200. Tests that set a
    custom fake/real `app.state.stream_http_client` and expect
    `get_stream_client` to return it should instead monkeypatch this same
    name, or set `app.state.stream_http_client` *and* monkeypatch
    `marcellus.routes.health.get_stream_client` to read it -- see
    test_api.py / test_health.py for the pattern.
    """

    class _FakeResponse:
        status_code = 200

    class _FakeAsyncClient:
        async def get(self, url: str, *args: object, **kwargs: object) -> _FakeResponse:
            return _FakeResponse()

    _fake_client = _FakeAsyncClient()

    def _fake_get_stream_client(app: object) -> _FakeAsyncClient:
        stream_client = getattr(getattr(app, "state", None), "stream_http_client", None)
        return stream_client if stream_client is not None else _fake_client

    monkeypatch.setattr(
        "marcellus.routes.health.get_stream_client", _fake_get_stream_client
    )
    yield
