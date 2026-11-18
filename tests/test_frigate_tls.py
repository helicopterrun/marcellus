"""`frigate.verify_tls` and the certificate/scheme diagnosis (`frigate_tls.py`).

Frigate serves its signed-in port over HTTPS with a self-signed certificate by
default. Before `frigate.verify_tls` the sidecar could not talk to a stock
Frigate at all and said nothing about why. These tests pin that:

- every Frigate-bound client gets the setting (and no module builds an httpx
  client of its own outside `frigate_tls.py` except the known non-Frigate ones);
- failures classify into `tls_untrusted` / `https_required`, while ordinary
  failures keep their usual handling;
- the warning is logged once per change, not per request;
- `/healthz` and the login page expose the diagnosis.
"""

from __future__ import annotations

import ast
import logging
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from marcellus import frigate_tls
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app

SRC = Path(__file__).resolve().parents[1] / "src" / "marcellus"

# Modules allowed to build httpx clients directly because they never talk to
# Frigate: each has its own endpoint and, where TLS matters, its own setting.
NON_FRIGATE_CLIENT_MODULES = {
    "push/transport.py": "push relay / APNs",
    "push/unifi_protect.py": "UniFi Protect console (unifi_protect.verify_tls)",
    "external/media.py": "external sources (their own base_url)",
}
_HTTPX_CONSTRUCTORS = {
    "Client", "AsyncClient", "get", "post", "put", "patch", "delete", "head",
    "options", "request", "stream",
}
_FACTORY_CALLS = {"FrigateClient", "async_client", "sync_client"}

