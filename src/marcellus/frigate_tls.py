"""Certificate checks and scheme diagnosis for connections to Frigate.

Frigate serves its signed-in port (8971) over HTTPS with a self-signed
certificate by default. A client that checks certificates cannot talk to it
until the certificate is trusted, and a plain `http://` URL pointed at that
port gets nginx's "400 The plain HTTP request was sent to HTTPS port" for every
request. Before this module both failures looked like a Frigate that was
simply down: the login check answered 401, proxied calls 502, and nothing said
why.

Two jobs, kept in one place:

1. **Build every Frigate-bound client.** `async_client()`, `sync_client()`,
   `get()` and `ws_ssl_context()` all take `frigate.verify_tls` as a required
   keyword, and no other module constructs an httpx client or request bound
   for Frigate (`tests/test_frigate_tls.py` fails the build if one does). With
   `verify_tls: false` the certificate is not checked on connections to
   Frigate's two origins (`frigate.base_url`, `frigate.proxy_base_url`), HTTP
   and WebSocket alike, and nowhere else: the push relay, APNs, UniFi Protect
   and external sources build their own clients with their own settings.
2. **Say why a connection failed.** `classify_error()` / `classify_response()`
   turn a failure into one of two stable diagnosis words (`TLS_UNTRUSTED`,
   `HTTPS_REQUIRED`); `FrigateTlsMonitor` logs one WARNING per change (and at
   most once an hour while it lasts) and holds the current word for
   `/healthz` (`checks.frigate_diagnosis`) and the login page.

`probe_signin_port()` is the `fsc init` side: it works out which scheme the
signed-in port speaks and whether its certificate is trusted, with short
timeouts and a hard overall deadline so a dead address can never hang init.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

#: `/healthz` `checks.frigate_diagnosis` values. Stable wire words: the app
#: may show its own copy for them, so never rename one.
TLS_UNTRUSTED = "tls_untrusted"
HTTPS_REQUIRED = "https_required"
DIAGNOSES = (TLS_UNTRUSTED, HTTPS_REQUIRED)

#: Origin keys for `FrigateTlsMonitor`.
PROXY_ORIGIN = "proxy_base_url"
API_ORIGIN = "base_url"

# nginx's standard answer (its internal 497, sent as 400) when a plain HTTP
# request reaches a port that is listening with `ssl`. Frigate's own nginx
# serves 8971 that way.
_PLAIN_HTTP_PHRASE = "plain http request was sent to https port"

#: While a diagnosis lasts, it is logged again at most this often, so someone
#: who opens the log a day later still finds it near the end.
RELOG_INTERVAL_S = 3600.0


# -- clients -------------------------------------------------------------------


def async_client(*, verify_tls: bool, **kwargs: Any) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` for Frigate; `verify_tls` is `frigate.verify_tls`."""
    return httpx.AsyncClient(verify=verify_tls, **kwargs)


def sync_client(*, verify_tls: bool, **kwargs: Any) -> httpx.Client:
    """An `httpx.Client` for Frigate; `verify_tls` is `frigate.verify_tls`."""
    return httpx.Client(verify=verify_tls, **kwargs)


def get(url: str, *, verify_tls: bool, **kwargs: Any) -> httpx.Response:
    """One-shot `httpx.get` to Frigate; `verify_tls` is `frigate.verify_tls`."""
    return httpx.get(url, verify=verify_tls, **kwargs)


def ws_ssl_context(url: str, *, verify_tls: bool) -> ssl.SSLContext | None:
    """The `ssl=` argument for a WebSocket connection to Frigate.

    None for `ws://` (websockets refuses an `ssl` argument there) and for a
    verified `wss://` (websockets then builds its own default, verifying
    context).
    """
    if verify_tls or not url.lower().startswith("wss://"):
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def app_verify_tls(app: Any) -> bool:
    """`frigate.verify_tls` for an app; strict when the app has no settings."""
    settings = getattr(getattr(app, "state", None), "settings", None)
    frigate = getattr(settings, "frigate", None)
    return bool(getattr(frigate, "verify_tls", True))


# -- diagnosis -------------------------------------------------------------------


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _is_plain_scheme(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "ws")


def classify_response(
    status_code: int, body: object, url: str, location: str | None = None
) -> str | None:
    """`HTTPS_REQUIRED` when an answer to a plain `http://`/`ws://` URL says
    the port speaks HTTPS; otherwise None.

    Recognises nginx's standard page (Frigate's nginx sends it as 400; 497 is
    nginx's internal code, accepted in case a proxy in front passes it on)
    and, defensively, a redirect to `https://` on the same host and port.
    """
    if not _is_plain_scheme(url):
        return None
    if status_code in (400, 497):
        if isinstance(body, (bytes, bytearray, memoryview)):
            text = bytes(body).decode("utf-8", "replace")
        else:
            text = body if isinstance(body, str) else ""
        if _PLAIN_HTTP_PHRASE in text.lower():
            return HTTPS_REQUIRED
        return None
    if 300 <= status_code < 400 and location:
        target = urlsplit(location)
        origin = urlsplit(url)
        if (
            target.scheme.lower() == "https"
            and (target.hostname or "") == (origin.hostname or "")
            and target.port == origin.port
        ):
            return HTTPS_REQUIRED
    return None


