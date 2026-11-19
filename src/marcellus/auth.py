"""Frigate-session gate for every request the sidecar serves.

The sidecar has no user database and never holds a password: it validates the
caller's own Frigate credential -- the `Cookie` header (`frigate_token`) and/or
an `Authorization: Bearer` header, both of which Frigate accepts -- against
Frigate's authenticated origin and caches the pass for a short TTL
(docs/scrub-cache-and-proxy-spec.md §3.2 finding 5, option (a)). It covers the
sidecar's own routes (triage UI, `/faces/captures`, `/analysis`, `/toybox`,
`/v1`), which expose event history, face crops of identified people and writes
with side effects on Frigate, and the reverse-proxy catch-all as well.

The catch-all used to be left to Frigate ("Frigate authenticates proxied
traffic itself"). That held only while every proxied request carried exactly
its caller's credential and `frigate.proxy_base_url` was an origin that checks
it. The shared proxy client once kept a cookie jar and replayed the last
proxied login's `frigate_token` on every later request that sent no cookie,
so any caller acted as the last user who signed in; and a `proxy_base_url`
pointed at Frigate's unauthenticated port checks nothing. The gate now runs
before anything is forwarded, so a caller without a credential of its own is
refused here, and an origin that would serve such callers while Frigate has
sign-in on (`proxy_origin_unprotected`) fails every request closed.

Deliberately NOT gated here:

* `POST /api/login` -- forwarded to Frigate so a caller can get a session;
  rate-limited per IP instead (`LoginRateLimiter`);
* `/v1/capabilities`, `/healthz`, `/version` -- reachability probes a client
  needs *before* it has a session, `/login` (the page that gets one), and
  `/static`;
* `/v1/push/thumbnail/` -- fetched by the iOS Notification Service Extension
  (docs/push-notifications.md), which has no Frigate session and cannot
  acquire one (it runs in its own short-lived, network-isolated sandbox).
  Safe to leave open: a handle is an opaque, unguessable, short-lived
  (`push.handle_ttl_s`/`situation_handle_ttl_s`) token that maps only to a
  pre-fetched thumbnail image, never to anything the sidecar or Frigate can
  be made to do -- the same trust model as a signed download link.

A request with no credential at all is admitted only when Frigate itself
serves such callers, i.e. sign-in is turned off there
(`frigate_allows_anonymous`): then the gate gives away nothing Frigate does
not, and an app that saved the server as "no sign-in" still gets the sidecar's
features and the proxy.

The gate is applied as raw ASGI middleware rather than a router dependency so
it can't be forgotten on a newly added route, and so it doesn't wrap the
proxy's streaming responses.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from marcellus import frigate_tls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

    from marcellus.config import Settings

logger = logging.getLogger(__name__)

ERR_UNAUTHORIZED = "unauthorized"
ERR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
# `frigate.proxy_base_url` serves callers with no credential while Frigate has
# sign-in on, so it can't tell a real session from a made-up one.
ERR_PROXY_ORIGIN_UNPROTECTED = "proxy_origin_unprotected"

# Reachability probes a client may need before it holds a Frigate session
# (and the login page itself, which exists to acquire one).
EXEMPT_PATHS = frozenset(
    {"/healthz", "/version", "/v1/capabilities", "/login", "/login/diagnosis"}
)
# Trailing slash on the push-thumbnail prefix on purpose: exempts only
# `/v1/push/thumbnail/{handle}` fetches, not `/v1/push/thumbnail` itself or
# any other `/v1/push/...` route (device registration, handle redemption,
# snooze, etc. all stay gated).
EXEMPT_PREFIXES = ("/static/", "/v1/push/thumbnail/")


REMEMBER_COOKIE = "sidecar_remember"


def session_secret(app: FastAPI) -> bytes:
    """Per-install signing secret for the remember-me cookie.

    Persisted next to the sidecar DB so tokens survive restarts; deleting the
    file invalidates every outstanding remember-me cookie at once.
    """
    cached: bytes | None = getattr(app.state, "session_secret", None)
    if cached is not None:
        return cached
    settings: Settings = app.state.settings
    path = settings.sidecar.db_path.parent / ".session_secret"
    try:
        secret = path.read_bytes().strip()
        if len(secret) < 32:
            raise ValueError("secret too short")
    except (OSError, ValueError):
        secret = secrets.token_hex(32).encode()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(mode=0o600, exist_ok=True)
            path.write_bytes(secret)
        except OSError:
            # Unwritable data dir: tokens still work until restart.
            pass
    app.state.session_secret = secret
    return secret


def mint_remember_token(app: FastAPI, ttl_s: float) -> str:
    expiry = str(int(time.time() + ttl_s))
    sig = hmac.new(session_secret(app), f"remember:{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"


def _remember_token_valid(app: FastAPI, token: str) -> bool:
    expiry, _, sig = token.partition(".")
    if not expiry.isdigit() or not sig:
        return False
    if int(expiry) <= time.time():
        return False
    want = hmac.new(session_secret(app), f"remember:{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def _cookie_value(cookie_header: str, name: str) -> str:
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
        morsel = jar.get(name)
        return morsel.value if morsel else ""
    except Exception:  # noqa: BLE001 -- malformed cookie header is just "absent"
        return ""


def _cache(app: FastAPI) -> dict[str, float]:
    cache: dict[str, float] | None = getattr(app.state, "auth_cache", None)
    if cache is None:
        cache = {}
        app.state.auth_cache = cache
    return cache


def _remember(app: FastAPI, key: str, expiry: float, *, max_entries: int) -> None:
    """Record a validated session, evicting expired entries first.

    Frigate rotates its JWT, so the raw cookie is an unbounded key space: the
    cache has to be swept and capped or it is simply a slow memory leak.
    """
    cache = _cache(app)
    now = time.time()
    if len(cache) >= max_entries:
        for k in [k for k, exp in cache.items() if exp <= now]:
            del cache[k]
    while len(cache) >= max_entries:
        oldest = min(cache, key=lambda k: cache[k])
        del cache[oldest]
    cache[key] = expiry


async def validate_frigate_session(
    app: FastAPI,
    cookie: str,
    *,
    ttl_s: float | None = None,
    authorization: str = "",
) -> None:
    """Raise HTTPException unless the caller's credential is a live Frigate
    session: its `Cookie` header and/or its `Authorization` header (Frigate
    reads a bearer token first, then the `frigate_token` cookie).

    Validated against `frigate.proxy_base_url` -- the authenticated origin --
    because that is the one that can actually reject a bad credential. When
    that origin serves callers with no credential while Frigate has sign-in on
    (`proxy_origin_unprotected`), it would accept anything, so the request is
    refused with a 502 instead.

    `ttl_s` overrides how long a successful check is cached; callers holding a
    valid remember-me cookie pass the longer `remember_cache_ttl_s`. Defaults
    to `sidecar.auth_cache_ttl_s`.
    """
    from marcellus.frigate_api import get_async_client

    settings: Settings = app.state.settings
    if not cookie and not authorization:
        # No credential to check. Admitted only when Frigate itself serves
        # callers without one (sign-in turned off) -- see
        # `frigate_allows_anonymous`.
        if await frigate_allows_anonymous(app):
            return
        raise HTTPException(status_code=401, detail="frigate session required")

    if await proxy_origin_unprotected(app):
        raise HTTPException(
            status_code=502,
            detail=(
                f"{ERR_PROXY_ORIGIN_UNPROTECTED}: frigate.proxy_base_url serves requests "
                "without a session while Frigate has sign-in on, so sessions cannot be "
                "checked; point it at Frigate's authenticated port (8971)"
            ),
        )

    key = hashlib.sha256(f"{cookie}\n{authorization}".encode()).hexdigest()
    now = time.time()
    expiry = _cache(app).get(key)
    if expiry is not None and expiry > now:
        return

    upstream = f"{settings.frigate.proxy_base_url.rstrip('/')}/api/version"
    client = get_async_client(app)
    headers = {}
    if cookie:
        headers["cookie"] = cookie
    if authorization:
        headers["authorization"] = authorization
    try:
        resp = await client.get(upstream, headers=headers, timeout=5.0)
    except httpx.HTTPError as exc:
        frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, upstream, exc)
        raise HTTPException(
            status_code=502, detail=f"{ERR_UPSTREAM_UNAVAILABLE}: {exc}"
        ) from exc

    frigate_tls.note_response(app, frigate_tls.PROXY_ORIGIN, upstream, resp)
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=401, detail="frigate session invalid")
    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=ERR_UPSTREAM_UNAVAILABLE)

    _remember(
        app,
        key,
        now + (settings.sidecar.auth_cache_ttl_s if ttl_s is None else ttl_s),
        max_entries=settings.sidecar.auth_cache_max_entries,
    )


# How long a failed anonymous-access check (Frigate unreachable, or an answer
# that would not parse) is remembered. Shorter than a definite answer's
# `auth_cache_ttl_s`, so a Frigate that was briefly down at the app's first
# request does not lock a sign-in-off deployment out for the full window.
ANONYMOUS_ERROR_TTL_S = 5.0


def config_says_signin_off(body: object) -> bool:
    """True when a Frigate `/api/config` body has `auth.enabled` exactly
    `false`. A missing key, `true`, or a body that is not a Frigate config all
    mean no (shared with `marcellus doctor`)."""
    auth_section = body.get("auth") if isinstance(body, dict) else None
    return isinstance(auth_section, dict) and auth_section.get("enabled") is False


async def frigate_allows_anonymous(app: FastAPI) -> bool:
    """True only when Frigate itself admits callers that carry no session.

    That is the case for a Frigate with sign-in turned off (`auth.enabled:
    false`): it serves every caller on its authenticated port without a
    cookie, so the sidecar admitting the same callers gives away nothing
    Frigate does not already give away. Never assumed -- both have to hold:

    1. Frigate's running config, read from `frigate.base_url` (the origin the
       sidecar calls itself, which reports the real value on either port),
       says `auth.enabled` is exactly `false`. A missing key, `true`, a
       non-200 or a body that is not a Frigate config all mean "no".
    2. A bare `GET /api/version` on `frigate.proxy_base_url` -- the origin
       client traffic is forwarded to -- answers 2xx. This is Frigate's own
       decision for a caller with no credential: it refuses with 401 when
       sign-in is on, and also when `proxy.auth_secret` is set or a reverse
       proxy in front of that origin wants its own login.

    (1) on its own would open the gate when `proxy_base_url` is an origin
    Frigate does not serve openly; (2) on its own would open it when
    `proxy_base_url` is misconfigured to the unauthenticated port 5000 of a
    Frigate that has sign-in on. Requiring both means a deployment where
    Frigate asks for a session keeps asking for one here too.

    The answer is cached for `sidecar.auth_cache_ttl_s` (a failed check for
    `ANONYMOUS_ERROR_TTL_S`), both ways, so turning sign-in back on in Frigate
    closes the gate within one window, and session-less requests to a Frigate
    with sign-in on cost one upstream check per window, not one each.
    """
    settings: Settings = app.state.settings
    now = time.time()
    cached: tuple[float, bool] | None = getattr(app.state, "anonymous_access", None)
    if cached is not None and cached[0] > now:
        return cached[1]

    allowed, ttl_s, origin_open = await _check_anonymous_access(app, settings)
    previous = cached[1] if cached is not None else False
    if allowed and not previous:
        logger.warning(
            "Frigate reports sign-in turned off (auth.enabled: false) and serves "
            "callers without a session; the sidecar now does the same"
        )
    elif previous and not allowed:
        logger.warning("Frigate no longer serves callers without a session; sign-in required again")
    if origin_open and not getattr(app.state, "proxy_origin_open", False):
        logger.error(
            "frigate.proxy_base_url (%s) serves requests without a session although "
            "Frigate has sign-in on -- probably Frigate's unauthenticated port. Sessions "
            "cannot be checked against it, so every request is refused until it points "
            "at the authenticated port",
            settings.frigate.proxy_base_url,
        )
    app.state.proxy_origin_open = origin_open
    app.state.anonymous_access = (now + ttl_s, allowed)
    return allowed


async def proxy_origin_unprotected(app: FastAPI) -> bool:
    """True when `frigate.proxy_base_url` answers a bare request although
    Frigate's config says sign-in is on.

    That origin is then not one that checks credentials (typically
    `proxy_base_url` set to the unauthenticated port 5000): validating a
    session against it would pass anything, and forwarding to it would serve
    every caller. Read from the same cached checks as
    `frigate_allows_anonymous`.
    """
    await frigate_allows_anonymous(app)
    return bool(getattr(app.state, "proxy_origin_open", False))


async def _check_anonymous_access(
    app: FastAPI, settings: Settings
) -> tuple[bool, float, bool]:
    """One uncached run of the two checks in `frigate_allows_anonymous`.

    Returns `(allowed, ttl_s, origin_open)`: whether a caller with no
    credential is admitted, how long the answer may be trusted, and whether
    the proxy origin serves such callers although Frigate's config says
    sign-in is on (`proxy_origin_unprotected`).
    """
    from marcellus.frigate_api import get_async_client

    ttl_s = settings.sidecar.auth_cache_ttl_s
    client = get_async_client(app)
    timeout = httpx.Timeout(5.0).as_dict()
    # Requests built by hand and sent with `send()`, so the pooled client's
    # cookie jar is never merged in: these checks must carry no credential.
    config_req = httpx.Request(
        "GET",
        f"{settings.frigate.base_url.rstrip('/')}/api/config",
        extensions={"timeout": timeout},
    )
    version_req = httpx.Request(
        "GET",
        f"{settings.frigate.proxy_base_url.rstrip('/')}/api/version",
        extensions={"timeout": timeout},
    )
    try:
        resp = await client.send(config_req)
    except httpx.HTTPError as exc:
        frigate_tls.note_error(app, frigate_tls.API_ORIGIN, str(config_req.url), exc)
        return False, ANONYMOUS_ERROR_TTL_S, False
    frigate_tls.note_response(app, frigate_tls.API_ORIGIN, str(config_req.url), resp)
    try:
        if resp.status_code != 200:
            return False, ttl_s, False
        body = resp.json()
    except ValueError:
        return False, ANONYMOUS_ERROR_TTL_S, False
    signin_off = config_says_signin_off(body)
    # Probed whichever way the config reads: with sign-in on, a 2xx here means
    # the proxy origin does not check credentials at all.
    try:
        bare = await client.send(version_req)
    except httpx.HTTPError as exc:
        frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, str(version_req.url), exc)
        return False, ANONYMOUS_ERROR_TTL_S, False
    frigate_tls.note_response(app, frigate_tls.PROXY_ORIGIN, str(version_req.url), bare)
    served = 200 <= bare.status_code < 300
    return signin_off and served, ttl_s, served and not signin_off


class LoginRateLimiter:
    """Per-IP sliding-window limiter over failed login attempts.

    An "attempt" is a POST to `/api/login` that the proxy relays (see
    `FrigateAuthMiddleware`) that fails; a successful login never counts.
    Deliberately does NOT count 401s from `validate_frigate_session` on owned
    routes: the iOS app fires several requests in parallel whenever its
    cookie has expired, so counting each 401 hits the limit in seconds and
    then 429s its own re-login for a full window. Guessing a valid session
    cookie is also infeasible (HMAC-signed), so counting those 401s buys no
    real protection anyway -- only the login endpoint itself is worth
    limiting. State lives in a plain dict on `app.state`, swept and capped
    the same way `auth.py`'s `_cache`/`_remember` are -- the IP key space is
    unbounded, so it has to be evicted or it's a slow memory leak.
    """

    MAX_ENTRIES = 4096

    def __init__(self, app: FastAPI, attempts: int, window_s: float) -> None:
        self.app = app
        self.attempts = attempts
        self.window_s = window_s

    def _buckets(self) -> dict[str, list[float]]:
        buckets: dict[str, list[float]] | None = getattr(
            self.app.state, "login_rate_limit_buckets", None
        )
        if buckets is None:
            buckets = {}
            self.app.state.login_rate_limit_buckets = buckets
        return buckets

    def _warned(self) -> dict[str, float]:
        warned: dict[str, float] | None = getattr(
            self.app.state, "login_rate_limit_warned", None
        )
        if warned is None:
            warned = {}
            self.app.state.login_rate_limit_warned = warned
        return warned

    def _live(self, ip: str, now: float) -> list[float]:
        buckets = self._buckets()
        times = [t for t in buckets.get(ip, []) if t > now - self.window_s]
        buckets[ip] = times
        return times

    def retry_after(self, ip: str) -> float | None:
        """Seconds until `ip`'s window frees, or None if it isn't blocked.

        Read-only: does not itself count as an attempt.
        """
        if self.attempts <= 0:
            return None
        now = time.time()
        times = self._live(ip, now)
        if len(times) < self.attempts:
            return None
        blocked_since = min(times)
        if self._warned().get(ip) != blocked_since:
            self._warned()[ip] = blocked_since
            logger.warning("login rate limit: %s", ip)
        return max(1.0, blocked_since + self.window_s - now)

    def record_failure(self, ip: str) -> None:
        """Count one failed attempt for `ip`."""
        if self.attempts <= 0:
            return
        now = time.time()
        buckets = self._buckets()
        times = self._live(ip, now)
        times.append(now)
        buckets[ip] = times
        if len(buckets) > self.MAX_ENTRIES:
            for k in [k for k, v in buckets.items() if not v]:
                del buckets[k]
            while len(buckets) > self.MAX_ENTRIES:
                oldest = min(buckets, key=lambda k: min(buckets[k], default=now))
                del buckets[oldest]
                self._warned().pop(oldest, None)


def _client_ip(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _rate_limit_response(retry_after: float, app: FastAPI) -> JSONResponse:
    app.state.login_rate_limit_rejections = (
        getattr(app.state, "login_rate_limit_rejections", 0) + 1
    )
    seconds = int(retry_after) + (1 if retry_after % 1 else 0)
    body = {
        "detail": {
            "error": "rate_limited",
            "message": f"too many login attempts; retry in {seconds}s",
        }
    }
    response = JSONResponse(body, status_code=429, headers={"Retry-After": str(seconds)})
    return response


class FrigateAuthMiddleware:
    """Require the caller's own Frigate credential on every request: the
    sidecar's routes and the proxy catch-all alike (exemptions in the module
    docstring)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # WebSocket scopes are gated too: the proxied ones (Frigate's `/ws`
        # state feed, go2rtc signalling under `/live`) and any the sidecar
        # adds itself.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        app: FastAPI = scope["app"]
        settings: Settings = app.state.settings
        path: str = scope.get("path", "")
        is_login_post = (
            scope["type"] == "http" and scope.get("method") == "POST" and path == "/api/login"
        )
        if (
            not settings.sidecar.require_frigate_auth
            or path in EXEMPT_PATHS
            or path.startswith(EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return

        if is_login_post:
            # /api/login is proxied (routes/proxy.py) and is how a caller gets
            # a credential, so it skips the validation below -- rate-limit it
            # here, before it reaches Frigate, and count it only if it fails.
            # Only this endpoint is rate-limited/counted (see LoginRateLimiter's
            # docstring for why owned-route 401s deliberately are not).
            limiter = LoginRateLimiter(
                app,
                settings.sidecar.login_rate_limit_attempts,
                settings.sidecar.login_rate_limit_window_s,
            )
            ip = _client_ip(scope)
            retry_after = limiter.retry_after(ip)
            if retry_after is not None:
                rl_response: Any = _rate_limit_response(retry_after, app)
                await rl_response(scope, receive, send)
                return

            status_holder: dict[str, int] = {}

            async def _send_wrapper(message: Any, _holder: dict[str, int] = status_holder) -> None:
                if message["type"] == "http.response.start":
                    _holder["status"] = message["status"]
                await send(message)

            await self.app(scope, receive, _send_wrapper)
            if status_holder.get("status", 200) >= 400:
                limiter.record_failure(ip)
            return

        cookie = ""
        authorization = ""
        for name, value in scope.get("headers", []):
            if name == b"cookie" and not cookie:
                cookie = value.decode("latin-1")
            elif name == b"authorization" and not authorization:
                authorization = value.decode("latin-1")

        # The Frigate session is always validated, so a stale Frigate JWT
        # triggers re-login — without that, proxied content (/api/* images,
        # live streams) would silently 401 while sidecar pages kept working.
        # A valid remember-me cookie ("stay signed in" at /login) does not
        # bypass that check; it buys a longer cache window, so a device that
        # ticked the box re-probes upstream every remember_cache_ttl_s instead
        # of every auth_cache_ttl_s. Chosen before the call rather than in a
        # branch around it, so an auth failure costs exactly one probe.
        remember = _cookie_value(cookie, REMEMBER_COOKIE)
        ttl_s = (
            settings.sidecar.remember_cache_ttl_s
            if remember and _remember_token_valid(app, remember)
            else settings.sidecar.auth_cache_ttl_s
        )

        try:
            await validate_frigate_session(
                app, cookie, ttl_s=ttl_s, authorization=authorization
            )
        except HTTPException as exc:
            if scope["type"] == "websocket":
                # Reject before the handshake completes; 1008 is "policy
                # violation", which is what a client sees for an auth failure.
                await send({"type": "websocket.close", "code": 1008})
                return
            # A browser navigating to a page gets sent to /login instead of a
            # bare JSON 401; API clients (the iOS app, curl) keep the JSON —
            # distinguished by the request Accept-ing text/html on a GET.
            if exc.status_code == 401 and scope.get("method") == "GET":
                accept = ""
                for name, value in scope.get("headers", []):
                    if name == b"accept":
                        accept = value.decode("latin-1")
                        break
                if "text/html" in accept:
                    next_path = path
                    if scope.get("query_string"):
                        next_path += "?" + scope["query_string"].decode("latin-1")
                    response: Any = RedirectResponse(
                        "/login?next=" + quote(next_path, safe=""), status_code=302,
                    )
                    await response(scope, receive, send)
                    return
            if exc.status_code == 401:
                error = ERR_UNAUTHORIZED
            elif str(exc.detail).startswith(ERR_PROXY_ORIGIN_UNPROTECTED):
                error = ERR_PROXY_ORIGIN_UNPROTECTED
            else:
                error = ERR_UPSTREAM_UNAVAILABLE
            body = {"error": error, "message": str(exc.detail)}
            response = JSONResponse(body, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