NGINX_400 = (
    "<html>\r\n<head><title>400 The plain HTTP request was sent to HTTPS port</title>"
    "</head>\r\n<body>\r\n<center><h1>400 Bad Request</h1></center>\r\n"
    "<center>The plain HTTP request was sent to HTTPS port</center>\r\n"
    "<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


def _self_signed_error(url: str = "https://192.168.1.10:8971/api/version") -> httpx.ConnectError:
    """The shape httpx raises for a self-signed certificate (measured)."""
    cause = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    err = httpx.ConnectError(str(cause), request=httpx.Request("GET", url))
    err.__cause__ = cause
    return err


# -- every Frigate-bound client gets the setting ---------------------------------


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def test_no_module_builds_an_httpx_client_outside_frigate_tls() -> None:
    """A Frigate-bound client built straight from httpx would silently ignore
    `frigate.verify_tls`. Only `frigate_tls.py` and the allowlisted
    non-Frigate modules may construct one; everything else goes through
    `frigate_tls.async_client` / `sync_client` / `get` (or `FrigateClient`),
    which take the setting as a required keyword."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel == "frigate_tls.py" or rel in NON_FRIGATE_CLIENT_MODULES:
            continue
        for call in _calls(ast.parse(path.read_text(), filename=rel)):
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "httpx"
                and func.attr in _HTTPX_CONSTRUCTORS
            ):
                offenders.append(f"{rel}:{call.lineno} httpx.{func.attr}(...)")
            if isinstance(func, ast.Name) and func.id in {"AsyncClient", "Client"}:
                offenders.append(f"{rel}:{call.lineno} {func.id}(...)")
    assert not offenders, (
        "httpx clients built outside frigate_tls.py (use frigate_tls.async_client/"
        f"sync_client/get with verify_tls=..., or allowlist a non-Frigate module): {offenders}"
    )


def test_every_frigate_client_factory_call_passes_verify_tls() -> None:
    missing: list[str] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for call in _calls(ast.parse(path.read_text(), filename=rel)):
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in _FACTORY_CALLS:
                continue
            if name in {"async_client", "sync_client"} and not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "frigate_tls"
            ):
                continue
            if rel == "frigate_tls.py":
                continue
            if not any(kw.arg == "verify_tls" for kw in call.keywords):
                missing.append(f"{rel}:{call.lineno} {name}(...)")
    assert not missing, f"Frigate client built without verify_tls=: {missing}"


def test_server_wires_verify_tls_into_push_engine_and_doorbell() -> None:
    """PushEngine and doorbell.handle_ring default to strict for tests; the
    server must hand them the configured value."""
    tree = ast.parse((SRC / "server.py").read_text())
    seen: dict[str, bool] = {}
    for call in _calls(tree):
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in {"PushEngine", "handle_ring"}:
            seen[name] = any(
                kw.arg == "frigate_verify_tls"
                and ast.unparse(kw.value) == "settings.frigate.verify_tls"
                for kw in call.keywords
            )
    assert seen == {"PushEngine": True, "handle_ring": True}


class _SpyClient:
    """Records the `verify` each constructed client was given."""

    made: list[Any] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _SpyClient.made.append(kwargs.get("verify", "<unset>"))
        self.is_closed = False

    async def __aenter__(self) -> _SpyClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __enter__(self) -> _SpyClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get(self, url: str, **_: Any) -> httpx.Response:
        raise httpx.ConnectError("spy: no network", request=httpx.Request("GET", url))


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    _SpyClient.made = []
    monkeypatch.setattr(httpx, "AsyncClient", _SpyClient)
    monkeypatch.setattr(httpx, "Client", _SpyClient)
    return _SpyClient.made


def _settings(
    tmp_path: Path, *, verify_tls: bool, proxy: str = "https://frigate.test:8971"
) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url=proxy,
            verify_tls=verify_tls,
            config_path=fake_config,
            db_path=tmp_path / "frigate.db",
        ),
        sidecar=SidecarSection(
            db_path=tmp_path / "marcellus.db", bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=True),
    )


@pytest.mark.parametrize("verify_tls", [True, False])
def test_app_pools_get_the_setting(tmp_path: Path, spy: list[Any], verify_tls: bool) -> None:
    from marcellus import frigate_api

    app = create_app(_settings(tmp_path, verify_tls=verify_tls))
    frigate_api.get_async_client(app)
    frigate_api.get_stream_client(app)
    frigate_api.get_vod_stream_client(app)
    assert spy == [verify_tls] * 3


@pytest.mark.parametrize("verify_tls", [True, False])
def test_frigate_client_gets_the_setting_for_both_its_clients(
    spy: list[Any], verify_tls: bool
) -> None:
    from marcellus.frigate_api import FrigateClient

    FrigateClient("https://frigate.test:5000", verify_tls=verify_tls)
    assert spy == [verify_tls, verify_tls]


async def test_background_clients_get_the_setting(tmp_path: Path, spy: list[Any]) -> None:
    from marcellus.push import mqtt
    from marcellus.push.engine import PushEngine
    from marcellus.recording_watch import RecordingWatcher
    from marcellus.routes import status

    settings = _settings(tmp_path, verify_tls=False)
    await status._probe_frigate(settings.frigate.base_url, verify_tls=False)
    engine = PushEngine(
        db_path=str(tmp_path / "m.db"), transport=None,  # type: ignore[arg-type]
        server_id="s", frigate_base_url="http://frigate.test:5000", frigate_verify_tls=False,
    )
    engine._client()
    await mqtt.backfill_since(engine, frigate_base_url="http://frigate.test:5000", after=0.0)
    watcher = RecordingWatcher.__new__(RecordingWatcher)
    watcher.settings = settings
    await watcher._fetch_runtime()
    assert spy == [False] * 4


def test_watchdog_probe_gets_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from marcellus import watchdog

    seen: list[object] = []

    def fake_get(url: str, **kwargs: Any) -> httpx.Response:
        seen.append(kwargs.get("verify"))
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert watchdog._classify("https://frigate.test:5000/api/version", 2.0, verify_tls=False)[0]
    assert seen == [False]


def test_watchdog_does_not_count_a_certificate_failure_as_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting Frigate never fixes a sidecar config mistake."""
    from marcellus import watchdog

    def fake_get(url: str, **_: Any) -> httpx.Response:
        raise _self_signed_error(url)

    monkeypatch.setattr(httpx, "get", fake_get)
    outcome, detail = watchdog._classify(
        "https://frigate.test:5000/api/version", 2.0, verify_tls=True
    )
    assert outcome == "other"
    assert "frigate.verify_tls: false" in detail


def test_ws_ssl_context() -> None:
    assert frigate_tls.ws_ssl_context("ws://192.168.1.10:8971/ws", verify_tls=False) is None
    assert frigate_tls.ws_ssl_context("wss://192.168.1.10:8971/ws", verify_tls=True) is None
    ctx = frigate_tls.ws_ssl_context("wss://192.168.1.10:8971/ws", verify_tls=False)
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


async def test_ws_proxy_connector_passes_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    import types

    from marcellus.routes import proxy

    captured: dict[str, Any] = {}

    async def fake_connect(url: str, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "connected"

    fake_module = types.SimpleNamespace(connect=fake_connect)
    real_import = importlib.import_module
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name, *a: fake_module if name.startswith("websockets") else real_import(name, *a),
    )
    strict = proxy._ws_connector(verify_tls=True)
    await strict("wss://frigate.test:8971/ws", [], [])
    assert "ssl" not in captured
    loose = proxy._ws_connector(verify_tls=False)
    await loose("wss://frigate.test:8971/ws", [], [])
    assert captured["ssl"].verify_mode == ssl.CERT_NONE


def test_verify_tls_defaults_strict_and_reads_from_yaml_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FrigateSection().verify_tls is True
    assert Settings.model_validate({"frigate": {"verify_tls": False}}).frigate.verify_tls is False
    monkeypatch.setenv("MARCELLUS_FRIGATE__VERIFY_TLS", "false")
    assert Settings().frigate.verify_tls is False


# -- classification ----------------------------------------------------------------


def test_self_signed_certificate_is_tls_untrusted() -> None:
    err = _self_signed_error()
    assert frigate_tls.classify_error(err, "https://192.168.1.10:8971/api/version") == (
        frigate_tls.TLS_UNTRUSTED
    )


def test_certificate_error_named_only_in_the_message_is_tls_untrusted() -> None:
    err = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    url = "wss://frigate.local:8971/ws"
    assert frigate_tls.classify_error(err, url) == frigate_tls.TLS_UNTRUSTED


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("[Errno 61] Connection refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.ReadError("[Errno 54] Connection reset by peer"),
    ],
)
def test_ordinary_failures_are_not_diagnosed(exc: Exception) -> None:
    for url in ("http://192.168.1.10:8971/api/version", "https://192.168.1.10:8971/api/version"):
        assert frigate_tls.classify_error(exc, url) is None


