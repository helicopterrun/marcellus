"""Tests for the central Frigate-session gate (marcellus.auth).

The sidecar's own surface -- triage UI, /faces/captures, /analysis, /toybox, /v1 --
exposes event history, face crops and writes with side effects on Frigate, so
it must not be reachable without the same session Frigate itself requires. The
proxy catch-all is gated the same way (tests/test_proxy_auth.py).

Auditing this against a live deployment is not a matter of reading status
codes: an unmatched path falls through to Frigate, which answers almost
anything with `200 text/html` (its SPA shell). `/analysis` pages
both return 200 unauthenticated for exactly that reason and are not leaks. The
question is always whether the *body* is sidecar content, so check for a
sidecar marker rather than concluding from the status alone.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus import auth
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
    # Pre-seed the pooled clients so nothing touches the network. The proxy
    # catch-all (routes/proxy.py) uses the separate stream client, not the
    # API one, since the 2026-09-10 pool-separation fix.
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.stream_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TestClient(app)


@pytest.fixture
def upstream_calls() -> list[httpx.Request]:
    return []


def _ok_handler(calls: list[httpx.Request]) -> Any:
    """Frigate with sign-in on: the authenticated port (8971) refuses a request
    that carries no credential, and accepts any that does."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.port == 8971 and not (
            "cookie" in request.headers or "authorization" in request.headers
        ):
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"version": "0.16.0"})

    return handler


def _denied_handler(calls: list[httpx.Request]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="unauthorized")

    return handler


@pytest.mark.parametrize(
    "path",
    ["/", "/event/e1", "/faces/captures", "/toybox", "/analysis/motion-rate", "/score-histogram"],
)
def test_sidecar_surface_requires_a_session(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request], path: str,
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    r = client.get(path)
    assert r.status_code == 401, path
    assert r.json()["error"] == "unauthorized"
    # No cookie at all: the only upstream traffic is the sign-in-off check
    # (Frigate's config on the sidecar's own origin, then a bare probe of the
    # proxy origin), neither carrying a credential.
    assert [(c.url.port, c.url.path) for c in upstream_calls] == [
        (5000, "/api/config"),
        (8971, "/api/version"),
    ]
    assert all("cookie" not in c.headers for c in upstream_calls)


