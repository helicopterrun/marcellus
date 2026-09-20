"""Tests for `auth.LoginRateLimiter` -- per-IP login attempt limiting.

`/api/login` is proxied (routes/proxy.py), not owned by the sidecar, so the
limiter has to intercept it inside `FrigateAuthMiddleware` before the request
ever reaches the proxy. See auth.py's `LoginRateLimiter` and the `is_login_post`
branch in `FrigateAuthMiddleware.__call__`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app


def _build(
    frigate_db_path: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    handler: Any,
    **sidecar_kw: Any,
) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url="http://frigate.test:8971",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, **sidecar_kw),
        proxy=ProxySection(enabled=True),
    )
    app = create_app(settings)
    # /api/login goes through the proxy catch-all, which uses the separate
    # stream client since the 2026-09-10 pool-separation fix -- pre-seed both
    # so nothing touches the network.
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.stream_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app)


def _login_handler(calls: list[httpx.Request], *, ok: bool) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if ok:
            return httpx.Response(200, json={}, headers={"set-cookie": "frigate_token=abc"})
        return httpx.Response(401, json={"message": "bad credentials"})

    return handler


def test_failed_logins_are_rate_limited(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _login_handler(calls, ok=False),
        login_rate_limit_attempts=3, login_rate_limit_window_s=60,
    )
    for _ in range(3):
        r = client.post("/api/login", json={"user": "x", "password": "y"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"user": "x", "password": "y"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert r.json()["detail"]["error"] == "rate_limited"
    # The 4th attempt never reached "Frigate".
    assert len(calls) == 3


def test_window_expiry_frees_the_bucket(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: Any,
) -> None:
    import time as time_mod

    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _login_handler(calls, ok=False),
        login_rate_limit_attempts=1, login_rate_limit_window_s=5,
    )
    now = time_mod.time()
    monkeypatch.setattr(time_mod, "time", lambda: now)
    assert client.post("/api/login", json={}).status_code == 401
    assert client.post("/api/login", json={}).status_code == 429
    monkeypatch.setattr(time_mod, "time", lambda: now + 6)
    assert client.post("/api/login", json={}).status_code == 401


def test_successful_login_does_not_count(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _login_handler(calls, ok=True),
        login_rate_limit_attempts=2, login_rate_limit_window_s=60,
    )
    for _ in range(5):
        r = client.post("/api/login", json={})
        assert r.status_code == 200
    assert len(calls) == 5


def test_zero_disables_the_limiter(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _login_handler(calls, ok=False),
        login_rate_limit_attempts=0,
    )
    for _ in range(20):
        assert client.post("/api/login", json={}).status_code == 401
    assert len(calls) == 20


def test_rejection_counter_increments(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _login_handler(calls, ok=False),
        login_rate_limit_attempts=1, login_rate_limit_window_s=60,
    )
    app = client.app
    assert getattr(app.state, "login_rate_limit_rejections", 0) == 0
    client.post("/api/login", json={})
    assert client.post("/api/login", json={}).status_code == 429
    assert getattr(app.state, "login_rate_limit_rejections", 0) == 1
    client.post("/api/login", json={})
    assert getattr(app.state, "login_rate_limit_rejections", 0) == 2


def test_401_on_owned_routes_does_not_count(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    """The iOS app fires several requests in parallel whenever its cookie has
    expired; counting each 401 would hit the limit in seconds and then 429
    its own re-login. Guessing a valid session cookie is also infeasible
    (HMAC-signed), so only /api/login itself is worth limiting."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, handler,
        login_rate_limit_attempts=2, login_rate_limit_window_s=60,
    )
    for _ in range(10):
        r = client.get("/", headers={"cookie": "frigate_token=bad"})
        assert r.status_code == 401
    assert len(calls) == 10
    assert getattr(client.app.state, "login_rate_limit_rejections", 0) == 0


def test_owned_route_never_429s_while_login_ip_is_blocked(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/login":
            return httpx.Response(401, json={"message": "bad credentials"})
        return httpx.Response(200, json={"version": "0.16.0"})

    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, handler,
        login_rate_limit_attempts=1, login_rate_limit_window_s=60,
    )
    client.post("/api/login", json={})
    assert client.post("/api/login", json={}).status_code == 429

    # Same client/IP; a valid-cookie request to an owned route is unaffected.
    r = client.get("/", headers={"cookie": "frigate_token=good"})
    assert r.status_code == 200