def test_nginx_plain_http_page_is_https_required() -> None:
    url = "http://192.168.1.10:8971/api/version"
    assert frigate_tls.classify_response(400, NGINX_400, url) == frigate_tls.HTTPS_REQUIRED
    assert frigate_tls.classify_response(400, NGINX_400.encode(), url) == frigate_tls.HTTPS_REQUIRED
    assert frigate_tls.classify_response(497, NGINX_400, url) == frigate_tls.HTTPS_REQUIRED


def test_other_answers_are_not_https_required() -> None:
    url = "http://192.168.1.10:8971/api/version"
    assert frigate_tls.classify_response(400, "bad request", url) is None
    assert frigate_tls.classify_response(401, NGINX_400, url) is None
    assert frigate_tls.classify_response(200, "0.18.2", url) is None
    # Already https: the page cannot be about this URL's scheme.
    assert frigate_tls.classify_response(400, NGINX_400, "https://frigate.local:8971/") is None


def test_redirect_to_https_on_the_same_port_is_https_required() -> None:
    url = "http://frigate.local:8971/api/version"
    assert (
        frigate_tls.classify_response(301, "", url, "https://frigate.local:8971/api/version")
        == frigate_tls.HTTPS_REQUIRED
    )
    assert frigate_tls.classify_response(302, "", url, "https://elsewhere.local/login") is None
    assert frigate_tls.classify_response(302, "", url, "/login") is None


def test_websocket_handshake_rejection_with_the_nginx_page_is_https_required() -> None:
    class _Resp:
        status_code = 400
        headers: dict[str, str] = {}
        body = NGINX_400.encode()

    class InvalidStatus(Exception):
        response = _Resp()

    assert (
        frigate_tls.classify_error(InvalidStatus("rejected"), "ws://frigate.local:8971/ws")
        == frigate_tls.HTTPS_REQUIRED
    )


def test_tls_alert_read_as_a_status_line_is_https_required() -> None:
    err = httpx.RemoteProtocolError(
        "illegal status line: bytearray(b'\\x15\\x03\\x01\\x00\\x02\\x02P')"
    )
    assert (
        frigate_tls.classify_error(err, "http://frigate.local:8971/api/version")
        == frigate_tls.HTTPS_REQUIRED
    )


# -- once per window logging ---------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "marcellus.frigate_tls" and r.levelno == logging.WARNING
    ]


