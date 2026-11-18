"""The session gate on a Frigate with sign-in turned off (marcellus.auth).

A Frigate with `auth.enabled: false` serves every caller on its authenticated
port without a cookie, and the iOS app saves such a server as "no sign-in" and
sends none. The gate admits a cookie-less request only when Frigate itself says
so: its running config reports `auth.enabled: false` AND a bare request to the
proxy origin is served. Every other combination must keep asking for a session.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus import auth
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app

BASE = "http://frigate.test:5000"
PROXY = "http://frigate.test:8971"


def _build(
    frigate_db_path: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    proxy_base_url: str = PROXY,
) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url=BASE,
            proxy_base_url=proxy_base_url,
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        proxy=ProxySection(enabled=True),
    )
    app = create_app(settings)
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.stream_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app)


def _frigate(
    calls: list[httpx.Request],
    *,
    auth_enabled: Any = False,
    proxy_bare_status: int = 200,
    config_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    """A fake Frigate: port 5000 always open, port 8971 open to a bare request
    only with `proxy_bare_status` 2xx. `auth_enabled=None` omits the section."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.url.port == 5000:
            if path == "/api/config":
                if config_status != 200:
                    return httpx.Response(config_status, text="nope")
                body: dict[str, Any] = {"cameras": {}}
                if auth_enabled is not None:
                    body["auth"] = {"enabled": auth_enabled}
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={})
        if "cookie" not in request.headers and proxy_bare_status >= 300:
            return httpx.Response(proxy_bare_status, text="unauthorized")
        if path == "/api/version":
            return httpx.Response(200, text="0.17.2")
        return httpx.Response(200, json={})

    return handler


def _checks(calls: list[httpx.Request]) -> list[tuple[int | None, str]]:
    """The anonymous-access checks among the upstream calls (not /healthz's
    stream-pool probe, which also hits /api/version on the proxy origin)."""
    return [
        (c.url.port, c.url.path)
        for c in calls
        if (c.url.port, c.url.path) in {(5000, "/api/config"), (8971, "/api/version")}
    ]


def test_signin_off_admits_requests_without_a_cookie(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.get("/v1/stats", headers={"accept": "application/json"})
    assert r.status_code == 200, r.text
    # Both checks ran, neither carried a credential.
    assert _checks(calls) == [(5000, "/api/config"), (8971, "/api/version")]
    assert all("cookie" not in c.headers and "authorization" not in c.headers for c in calls)
    # Cached: more requests do not re-check Frigate within the window.
    for path in ("/v1/stats", "/v1/push/decisions"):
        assert client.get(path, headers={"accept": "application/json"}).status_code == 200
    assert _checks(calls) == [(5000, "/api/config"), (8971, "/api/version")]


def test_signin_off_serves_pages_instead_of_the_login_redirect(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 200


def test_mutating_routes_follow_the_same_decision(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """A write is no different: Frigate serves anonymous writes with sign-in off
    too. What matters is that the decision came from Frigate."""
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    assert client.post("/faces/captures/scan").status_code != 401


@pytest.mark.parametrize("auth_enabled", [True, None, "false", 0])
def test_config_must_say_exactly_false(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, auth_enabled: Any,
) -> None:
    """Sign-in on, a missing `auth` section (pre-auth Frigate, or not a Frigate
    config) or a value that is merely falsy: still a session required, and the
    proxy origin is never probed."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, auth_enabled=auth_enabled)
    )
    r = client.get("/v1/stats", headers={"accept": "application/json"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert _checks(calls) == [(5000, "/api/config")]


def test_proxy_origin_refusing_a_bare_request_keeps_the_gate_shut(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """`auth.enabled: false` but the proxy origin still answers 401 to a caller
    with no credential (Frigate's `proxy.auth_secret`, or a reverse proxy with
    its own login in front): the sidecar follows the origin it fronts."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, proxy_bare_status=401)
    )
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 401
    assert _checks(calls) == [(5000, "/api/config"), (8971, "/api/version")]


def test_proxy_origin_on_the_open_port_does_not_open_a_signin_on_frigate(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """The misconfiguration `proxy_base_url` = port 5000 makes a bare probe pass
    whatever Frigate's setting. Frigate's config saying sign-in is on wins."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, auth_enabled=True),
        proxy_base_url=BASE,
    )
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 401


def test_unreadable_config_keeps_the_gate_shut(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, config_status=401)
    )
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 401
    assert _checks(calls) == [(5000, "/api/config")]


def test_unreachable_frigate_is_a_short_lived_no(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _build(frigate_db_path, sidecar_db_path, tmp_path, handler)
    before = time.time()
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 401
    expiry, allowed = client.app.state.anonymous_access  # type: ignore[attr-defined]
    assert allowed is False
    assert expiry <= before + auth.ANONYMOUS_ERROR_TTL_S + 1


def test_turning_signin_back_on_closes_the_gate_after_the_window(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    state = {"auth_enabled": False}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return _frigate(calls, auth_enabled=state["auth_enabled"])(request)

    client = _build(frigate_db_path, sidecar_db_path, tmp_path, handler)
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 200
    state["auth_enabled"] = True
    # Within the window the cached answer stands...
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 200
    # ...and once it lapses Frigate is asked again and the gate shuts.
    expiry, allowed = client.app.state.anonymous_access  # type: ignore[attr-defined]
    client.app.state.anonymous_access = (time.time() - 1, allowed)  # type: ignore[attr-defined]
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 401


def test_checks_never_carry_the_pooled_clients_cookies(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """A cookie left in the shared client's jar must not turn the bare probe
    into an authenticated one."""
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    client.app.state.http_client.cookies.set("frigate_token", "stale")  # type: ignore[attr-defined]
    assert client.get("/v1/stats", headers={"accept": "application/json"}).status_code == 200
    assert all("cookie" not in c.headers for c in calls)


def test_a_cookie_is_still_validated_as_a_session(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """Sign-in on: a request that does carry a cookie takes the usual path, and
    the sign-in-off check is not consulted at all."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, auth_enabled=True)
    )
    r = client.get(
        "/v1/stats", headers={"accept": "application/json", "cookie": "frigate_token=abc"}
    )
    assert r.status_code == 200
    assert not hasattr(client.app.state, "anonymous_access")  # type: ignore[attr-defined]
    assert (5000, "/api/config") not in _checks(calls)