def classify_error(exc: BaseException, url: str) -> str | None:
    """The diagnosis for a failed connection to `url`, or None for an
    ordinary failure (refused, timed out, DNS) that keeps its usual handling.

    `TLS_UNTRUSTED`: certificate verification failed (self-signed, unknown
    issuer, or a name that does not match). `HTTPS_REQUIRED`: a plain
    `http://`/`ws://` attempt got an HTTPS-only answer -- nginx's 400 page
    inside a WebSocket handshake rejection, or a TLS alert record where an
    HTTP status line should be.
    """
    chain = _exception_chain(exc)
    for err in chain:
        if isinstance(err, ssl.SSLCertVerificationError):
            return TLS_UNTRUSTED
    text = " ".join(str(err) for err in chain)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return TLS_UNTRUSTED
    if not _is_plain_scheme(url):
        return None
    for err in chain:
        # websockets' InvalidStatus carries the rejected handshake response.
        response = getattr(err, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            headers = getattr(response, "headers", None)
            location = headers.get("location") if headers is not None else None
            found = classify_response(status, getattr(response, "body", None), url, location)
            if found is not None:
                return found
    # A TLS alert record (0x15 0x03 ...) read as an HTTP status line.
    if "\\x15\\x03" in text:
        return HTTPS_REQUIRED
    return None


def explain(diagnosis: str, url: str, *, setting: str = "frigate.proxy_base_url") -> str:
    """The plain-language WARNING text for a diagnosis."""
    if diagnosis == TLS_UNTRUSTED:
        return (
            f"Frigate at {url} presented a TLS certificate Marcellus does not trust "
            "(Frigate makes a self-signed one by default), so every request to it fails. "
            "Either set `frigate.verify_tls: false` in the sidecar config (fine on a home "
            "network; it gives up proof that the other end really is your Frigate) or turn "
            "TLS off in Frigate (`tls: enabled: false`) and use http:// in "
            f"`{setting}`."
        )
    if diagnosis == HTTPS_REQUIRED:
        return (
            f"Frigate at {url} speaks HTTPS, but `{setting}` uses http://, so Frigate "
            "refuses every request (400: plain HTTP request sent to an HTTPS port). Change "
            f"`{setting}` to https://. Frigate's default certificate is self-signed, so "
            "also set `frigate.verify_tls: false`, or turn TLS off in Frigate "
            "(`tls: enabled: false`) and keep http://."
        )
    return diagnosis


def login_message(diagnosis: str) -> str:
    """The shorter text the login page shows for a diagnosis."""
    if diagnosis == TLS_UNTRUSTED:
        return (
            "Marcellus cannot reach Frigate's sign-in port: it presented a TLS certificate "
            "Marcellus does not trust (Frigate makes a self-signed one by default). In the "
            "Marcellus config, set frigate.verify_tls: false, or turn TLS off in Frigate "
            "(tls: enabled: false) and use http:// in frigate.proxy_base_url."
        )
    if diagnosis == HTTPS_REQUIRED:
        return (
            "Marcellus cannot reach Frigate's sign-in port: it speaks HTTPS, but "
            "frigate.proxy_base_url in the Marcellus config uses http://. Change it to "
            "https:// (with Frigate's default self-signed certificate, also set "
            "frigate.verify_tls: false)."
        )
    return diagnosis


@dataclass
class _OriginState:
    diagnosis: str | None
    url: str
    logged_at: float


class FrigateTlsMonitor:
    """The current diagnosis per Frigate origin, logged on change.

    `note()` is called with the outcome of each instrumented request (None on
    a success or an ordinary failure). A new diagnosis is logged at WARNING
    once; while it lasts it is logged again at most every
    `RELOG_INTERVAL_S`; its clearing is logged at INFO. Requests that never
    touch the network (cache hits) do not call `note()`, so nothing here runs
    per request on a healthy path.
    """

    def __init__(self, relog_interval_s: float = RELOG_INTERVAL_S) -> None:
        self.relog_interval_s = relog_interval_s
        self._origins: dict[str, _OriginState] = {}
        self._lock = threading.Lock()

    def note(
        self, origin: str, diagnosis: str | None, url: str, *, now: float | None = None
    ) -> None:
        now = time.monotonic() if now is None else now
        setting = f"frigate.{origin}"
        with self._lock:
            previous = self._origins.get(origin)
            if diagnosis is None:
                if previous is not None and previous.diagnosis is not None:
                    logger.info(
                        "Frigate at %s answers again; the earlier %s problem is gone",
                        url,
                        previous.diagnosis,
                    )
                self._origins[origin] = _OriginState(None, url, now)
                return
            due = previous is None or previous.diagnosis != diagnosis
            if not due and previous is not None:
                due = now - previous.logged_at >= self.relog_interval_s
            if due:
                logger.warning("%s", explain(diagnosis, url, setting=setting))
                self._origins[origin] = _OriginState(diagnosis, url, now)

    def current(self, origin: str) -> str | None:
        with self._lock:
            state = self._origins.get(origin)
            return state.diagnosis if state is not None else None


def monitor(app: Any) -> FrigateTlsMonitor:
    """The app's monitor, created on first use."""
    state = app.state
    found: FrigateTlsMonitor | None = getattr(state, "frigate_tls_monitor", None)
    if found is None:
        found = FrigateTlsMonitor()
        state.frigate_tls_monitor = found
    return found


def note_error(app: Any, origin: str, url: str, exc: BaseException) -> str | None:
    """Classify a failed request and record a diagnosis; returns it.

    An ordinary failure (refused, timed out) records nothing: it says nothing
    about whether an earlier certificate or scheme problem is fixed. Only a
    real answer from Frigate (`note_response`) clears one.
    """
    try:
        diagnosis = classify_error(exc, url)
        if diagnosis is not None:
            monitor(app).note(origin, diagnosis, url)
    except Exception:  # noqa: BLE001 -- explaining a failure must never add one
        logger.debug("frigate_tls: could not classify %r", exc, exc_info=True)
        return None
    return diagnosis


def note_response(app: Any, origin: str, url: str, resp: Any) -> str | None:
    """Classify an answer (any object with `status_code`, optional `text` and
    `headers`) and record it; returns the diagnosis."""
    status = getattr(resp, "status_code", 0)
    body: str | None = None
    location: str | None = None
    if status in (400, 497) or 300 <= status < 400:
        try:
            body = resp.text
        except Exception:  # noqa: BLE001 -- a stream not read yet has no text
            body = None
        headers = getattr(resp, "headers", None)
        location = headers.get("location") if headers is not None else None
    try:
        diagnosis = classify_response(status, body, url, location)
        monitor(app).note(origin, diagnosis, url)
    except Exception:  # noqa: BLE001 -- explaining a failure must never add one
        logger.debug("frigate_tls: could not classify a %s answer", status, exc_info=True)
        return None
    return diagnosis


# -- `fsc init` probe ------------------------------------------------------------


@dataclass(frozen=True)
class SigninProbe:
    """What `probe_signin_port` found.

    `scheme` is "https", "http" or None (no answer either way). `trusted` is
    only meaningful for https: True when the certificate verified, False when
    it did not, None when unknown. `address` is the IP the host resolved to.
    """

    scheme: str | None
    trusted: bool | None = None
    address: str | None = None
    detail: str = ""

    @property
    def private_address(self) -> bool:
        if not self.address:
            return False
        try:
            return ipaddress.ip_address(self.address).is_private
        except ValueError:
            return False


def with_scheme(url: str, scheme: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def probe_signin_port(url: str, *, timeout: float = 3.0) -> SigninProbe:
    """Find out whether the port in `url` speaks HTTPS, and whether its
    certificate is trusted. Any failure just means "unknown"."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return SigninProbe(None, detail="no host in URL")
    try:
        port = parts.port
    except ValueError:
        return SigninProbe(None, detail="bad port in URL")
    address: str | None = None
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        address = str(infos[0][4][0]) if infos else None
    except OSError as exc:
        return SigninProbe(None, detail=f"cannot resolve {host}: {exc}")

    https_url = with_scheme(url, "https").rstrip("/") + "/api/version"
    http_url = with_scheme(url, "http").rstrip("/") + "/api/version"
    try:
        get(https_url, verify_tls=True, timeout=timeout)
        return SigninProbe("https", True, address)
    except httpx.HTTPError as exc:
        if classify_error(exc, https_url) == TLS_UNTRUSTED:
            return SigninProbe("https", False, address, detail=str(exc))
    try:
        resp = get(http_url, verify_tls=True, timeout=timeout)
    except httpx.HTTPError as exc:
        if classify_error(exc, http_url) == HTTPS_REQUIRED:
            return SigninProbe("https", None, address, detail=str(exc))
        return SigninProbe(None, address=address, detail=str(exc))
    if classify_response(resp.status_code, resp.text, http_url, resp.headers.get("location")):
        return SigninProbe("https", None, address, detail="answers HTTPS only")
    return SigninProbe("http", None, address)


def probe_signin_port_bounded(url: str, *, deadline_s: float = 8.0) -> SigninProbe:
    """`probe_signin_port` with a hard overall deadline.

    Name resolution has no timeout of its own, so the probe runs on a daemon
    thread and is abandoned (reported as unknown) if it is not done in time.
    """
    result: list[SigninProbe] = []

    def _run() -> None:
        try:
            result.append(probe_signin_port(url))
        except Exception as exc:  # noqa: BLE001 -- a probe never fails init
            result.append(SigninProbe(None, detail=str(exc)))

    thread = threading.Thread(target=_run, name="fsc-init-probe", daemon=True)
    thread.start()
    thread.join(deadline_s)
    if not result:
        return SigninProbe(None, detail=f"no answer within {deadline_s:.0f}s")
    return result[0]