def test_warning_is_logged_once_per_change_not_per_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="marcellus.frigate_tls")
    mon = frigate_tls.FrigateTlsMonitor(relog_interval_s=3600.0)
    url = "https://192.168.1.10:8971/api/version"
    for i in range(50):
        mon.note(frigate_tls.PROXY_ORIGIN, frigate_tls.TLS_UNTRUSTED, url, now=1000.0 + i)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "certificate Marcellus does not trust" in warnings[0]
    assert "`frigate.verify_tls: false`" in warnings[0]
    assert "`tls: enabled: false`" in warnings[0]
    assert "http://" in warnings[0]
    assert mon.current(frigate_tls.PROXY_ORIGIN) == frigate_tls.TLS_UNTRUSTED

    # Still there an hour later: logged again, once.
    mon.note(frigate_tls.PROXY_ORIGIN, frigate_tls.TLS_UNTRUSTED, url, now=1000.0 + 3600.0)
    mon.note(frigate_tls.PROXY_ORIGIN, frigate_tls.TLS_UNTRUSTED, url, now=1000.0 + 3601.0)
    assert len(_warnings(caplog)) == 2

    # A different cause is a change: logged at once.
    mon.note(frigate_tls.PROXY_ORIGIN, frigate_tls.HTTPS_REQUIRED, url, now=4700.0)
    warnings = _warnings(caplog)
    assert len(warnings) == 3
    assert "Change `frigate.proxy_base_url` to https://" in warnings[2]

    # A real answer clears it (INFO, not WARNING).
    mon.note(frigate_tls.PROXY_ORIGIN, None, url, now=4701.0)
    assert mon.current(frigate_tls.PROXY_ORIGIN) is None
    assert len(_warnings(caplog)) == 3
    assert any("problem is gone" in r.getMessage() for r in caplog.records)


def test_an_ordinary_failure_does_not_clear_a_diagnosis(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, verify_tls=True))
    url = "https://frigate.test:8971/api/version"
    frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, url, _self_signed_error(url))
    frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, url, httpx.ConnectError("refused"))
    assert frigate_tls.monitor(app).current(frigate_tls.PROXY_ORIGIN) == frigate_tls.TLS_UNTRUSTED
    frigate_tls.note_response(
        app, frigate_tls.PROXY_ORIGIN, url, httpx.Response(401, request=httpx.Request("GET", url))
    )
    assert frigate_tls.monitor(app).current(frigate_tls.PROXY_ORIGIN) is None


# -- /healthz and the login page -------------------------------------------------------


