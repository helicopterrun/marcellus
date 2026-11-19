"""The proxy catch-all forwards only the caller's own Frigate credential.

Two faults, fixed together:

* The pooled Frigate clients kept httpx's default cookie jar. A proxied
  `POST /api/login` stored its `Set-Cookie: frigate_token=...` there, and
  every later proxied request that carried no `Cookie` header of its own was
  sent with that token: any caller acted as the last user who signed in.
* The catch-all was not gated, so nothing on the sidecar refused a caller with
  no credential; and with `frigate.proxy_base_url` on Frigate's
  unauthenticated port nothing upstream would have either.

The fake Frigate here has sign-in on unless a test says otherwise: its
authenticated port (8971) answers only a request carrying a known session,
as a cookie or a bearer token. The pooled clients are built by the real
`frigate_tls.async_client` so the cookie-jar behaviour under test is the
shipped one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from marcellus import frigate_api, frigate_tls
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.routes import proxy as proxy_module
from marcellus.server import create_app

BASE = "http://frigate.test:5000"
PROXY = "http://frigate.test:8971"
SESSIONS = {"token-a": "alice", "token-b": "bob"}


def _session_of(request: httpx.Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        return SESSIONS.get(auth_header[len("Bearer ") :])
    for part in request.headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "frigate_token" and value in SESSIONS:
            return SESSIONS[value]
    return None


def _frigate(
    calls: list[httpx.Request], *, signin_on: bool = True
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        path = request.url.path
        if request.url.port == 5000:
            if path == "/api/config":
                return httpx.Response(200, json={"auth": {"enabled": signin_on}})
            return httpx.Response(200, json={})
        if path == "/api/login" and request.method == "POST":
            user = request.read().decode()
            token = "token-a" if "alice" in user else "token-b"
            return httpx.Response(
                200, json={}, headers={"set-cookie": f"frigate_token={token}; Path=/; HttpOnly"}
            )
        user = _session_of(request)
        if signin_on and user is None:
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, json={"path": path, "user": user})

    return handler


def _build(
    frigate_db_path: Path,
    sidecar_db_path: Path,
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    proxy_base_url: str = PROXY,
    require_frigate_auth: bool = True,
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
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=require_frigate_auth
        ),
        proxy=ProxySection(enabled=True),
    )
    app = create_app(settings)
    transport = httpx.MockTransport(handler)
    for attr in ("http_client", "stream_http_client", "vod_stream_http_client"):
        setattr(app.state, attr, frigate_tls.async_client(verify_tls=True, transport=transport))
    return TestClient(app)


def _forwarded(calls: list[httpx.Request]) -> list[httpx.Request]:
    """Upstream requests other than the gate's own credential-less checks
    (Frigate's config on the API port, the bare `/api/version` probe) and its
    session checks (`/api/version` with a credential)."""
    return [c for c in calls if c.url.path not in ("/api/config", "/api/version")]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/version"),
        ("GET", "/api/config"),
        ("GET", "/api/events?limit=1"),
        ("GET", "/api/doorbell/latest.jpg"),
        ("GET", "/vod/doorbell/start/1000/end/1060/index.m3u8"),
        ("GET", "/api/exports"),
        ("POST", "/api/export/doorbell/start/1000/end/1060"),
        ("DELETE", "/api/events/e1"),
    ],
)
def test_caller_without_a_credential_is_refused_before_forwarding(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, method: str, path: str,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.request(method, path)
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"
    assert _forwarded(calls) == []
    assert all("cookie" not in c.headers and "authorization" not in c.headers for c in calls)


@pytest.mark.parametrize("path", ["/ws", "/live/webrtc/api/ws?src=doorbell"])
def test_websocket_without_a_credential_is_refused_before_connecting(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    connects: list[str] = []

    def _connector(**_: Any) -> Any:
        async def _connect(url: str, headers: Any, subs: Any) -> Any:
            connects.append(url)
            raise OSError("not reached in this test")

        return _connect

    monkeypatch.setattr(proxy_module, "_ws_connector", _connector)
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(path):
        pass
    assert connects == []


def test_websocket_with_a_valid_session_reaches_the_relay(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connects: list[tuple[str, Any]] = []

    def _connector(**_: Any) -> Any:
        async def _connect(url: str, headers: Any, subs: Any) -> Any:
            connects.append((url, headers))
            raise OSError("upstream refused")

        return _connect

    monkeypatch.setattr(proxy_module, "_ws_connector", _connector)
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws", headers={"cookie": "frigate_token=token-a"}),
    ):
        pass
    assert len(connects) == 1
    assert ("cookie", "frigate_token=token-a") in connects[0][1]


def test_one_callers_login_is_never_sent_on_another_callers_request(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))

    # Caller A signs in through the proxy and gets its own cookie back.
    r = client.post("/api/login", content=b'{"user": "alice"}')
    assert r.status_code == 200
    assert "frigate_token=token-a" in r.headers["set-cookie"]
    # Nothing of A's is kept on the sidecar's side.
    for attr in ("http_client", "stream_http_client", "vod_stream_http_client"):
        assert len(getattr(client.app.state, attr).cookies.jar) == 0
    client.cookies.clear()  # what follows is another caller, without A's cookie

    # Caller B with no credential: refused, never forwarded as A.
    before = len(calls)
    r = client.get("/api/events?limit=1")
    assert r.status_code == 401
    assert all("token-a" not in c.headers.get("cookie", "") for c in calls[before:])

    # Caller B with its own session: forwarded with B's cookie and nothing else.
    r = client.get("/api/events?limit=1", headers={"cookie": "frigate_token=token-b"})
    assert r.status_code == 200
    assert r.json()["user"] == "bob"
    forwarded = [c for c in _forwarded(calls) if c.url.path == "/api/events"]
    assert [c.headers.get("cookie") for c in forwarded] == ["frigate_token=token-b"]


def test_proxy_client_itself_never_replays_a_login_cookie(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """The root cause on its own, with the gate turned off: after a proxied
    login, a request with no cookie goes upstream with no cookie."""
    calls: list[httpx.Request] = []
    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls), require_frigate_auth=False
    )
    assert client.post("/api/login", content=b'{"user": "alice"}').status_code == 200
    client.cookies.clear()
    for path in ("/api/events?limit=1", "/vod/doorbell/start/1/end/2/index.m3u8"):
        r = client.get(path)
        assert r.status_code == 401  # Frigate's own answer: no session
        assert "cookie" not in calls[-1].headers


def test_shared_frigate_clients_keep_no_cookie_jar() -> None:
    class _App:
        class state:  # noqa: N801 - stand-in for FastAPI's app.state
            settings = None

    app: Any = _App()
    app.state.settings = Settings(frigate=FrigateSection(base_url=BASE, proxy_base_url=PROXY))
    for client in (
        frigate_api.get_async_client(app),
        frigate_api.get_stream_client(app),
        frigate_api.get_vod_stream_client(app),
    ):
        response = httpx.Response(
            200,
            headers={"set-cookie": "frigate_token=leaked; Path=/"},
            request=httpx.Request("GET", f"{PROXY}/api/login"),
        )
        client.cookies.extract_cookies(response)
        assert len(client.cookies.jar) == 0
        request = client.build_request("GET", f"{PROXY}/api/events")
        assert "cookie" not in request.headers


def test_bearer_token_is_accepted_and_forwarded(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.get("/api/events?limit=1", headers={"authorization": "Bearer token-a"})
    assert r.status_code == 200
    assert r.json()["user"] == "alice"
    assert _forwarded(calls)[-1].headers["authorization"] == "Bearer token-a"
    # The sidecar's own routes take it too.
    assert client.get("/v1/stats", headers={"authorization": "Bearer token-a"}).status_code == 200
    # An unknown bearer token is refused before forwarding.
    before = len(_forwarded(calls))
    r = client.get("/api/events?limit=1", headers={"authorization": "Bearer forged"})
    assert r.status_code == 401
    assert len(_forwarded(calls)) == before


def test_invalid_cookie_is_refused_before_forwarding(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.get("/api/events?limit=1", headers={"cookie": "frigate_token=forged"})
    assert r.status_code == 401
    assert _forwarded(calls) == []


def test_signin_off_frigate_is_still_proxied_without_a_credential(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls, signin_on=False))
    for path in ("/api/events?limit=1", "/api/doorbell/latest.jpg", "/vod/d/index.m3u8"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert all("cookie" not in c.headers for c in calls)


def test_unprotected_proxy_origin_fails_closed(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    """`proxy_base_url` on Frigate's unauthenticated port while Frigate has
    sign-in on: that origin would accept any made-up cookie, so nothing is
    validated against it or forwarded to it."""
    calls: list[httpx.Request] = []

    def open_port(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/config":
            return httpx.Response(200, json={"auth": {"enabled": True}})
        return httpx.Response(200, json={"path": request.url.path})

    client = _build(
        frigate_db_path, sidecar_db_path, tmp_path, open_port, proxy_base_url=BASE
    )
    r = client.get("/api/events?limit=1", headers={"cookie": "frigate_token=made-up"})
    assert r.status_code == 502
    assert r.json()["error"] == "proxy_origin_unprotected"
    assert client.get("/api/events?limit=1").status_code == 401
    assert client.get("/v1/stats", headers={"cookie": "frigate_token=x"}).status_code == 502
    assert _forwarded(calls) == []


def test_login_post_is_still_forwarded_without_a_credential(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []
    client = _build(frigate_db_path, sidecar_db_path, tmp_path, _frigate(calls))
    r = client.post("/api/login", content=b'{"user": "bob"}')
    assert r.status_code == 200
    assert [c.url.path for c in calls] == ["/api/login"]
    assert "cookie" not in calls[0].headers
