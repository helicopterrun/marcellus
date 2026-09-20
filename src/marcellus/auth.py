"""Frigate-session gate for every endpoint the sidecar owns.

The sidecar has no user database and never holds a password: it validates the
client's own Frigate session cookie against Frigate's authenticated origin and
caches the pass for a short TTL. That is the same mechanism `/v1` already used
(docs/scrub-cache-and-proxy-spec.md §3.2 finding 5, option (a)); this module
generalises it so the triage UI, `/faces/captures`, `/analysis` and `/toybox` are
covered too. Those endpoints expose event history, face crops of identified
people, and label/promote writes with side effects on Frigate itself, so
leaving them open made the sidecar a way around Frigate's own auth.

Deliberately NOT gated here:

* the reverse-proxy catch-all -- Frigate authenticates that traffic itself and
  its 401/`WWW-Authenticate` challenge has to reach the client intact;
* `/v1/capabilities`, `/healthz`, `/version` -- reachability probes a client
  needs *before* it has a session, and `/static`;
* `/v1/push/thumbnail/` -- fetched by the iOS Notification Service Extension
  (docs/push-notifications.md), which has no Frigate session and cannot
  acquire one (it runs in its own short-lived, network-isolated sandbox).
  Safe to leave open: a handle is an opaque, unguessable, short-lived
  (`push.handle_ttl_s`/`situation_handle_ttl_s`) token that maps only to a
  pre-fetched thumbnail image, never to anything the sidecar or Frigate can
  be made to do -- the same trust model as a signed download link.

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
from starlette.routing import Match

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterable

    from fastapi import FastAPI
    from starlette.routing import BaseRoute

    from marcellus.config import Settings

logger = logging.getLogger(__name__)

ERR_UNAUTHORIZED = "unauthorized"
ERR_UPSTREAM_UNAVAILABLE = "upstream_unavailable"

# Reachability probes a client may need before it holds a Frigate session
# (and the login page itself, which exists to acquire one).
EXEMPT_PATHS = frozenset({"/healthz", "/version", "/v1/capabilities", "/login"})
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
    app: FastAPI, cookie: str, *, ttl_s: float | None = None
) -> None:
    """Raise HTTPException unless `cookie` is a live Frigate session.

    Validated against `frigate.proxy_base_url` -- the authenticated origin --
    because that is the one that can actually reject a bad cookie.

    `ttl_s` overrides how long a successful check is cached; callers holding a
    valid remember-me cookie pass the longer `remember_cache_ttl_s`. Defaults
    to `sidecar.auth_cache_ttl_s`.
    """
    from marcellus.frigate_api import get_async_client

    settings: Settings = app.state.settings
    if not cookie:
        raise HTTPException(status_code=401, detail="frigate session required")

    key = hashlib.sha256(cookie.encode()).hexdigest()
    now = time.time()
    expiry = _cache(app).get(key)
    if expiry is not None and expiry > now:
        return

    upstream = f"{settings.frigate.proxy_base_url.rstrip('/')}/api/version"
    client = get_async_client(app)
    try:
        resp = await client.get(upstream, headers={"cookie": cookie}, timeout=5.0)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"{ERR_UPSTREAM_UNAVAILABLE}: {exc}"
        ) from exc

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
    """Require a Frigate session on the sidecar's own routes.

    `owned_routes` is everything registered before the proxy catch-all, so a
    path the sidecar doesn't serve itself falls through to Frigate untouched.
    """

    def __init__(self, app: Any, *, owned_routes: Iterable[BaseRoute]) -> None:
        self.app = app
        self._owned = list(owned_routes)

    def _owns(self, scope: dict[str, Any]) -> bool:
        for route in self._owned:
            match, _ = route.matches(scope)
            # PARTIAL means the path matched but the method didn't: still ours,
            # and a 405 shouldn't be answered by Frigate either.
            if match in (Match.FULL, Match.PARTIAL):
                return True
        return False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        # WebSocket scopes are gated too. No sidecar-owned WS route exists today
        # -- `_owns` matches none of them, so every upgrade falls through to the
        # proxy and Frigate authenticates it -- but skipping the whole scope
        # type meant the first one added would have been unauthenticated by
        # default, with nothing in the code saying so.
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        app: FastAPI = scope["app"]
        settings: Settings = app.state.settings
        path: str = scope.get("path", "")
        is_login_post = (
            scope["type"] == "http" and scope.get("method") == "POST" and path == "/api/login"
        )
        owns = self._owns(scope)
        if (
            not settings.sidecar.require_frigate_auth
            or path in EXEMPT_PATHS
            or path.startswith(EXEMPT_PREFIXES)
            or (not owns and not is_login_post)
        ):
            await self.app(scope, receive, send)
            return

        if is_login_post:
            # /api/login is proxied, not owned by the sidecar (routes/proxy.py),
            # so it never goes through the cookie-validation flow below -- gate
            # it here, before it reaches Frigate, and count it only if it fails.
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
        for name, value in scope.get("headers", []):
            if name == b"cookie":
                cookie = value.decode("latin-1")
                break

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
            await validate_frigate_session(app, cookie, ttl_s=ttl_s)
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
            body = {
                "error": ERR_UNAUTHORIZED if exc.status_code == 401 else ERR_UPSTREAM_UNAVAILABLE,
                "message": str(exc.detail),
            }
            response = JSONResponse(body, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