class _FakeClient:
    """Stands in for the stream pool (`/healthz`) and the API pool (login)."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls = 0

    async def get(self, url: str, **_: Any) -> Any:
        return self._answer(url)

    async def send(self, req: httpx.Request, **_: Any) -> Any:
        return self._answer(str(req.url))

    def _answer(self, url: str) -> Any:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        status, body = self.outcome
        return httpx.Response(status, text=body, request=httpx.Request("GET", url))

    async def aclose(self) -> None:
        return None

    is_closed = False


@pytest.fixture(autouse=True)
def _no_pool_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("marcellus.routes.health.pool_stats", lambda c: {})


def _health(
    tmp_path: Path, outcome: Any, proxy: str = "https://frigate.test:8971"
) -> dict[str, Any]:
    app = create_app(_settings(tmp_path, verify_tls=True, proxy=proxy))
    app.state.stream_http_client = _FakeClient(outcome)
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200, "a diagnosis never changes the status code"
    return resp.json()


def test_healthz_reports_tls_untrusted(tmp_path: Path) -> None:
    body = _health(tmp_path, _self_signed_error("https://frigate.test:8971/api/version"))
    assert body["status"] == "ok"
    assert body["checks"]["frigate"] == "unreachable"
    assert body["checks"]["frigate_diagnosis"] == "tls_untrusted"


def test_healthz_reports_https_required(tmp_path: Path) -> None:
    body = _health(tmp_path, (400, NGINX_400), proxy="http://frigate.test:8971")
    assert body["checks"]["frigate"] == "ok"
    assert body["checks"]["frigate_diagnosis"] == "https_required"


def test_healthz_has_no_diagnosis_for_an_ordinary_failure(tmp_path: Path) -> None:
    body = _health(tmp_path, httpx.ConnectError("[Errno 61] Connection refused"))
    assert body["checks"]["frigate"] == "unreachable"
    assert "frigate_diagnosis" not in body["checks"]


def test_healthz_has_no_diagnosis_when_frigate_answers(tmp_path: Path) -> None:
    body = _health(tmp_path, (401, "Unauthorized"))
    assert body["checks"]["frigate"] == "ok"
    assert "frigate_diagnosis" not in body["checks"]


def test_login_page_shows_a_known_diagnosis(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, verify_tls=True))
    url = "https://frigate.test:8971/api/version"
    frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, url, _self_signed_error(url))
    with TestClient(app) as client:
        page = client.get("/login").text
    assert 'data-diagnosis="tls_untrusted"' in page
    assert "frigate.verify_tls: false" in page
    assert "tls: enabled: false" in page


def test_login_page_hides_the_note_without_a_diagnosis(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, verify_tls=True))
    with TestClient(app) as client:
        page = client.get("/login").text
    assert 'id="login-problem"' in page
    assert 'data-diagnosis=""' in page


def test_login_diagnosis_probes_and_is_rate_limited(tmp_path: Path) -> None:
    settings = _settings(tmp_path, verify_tls=True)
    settings.sidecar.require_frigate_auth = True
    app = create_app(settings)
    fake = _FakeClient(_self_signed_error("https://frigate.test:8971/api/version"))
    app.state.http_client = fake
    with TestClient(app) as client:
        first = client.get("/login/diagnosis")
        second = client.get("/login/diagnosis")
    assert first.status_code == 200, "reachable without a session"
    assert first.json()["diagnosis"] == "tls_untrusted"
    assert "frigate.verify_tls: false" in first.json()["message"]
    assert second.json()["diagnosis"] == "tls_untrusted"
    assert fake.calls == 1


def test_login_diagnosis_is_null_when_frigate_answers(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, verify_tls=True))
    app.state.http_client = _FakeClient((401, "Unauthorized"))
    with TestClient(app) as client:
        body = client.get("/login/diagnosis").json()
    assert body == {"diagnosis": None, "message": ""}


# -- fsc init probe ------------------------------------------------------------------


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _NginxTlsPort(BaseHTTPRequestHandler):
    """What nginx's `listen ... ssl` port says to a plain HTTP request."""

    def do_GET(self) -> None:
        body = NGINX_400.encode()
        self.send_response(400)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return None


class _PlainFrigate(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return None


def test_probe_finds_https_behind_the_nginx_page() -> None:
    server = _Server(_NginxTlsPort)
    try:
        probe = frigate_tls.probe_signin_port(f"http://127.0.0.1:{server.port}", timeout=2.0)
    finally:
        server.close()
    assert probe.scheme == "https"
    assert probe.address == "127.0.0.1"
    assert probe.private_address


def test_probe_finds_plain_http() -> None:
    server = _Server(_PlainFrigate)
    try:
        probe = frigate_tls.probe_signin_port(f"https://127.0.0.1:{server.port}", timeout=2.0)
    finally:
        server.close()
    assert probe.scheme == "http"


def test_probe_of_a_closed_port_is_unknown() -> None:
    server = _Server(_PlainFrigate)
    port = server.port
    server.close()
    probe = frigate_tls.probe_signin_port(f"https://127.0.0.1:{port}", timeout=1.0)
    assert probe.scheme is None


def test_bounded_probe_never_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    def stuck(url: str, **_: Any) -> frigate_tls.SigninProbe:
        time.sleep(5)
        return frigate_tls.SigninProbe("https", True)

    monkeypatch.setattr(frigate_tls, "probe_signin_port", stuck)
    started = time.monotonic()
    probe = frigate_tls.probe_signin_port_bounded("https://192.168.1.10:8971", deadline_s=0.2)
    assert time.monotonic() - started < 2.0
    assert probe.scheme is None


def test_private_address_rule() -> None:
    assert frigate_tls.SigninProbe("https", False, "192.168.1.10").private_address
    assert frigate_tls.SigninProbe("https", False, "10.0.0.5").private_address
    assert frigate_tls.SigninProbe("https", False, "127.0.0.1").private_address
    assert not frigate_tls.SigninProbe("https", False, "8.8.8.8").private_address
    assert not frigate_tls.SigninProbe("https", False, None).private_address
