"""Tests for /healthz's Frigate-proxy probe (routes/health.py).

Covers the 2026-09-12 proxy-stall gating change: the probe now hits the
proxy's own base URL (`frigate.proxy_base_url`) through the same
stream-client pool routes/proxy.py uses, and reports `proxy_stalled` (503,
`reason: proxy_stalled`) on a timeout or pool error instead of the old
direct-to-Frigate check that could stay "ok" while the proxy itself was
wedged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app


class _FakeStreamClient:
    """Stand-in for the app's pooled stream client (see `get_stream_client`).

    Set directly onto `app.state.stream_http_client` -- the conftest
    `_default_frigate_reachable` fixture's fake `get_stream_client` reads
    that attribute when present, the same pattern test_api.py uses.
    """

    is_closed = False
    behavior: str = "ok"
    calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> Any:
        _FakeStreamClient.calls.append(url)
        if _FakeStreamClient.behavior == "timeout":
            raise httpx.ReadTimeout("timed out")
        if _FakeStreamClient.behavior == "pool_timeout":
            raise httpx.PoolTimeout("pool exhausted")
        if _FakeStreamClient.behavior == "unreachable":
            raise httpx.ConnectError("connection refused")

        class _Resp:
            status_code = 200

        return _Resp()

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_fake_stream_client() -> None:
    _FakeStreamClient.behavior = "ok"
    _FakeStreamClient.calls = []


@pytest.fixture(autouse=True)
def _fake_pool_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pool_stats()` reaches into httpx's private transport internals
    (frigate_api.py docstring) -- fake clients here don't have that shape,
    so real `pool_stats()` would silently degrade to `{}` and the
    `upstream_pool`/`api_pool` checks would never appear. Stub it to a fixed
    non-empty dict so those checks are exercised."""
    monkeypatch.setattr(
        "marcellus.routes.health.pool_stats",
        lambda c: {"connections": 1, "active": 0, "idle": 1},
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url="http://frigate.test:8971",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        proxy=ProxySection(enabled=True),
    )
    app = create_app(settings)
    app.state.stream_http_client = _FakeStreamClient()
    # `checks["api_pool"]` only appears once `app.state.http_client` exists --
    # it's created lazily by `get_async_client()` on first use, which a bare
    # `/healthz`-only test never triggers otherwise.
    app.state.http_client = httpx.AsyncClient()
    with TestClient(app) as c:
        yield c


def test_healthz_ok_probes_proxy_base_url(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["frigate"] == "ok"
    # Probed through `frigate.proxy_base_url`, not `frigate.base_url`.
    assert _FakeStreamClient.calls == ["http://frigate.test:8971/api/version"]


def test_healthz_ok_reports_both_pool_stats(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert "upstream_pool" in body["checks"]
    assert "api_pool" in body["checks"]


def test_healthz_degrades_on_stalled_proxy_probe(client: TestClient) -> None:
    _FakeStreamClient.behavior = "timeout"
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["frigate"] == "proxy_stalled"
    assert body["reason"] == "proxy_stalled"
    assert "upstream_pool" in body["checks"]
    assert "api_pool" in body["checks"]


def test_healthz_degrades_on_proxy_pool_timeout(client: TestClient) -> None:
    _FakeStreamClient.behavior = "pool_timeout"
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["checks"]["frigate"] == "proxy_stalled"
    assert body["reason"] == "proxy_stalled"


def test_healthz_probe_result_is_cached(client: TestClient) -> None:
    r1 = client.get("/healthz")
    assert r1.status_code == 200
    _FakeStreamClient.behavior = "timeout"
    # Cached for _FRIGATE_PROBE_INTERVAL_S (10s) -- still "ok" immediately after.
    r2 = client.get("/healthz")
    assert r2.json()["checks"]["frigate"] == "ok"

    # Force the cache to look stale: the next call re-probes and now sees
    # the stalled proxy.
    cache_ts, verdict = client.app.state._frigate_health_cache
    client.app.state._frigate_health_cache = (cache_ts - 3600, verdict)
    r3 = client.get("/healthz")
    assert r3.json()["checks"]["frigate"] == "proxy_stalled"


def test_healthz_recycles_stream_client_after_pool_wedged_30s(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream pool reported saturated (`active == max_connections`, `idle
    == 0`) that stays that way for >=30s must be recycled -- see
    routes/proxy.py's incident note on why a wedged connection can otherwise
    sit ACTIVE forever. A single saturated probe must NOT recycle yet (a
    brief real burst of viewers looks the same for one probe); rather than
    mocking the wall clock (patching the process-global `time.time` would
    also perturb everything else `/healthz` and the app touch), the second
    probe backdates the real first-seen timestamp `/healthz` itself
    recorded, the same way `test_healthz_probe_result_is_cached` above
    backdates the frigate-probe cache."""
    from marcellus import frigate_api

    saturated = {
        "connections": frigate_api._STREAM_LIMITS.max_connections,
        "active": frigate_api._STREAM_LIMITS.max_connections,
        "idle": 0,
    }
    monkeypatch.setattr("marcellus.routes.health.pool_stats", lambda c: saturated)

    old_client = client.app.state.stream_http_client

    r1 = client.get("/healthz")
    assert r1.status_code == 503
    assert client.app.state.stream_http_client is old_client  # not recycled yet
    assert client.app.state._stalled_pool_since is not None

    client.app.state._stalled_pool_since -= 31  # pretend 31s have passed

    r2 = client.get("/healthz")
    assert r2.status_code == 503
    assert client.app.state.stream_http_client is not old_client  # recycled


def test_healthz_resets_stall_timer_on_recovery(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcellus import frigate_api

    saturated = {
        "connections": frigate_api._STREAM_LIMITS.max_connections,
        "active": frigate_api._STREAM_LIMITS.max_connections,
        "idle": 0,
    }
    recovered = {"connections": 1, "active": 0, "idle": 1}
    # `pool_stats` is called twice per /healthz (upstream_pool, api_pool), so
    # each request below needs two matching entries in this sequence.
    stats_sequence = iter([saturated, saturated, recovered, recovered, saturated, saturated])
    monkeypatch.setattr("marcellus.routes.health.pool_stats", lambda c: next(stats_sequence))

    old_client = client.app.state.stream_http_client
    client.get("/healthz")  # saturated -- stall timer starts
    assert client.app.state._stalled_pool_since is not None
    client.app.state._stalled_pool_since -= 31  # pretend 31s have passed
    client.get("/healthz")  # recovered -- stall timer resets despite the age
    assert client.app.state._stalled_pool_since is None

    r3 = client.get("/healthz")  # saturated again, but the timer just reset
    assert r3.status_code == 503
    assert client.app.state.stream_http_client is old_client  # not recycled


def test_pool_stats_is_called_for_both_pools(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    def _spy(c: Any) -> dict[str, int]:
        calls.append(c)
        return {"connections": 1, "active": 0, "idle": 1}

    monkeypatch.setattr("marcellus.routes.health.pool_stats", _spy)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert len(calls) == 2