def test_mutating_endpoints_require_a_session(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    assert client.post("/label", json={"event_id": "e1", "label": "fp"}).status_code == 401
    assert client.post("/clear-label", json={"event_id": "e1"}).status_code == 401
    assert client.post("/faces/captures/scan").status_code == 401


def test_valid_session_passes_and_is_cached(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    for _ in range(3):
        r = client.get("/", headers={"cookie": "frigate_token=abc"})
        assert r.status_code == 200
    # Validated once upstream, then served from the TTL cache. (`/` also runs
    # the cookie-less /healthz Frigate probe for its verdict; not a validation.)
    validations = [c for c in upstream_calls if "cookie" in c.headers]
    assert len(validations) == 1
    assert validations[0].url.path == "/api/version"
    assert validations[0].headers["cookie"] == "frigate_token=abc"


def test_invalid_session_is_rejected(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _denied_handler(upstream_calls))
    r = client.get("/", headers={"cookie": "frigate_token=stale"})
    assert r.status_code == 401
    validations = [c for c in upstream_calls if "cookie" in c.headers]
    assert len(validations) == 1
    # A rejected cookie is never cached, so it gets re-checked next time.
    client.get("/", headers={"cookie": "frigate_token=stale"})
    validations = [c for c in upstream_calls if "cookie" in c.headers]
    assert len(validations) == 2


def test_probe_endpoints_stay_open(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    assert client.get("/healthz").status_code == 200
    assert client.get("/version").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200
    # /healthz probes Frigate through the proxy's stream pool by design
    # (routes/health.py); that is the only upstream traffic these endpoints
    # generate, and it never forwards a client cookie.
    assert [(r.method, r.url.path) for r in upstream_calls] == [("GET", "/api/version")]
    assert "cookie" not in upstream_calls[0].headers


def test_gate_can_be_disabled_for_an_unauthenticated_frigate(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls),
        require_frigate_auth=False,
    )
    assert client.get("/").status_code == 200
    # No session validation upstream. The only call allowed is the single
    # /healthz Frigate probe the status page's verdict makes (cached 10 s).
    assert len(upstream_calls) <= 1
    assert all(c.url.path == "/api/version" for c in upstream_calls)


def test_session_cache_is_bounded() -> None:
    """Frigate rotates its JWT, so an unbounded cache is just a slow leak."""

    class _App:
        class state:  # noqa: N801 - stand-in for FastAPI's app.state
            pass

    app: Any = _App()
    for i in range(50):
        auth._remember(app, f"key{i}", 1e12, max_entries=10)
    assert len(app.state.auth_cache) <= 10


def test_capabilities_always_carries_version(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    """Client contract: Frigate answers /v1/capabilities with its SPA shell on
    both its own ports, so a client can't tell a real capability document from
    the shell by status code. `version` decoding out of the JSON body is what
    distinguishes them -- it must not be dropped or renamed."""
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    body = client.get("/v1/capabilities").json()
    assert isinstance(body.get("version"), str) and body["version"]
    assert "scrub_cache" in body and "proxy" in body


def test_browser_page_request_redirects_to_login(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    r = client.get(
        "/cameras?tab=map",
        headers={"accept": "text/html,application/xhtml+xml"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/login?next=%2Fcameras%3Ftab%3Dmap"


def test_api_clients_keep_the_json_401(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    # No text/html in Accept (the iOS app, curl): JSON as before.
    r = client.get("/v1/push/decisions", headers={"accept": "application/json"})
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    # Same for a browser-shaped POST — only GET navigations redirect.
    r = client.post("/label", json={}, headers={"accept": "text/html"})
    assert r.status_code == 401


def test_login_page_is_exempt_and_renders(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    r = client.get("/login", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "/api/login" in r.text  # the form posts to Frigate via the proxy
    assert not upstream_calls


def test_remember_cookie_with_valid_frigate_session(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    r = client.post("/login/remember", headers={"cookie": "frigate_token=live"})
    assert r.status_code == 204
    token = r.cookies.get(auth.REMEMBER_COOKIE)
    assert token

    cookie = f"{auth.REMEMBER_COOKIE}={token}; frigate_token=live"
    r2 = client.get(
        "/", headers={"cookie": cookie, "accept": "text/html"},
    )
    assert r2.status_code == 200


def test_remember_cookie_redirects_when_frigate_session_expired(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    """When the Frigate JWT expires but the remember cookie is still valid,
    redirect to /login so proxied content (/api/* images) doesn't silently 401.
    """
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(upstream_calls))
    r = client.post("/login/remember", headers={"cookie": "frigate_token=live"})
    assert r.status_code == 204
    token = r.cookies.get(auth.REMEMBER_COOKIE)
    assert token

    calls2: list[httpx.Request] = []
    client2 = _build(frigate_db_path, sidecar_db_path, tmp_path, _denied_handler(calls2))
    r2 = client2.get(
        "/",
        headers={"cookie": f"{auth.REMEMBER_COOKIE}={token}", "accept": "text/html"},
        follow_redirects=False,
    )
    assert r2.status_code == 302
    assert r2.headers["location"].startswith("/login")


def test_remember_cookie_requires_valid_signature(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _denied_handler(upstream_calls))
    r = client.get(
        "/",
        headers={
            "cookie": f"{auth.REMEMBER_COOKIE}=9999999999.deadbeef",
            "accept": "text/html",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302  # forged token falls through to the (failing) gate


def test_remember_endpoint_itself_is_gated(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    upstream_calls: list[httpx.Request],
) -> None:
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _denied_handler(upstream_calls))
    r = client.post("/login/remember")
    assert r.status_code == 401


def test_auth_failure_probes_upstream_exactly_once(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """A rejected request must cost one upstream probe, not two.

    The remember-me path used to validate, swallow the HTTPException, and fall
    through to a second identical validate. Failures are never cached, so that
    was two round-trips to Frigate for every unauthenticated request carrying a
    remember cookie.
    """
    mint_calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(mint_calls))
    token = client.post(
        "/login/remember", headers={"cookie": "frigate_token=live"}
    ).cookies.get(auth.REMEMBER_COOKIE)
    assert token

    denied: list[httpx.Request] = []
    client2 = _build(frigate_db_path, sidecar_db_path, tmp_path, _denied_handler(denied))
    client2.get(
        "/",
        headers={"cookie": f"{auth.REMEMBER_COOKIE}={token}; frigate_token=stale"},
        follow_redirects=False,
    )
    validations = [c for c in denied if "cookie" in c.headers]
    assert len(validations) == 1, f"expected 1 upstream probe, got {len(validations)}"


def test_remember_cookie_grants_the_longer_auth_cache_window(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """A valid remember cookie caches the validated session for
    remember_cache_ttl_s instead of the much shorter auth_cache_ttl_s."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _ok_handler(calls),
        auth_cache_ttl_s=1.0, remember_cache_ttl_s=3600.0,
    )
    token = client.post(
        "/login/remember", headers={"cookie": "frigate_token=live"}
    ).cookies.get(auth.REMEMBER_COOKIE)
    assert token

    app = client.app
    app.state.auth_cache = {}  # drop the entry the mint request just wrote
    before = time.time()
    client.get(
        "/",
        headers={"cookie": f"{auth.REMEMBER_COOKIE}={token}; frigate_token=live"},
    )

    expiry = next(iter(app.state.auth_cache.values()))
    assert expiry - before > 60, "remember cookie did not extend the cache window"

    # Without the remember cookie the same session gets the short window.
    app.state.auth_cache = {}
    before2 = time.time()
    client.get("/", headers={"cookie": "frigate_token=live"})
    expiry2 = next(iter(app.state.auth_cache.values()))
    assert expiry2 - before2 <= 2, "plain session should use auth_cache_ttl_s"
