"""End to end against a real local TLS server with a self-signed certificate.

Stands in for Frigate's port 8971 with its default TLS: one server that answers
`GET /api/version` over HTTPS and accepts WebSocket upgrades on the same port.
The certificate is generated at test time with the `openssl` command; the
tests skip cleanly where that is unavailable.

- `frigate.verify_tls: true` (the default): healthz carries
  `frigate_diagnosis: tls_untrusted`, a proxied call answers 502 naming the
  setting, and the WebSocket relay refuses the upgrade.
- `frigate.verify_tls: false`: the proxied call and the WebSocket both reach
  the server.
- `http://` pointed at an HTTPS-only port (nginx's 400 page): `https_required`,
  over HTTP and over WebSocket.
"""

from __future__ import annotations

import asyncio
import http
import importlib
import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import frigate_tls
from marcellus.config import FrigateSection, ProxySection, Settings, SidecarSection
from marcellus.server import create_app

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

NGINX_400 = (
    "<html>\r\n<head><title>400 The plain HTTP request was sent to HTTPS port</title>"
    "</head>\r\n<body>\r\n<center><h1>400 Bad Request</h1></center>\r\n"
    "<center>The plain HTTP request was sent to HTTPS port</center>\r\n"
    "<hr><center>nginx</center>\r\n</body>\r\n</html>\r\n"
)


@pytest.fixture(autouse=True)
def _real_health_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest's `_default_frigate_reachable` fakes the `/healthz` probe;
    these tests need the real stream pool against the real local server."""
    from marcellus import frigate_api

    monkeypatch.setattr(
        "marcellus.routes.health.get_stream_client", frigate_api.get_stream_client
    )


@pytest.fixture(scope="module")
def self_signed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not available to generate a self-signed certificate")
    folder = tmp_path_factory.mktemp("tls")
    cert, key = folder / "cert.pem", folder / "key.pem"
    try:
        subprocess.run(
            [
                openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cert), "-days", "1",
                "-subj", "/CN=localhost",
                "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        pytest.skip(f"could not generate a self-signed certificate: {exc}")
    return cert, key


def _ws_serve() -> Any:
    try:
        return importlib.import_module("websockets.asyncio.server").serve
    except (ImportError, AttributeError):
        pytest.skip("needs websockets >= 13 (asyncio server with process_request)")


@pytest.fixture
def tls_frigate(self_signed: tuple[Path, Path]) -> Iterator[int]:
    """HTTPS `/api/version` and a WebSocket echo on one self-signed port."""
    serve = _ws_serve()
    cert, key = self_signed
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))

    def process_request(connection: Any, request: Any) -> Any:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return connection.respond(http.HTTPStatus.OK, "0.18.2\n")
        return None

    async def handler(ws: Any) -> None:
        async for message in ws:
            await ws.send(f"echo:{message}")

    port_holder: list[int] = []
    stop_holder: list[asyncio.Future[None]] = []
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    async def main() -> None:
        async with serve(
            handler, "127.0.0.1", 0, ssl=server_ctx, process_request=process_request
        ) as server:
            port_holder.append(server.sockets[0].getsockname()[1])
            stop_holder.append(asyncio.get_running_loop().create_future())
            ready.set()
            await stop_holder[0]

    thread = threading.Thread(target=lambda: loop.run_until_complete(main()), daemon=True)
    thread.start()
    assert ready.wait(10), "TLS server did not start"
    try:
        yield port_holder[0]
    finally:
        loop.call_soon_threadsafe(stop_holder[0].set_result, None)
        thread.join(timeout=5)
        if not loop.is_running():
            loop.close()


class _NginxTlsPort(BaseHTTPRequestHandler):
    """What nginx's `listen ... ssl` port says to any plain HTTP request."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        body = NGINX_400.encode()
        self.send_response(400)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return None


@pytest.fixture
def nginx_tls_port() -> Iterator[int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NginxTlsPort)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def _app(tmp_path: Path, frigate_db_path: Path, proxy_url: str, *, verify_tls: bool) -> Any:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:9",
            proxy_base_url=proxy_url,
            verify_tls=verify_tls,
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=tmp_path / "marcellus.db", bind_port=5001, require_frigate_auth=False
        ),
        proxy=ProxySection(enabled=True),
    )
    return create_app(settings)


def test_strict_check_fails_with_the_diagnosis(
    tmp_path: Path, frigate_db_path: Path, tls_frigate: int
) -> None:
    from starlette.websockets import WebSocketDisconnect

    app = _app(tmp_path, frigate_db_path, f"https://127.0.0.1:{tls_frigate}", verify_tls=True)
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["checks"]["frigate"] == "unreachable"
        assert health["checks"]["frigate_diagnosis"] == "tls_untrusted"

        proxied = client.get("/api/version")
        assert proxied.status_code == 502
        assert "CERTIFICATE_VERIFY_FAILED" in proxied.text
        assert "frigate.verify_tls: false" in proxied.text

        app.state.frigate_tls_monitor = None  # prove the WebSocket path diagnoses on its own
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
            ws.receive_text()
        assert (
            frigate_tls.monitor(app).current(frigate_tls.PROXY_ORIGIN)
            == frigate_tls.TLS_UNTRUSTED
        )


def test_verify_tls_false_reaches_the_self_signed_server(
    tmp_path: Path, frigate_db_path: Path, tls_frigate: int
) -> None:
    app = _app(tmp_path, frigate_db_path, f"https://127.0.0.1:{tls_frigate}", verify_tls=False)
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["checks"]["frigate"] == "ok"
        assert "frigate_diagnosis" not in health["checks"]

        proxied = client.get("/api/version")
        assert proxied.status_code == 200
        assert proxied.text.strip() == "0.18.2"

        with client.websocket_connect("/ws") as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"


def test_plain_http_to_an_https_port_is_https_required(
    tmp_path: Path, frigate_db_path: Path, nginx_tls_port: int
) -> None:
    from starlette.websockets import WebSocketDisconnect

    app = _app(tmp_path, frigate_db_path, f"http://127.0.0.1:{nginx_tls_port}", verify_tls=True)
    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["checks"]["frigate"] == "ok"
        assert health["checks"]["frigate_diagnosis"] == "https_required"

        app.state.frigate_tls_monitor = None
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws") as ws:
            ws.receive_text()
        assert (
            frigate_tls.monitor(app).current(frigate_tls.PROXY_ORIGIN)
            == frigate_tls.HTTPS_REQUIRED
        )


def test_probe_detects_the_self_signed_certificate(tls_frigate: int) -> None:
    for scheme in ("https", "http"):
        probe = frigate_tls.probe_signin_port(f"{scheme}://127.0.0.1:{tls_frigate}", timeout=3.0)
        assert probe.scheme == "https"
        assert probe.trusted is False
        assert probe.private_address
