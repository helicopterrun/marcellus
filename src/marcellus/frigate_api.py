"""Thin client over Frigate's HTTP API.

The analysis modules that need live signal (detector inference times,
per-camera observed fps, recordings summaries) go through here so there's
one place to set timeouts / headers / base URL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

import httpx

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI


class FrigateAPIError(RuntimeError):
    pass


# Finite default for the shared client -- every current caller (the `/v1`
# motion fetch, the session check, the config-refresh and thumbnail proxies)
# already passes its own short `timeout=` per request, so this is only a
# backstop for a caller that forgets to.
#
# Sized for a single-household LAN NVR: a handful of simultaneous API calls
# (motion fetch, session check, config refresh). No `httpx.Limits` before this
# meant an unbounded connection pool. Streaming media (VOD/live) used to share
# this same pool -- a handful of paused live-view tabs pinned every socket in
# it, and every other Frigate-backed route (login, /api/version) then hung for
# the full pool timeout before 502ing (2026-09-10 incident). Long-lived
# proxied streams now go through `get_stream_client()`'s separate pool
# instead; this one is API-calls-only and can stay small and short-timeout.
_DEFAULT_LIMITS = httpx.Limits(
    max_connections=20, max_keepalive_connections=10, keepalive_expiry=15.0
)
_DEFAULT_TIMEOUT = httpx.Timeout(15.0, pool=5.0)

# The passthrough proxy's pool (routes/proxy.py): sized for several concurrent
# VOD/live viewers. `read=None` because a stalled chunk is bounded by the
# proxy's own idle-chunk watchdog and stall/duration timeouts, not by httpx;
# `pool=5.0` so a request that can't get a connection fails fast with
# `httpx.PoolTimeout` (mapped to a 503) instead of piling up behind the 30s
# default pool wait.
_STREAM_LIMITS = httpx.Limits(
    max_connections=64, max_keepalive_connections=16, keepalive_expiry=15.0
)
_STREAM_TIMEOUT = httpx.Timeout(30.0, read=None, pool=5.0)


def get_async_client(app: FastAPI) -> httpx.AsyncClient:
    """One pooled AsyncClient per app, created lazily and closed on shutdown.

    Used by the `/v1` motion fetch, the session check, and other short
    Frigate API calls. A fresh client per request threw away keep-alive to
    Frigate and paid a new connection setup on every call.
    """
    client = getattr(app.state, "http_client", None)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT, limits=_DEFAULT_LIMITS, follow_redirects=False
        )
        app.state.http_client = client
    return client


def get_stream_client(app: FastAPI) -> httpx.AsyncClient:
    """The separate pooled AsyncClient for the passthrough media proxy.

    Kept apart from `get_async_client()`'s pool so long-lived VOD/live
    streams (held open for as long as a paused player keeps them, previously
    hours) can never starve the short API calls that share a client with
    login and health checks -- see the module docstring above.
    """
    client = getattr(app.state, "stream_http_client", None)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(
            timeout=_STREAM_TIMEOUT, limits=_STREAM_LIMITS, follow_redirects=False
        )
        app.state.stream_http_client = client
    return client


def pool_stats(client: httpx.AsyncClient) -> dict[str, int]:
    """Best-effort connection-pool counts for diagnostics (503 logs, healthz).

    Reaches into httpx's private transport internals, so any shape change on
    an httpx upgrade just degrades this to `{}` rather than breaking the
    caller.
    """
    try:
        connections = client._transport._pool.connections  # type: ignore[attr-defined]
        total = len(connections)
        idle = sum(1 for c in connections if c.is_idle())
        return {"connections": total, "active": total - idle, "idle": idle}
    except Exception:
        return {}


async def async_activity_motion(
    client: httpx.AsyncClient,
    base_url: str,
    camera: str,
    start: float,
    end: float,
    scale: float,
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Async twin of `FrigateClient.activity_motion`.

    `/v1/motion` and `/v1/reel` are async handlers, so using the sync client
    there blocked the event loop -- and with it every other request -- for the
    whole upstream round-trip.
    """
    url = f"{base_url.rstrip('/')}/api/review/activity/motion"
    params: dict[str, str | float] = {
        "cameras": camera, "after": start, "before": end, "scale": scale,
    }
    try:
        r = await client.get(url, params=params, timeout=timeout)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise FrigateAPIError(f"GET {url}: {exc}") from exc
    data = r.json()
    return data if isinstance(data, list) else []


class FrigatePlusError(RuntimeError):
    """A Plus-submission call returned a non-200 success=False response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FrigateClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Plus uploads can be slow (snapshot read + remote POST to plus.frigate.video),
        # so allow a longer timeout for those specifically.
        self._client = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=20, max_keepalive_connections=10, keepalive_expiry=15.0
            ),
        )
        self._plus_client = httpx.Client(timeout=30.0)

    def __enter__(self) -> FrigateClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._plus_client.close()

    def _get_json(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        return r.json()

    def stats(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._get_json("/api/stats"))

    def config(self) -> dict[str, Any]:
        return cast("dict[str, Any]", self._get_json("/api/config"))

    def recordings_summary(self, camera: str) -> list[dict[str, Any]]:
        return cast("list[dict[str, Any]]", self._get_json(f"/api/{camera}/recordings/summary"))

    def event_thumbnail(self, event_id: str, *, timeout: float = 10.0) -> tuple[bytes | None, int]:
        """An event's stored thumbnail JPEG, or ``(None, 404)`` when it has none.

        Served by the sidecar (not the browser proxy) because Frigate's nginx
        401s proxied `/api/events/...` image requests when Frigate auth is on --
        the sidecar's own connection to `frigate.base_url` is the authorized
        path, exactly as with `recording_snapshot`.
        """
        url = f"{self.base_url}/api/events/{quote(event_id, safe='')}/thumbnail.jpg"
        try:
            r = self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        if r.status_code == 404:
            return (None, 404)
        if r.status_code != 200:
            raise FrigateAPIError(f"GET {url}: HTTP {r.status_code}")
        return (r.content, 200)

    def event_snapshot_jpeg(
        self, event_id: str, *, height: int = 480, bbox: bool = True, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        """The event's full-frame snapshot with the bounding box drawn.

        Same scene geometry as `recording_snapshot`, which is what makes it the
        right reference image for visual clock-offset calibration -- the tiny
        detect-stream thumbnail crop is not comparable to a full frame. Same
        sidecar-authorized posture as `event_thumbnail` (Frigate's nginx 401s
        this path through the browser proxy)."""
        url = (
            f"{self.base_url}/api/events/{quote(event_id, safe='')}/snapshot.jpg"
            f"?bbox={1 if bbox else 0}&height={height}"
        )
        try:
            r = self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        if r.status_code == 404:
            return (None, 404)
        if r.status_code != 200:
            raise FrigateAPIError(f"GET {url}: HTTP {r.status_code}")
        return (r.content, 200)

    def set_annotation_offset(self, camera: str, offset_ms: int) -> None:
        """Write `cameras.<camera>.detect.annotation_offset` into Frigate's config.

        PUT /api/config/set (verified against 0.17's OpenAPI: body is
        `{requires_restart, config_data}` with config_data merged into the
        config file — the same call Frigate's own UI makes for config edits).
        Marks a restart required; actually restarting is `restart()`, kept
        separate so a caller can batch writes before one restart.
        """
        url = f"{self.base_url}/api/config/set"
        body = {
            "requires_restart": 1,
            "config_data": {
                "cameras": {camera: {"detect": {"annotation_offset": int(offset_ms)}}}
            },
        }
        try:
            r = self._client.put(url, json=body, timeout=15.0)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"PUT {url}: {exc}") from exc
        if r.status_code != 200:
            raise FrigateAPIError(f"PUT {url}: HTTP {r.status_code} {r.text[:200]}")

    def restart(self) -> None:
        """POST /api/restart — restart the Frigate process so config edits
        (annotation_offset included) take effect in the event pipeline."""
        url = f"{self.base_url}/api/restart"
        try:
            r = self._client.post(url, timeout=15.0)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"POST {url}: {exc}") from exc
        if r.status_code != 200:
            raise FrigateAPIError(f"POST {url}: HTTP {r.status_code}")

    def recording_snapshot(
        self, camera: str, ts: float, *, timeout: float = 15.0
    ) -> tuple[bytes | None, int]:
        """Full-resolution main-stream frame at wall-clock `ts`.

        Returns ``(jpeg_bytes, http_status)``; bytes is None when Frigate has no
        recording covering that instant (404).

        GET /api/{camera}/recordings/{ts:.3f}/snapshot.jpg serves a frame cut
        straight out of the recording -- 2560x1440 / ~330 KB / ~0.45s on
        gate-face here. Recordings are the MAIN stream, so this is full
        resolution regardless of what the detect stream runs at.

        A 404 is an expected, routine outcome, not an error: the endpoint 404s
        until the segment covering `ts` has been COMMITTED, and segments commit
        at their end (measured publish lag 5.4-9.4s per camera). Callers must
        wait out that lag before treating a 404 as terminal. Anything else
        non-2xx raises, so a broken upstream is retried rather than recorded as
        "no recording exists".
        """
        url = f"{self.base_url}/api/{quote(camera, safe='')}/recordings/{ts:.3f}/snapshot.jpg"
        try:
            r = self._client.get(url, timeout=timeout)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        if r.status_code == 404:
            return (None, 404)
        if r.status_code != 200:
            raise FrigateAPIError(f"GET {url}: HTTP {r.status_code}")
        return (r.content, 200)

    def set_sub_label(self, event_id: str, sub_label: str, *, score: float | None = None) -> None:
        """POST /api/events/{id}/sub_label — write an event's sub_label.

        The sidecar is the sole sub_label author on enrolled cameras (Frigate's
        own face recognition is disabled there), so there is no writer to race.
        Frigate caps sub_label at 100 chars; `subLabelScore` is optional 0..1.
        """
        url = f"{self.base_url}/api/events/{quote(event_id, safe='')}/sub_label"
        body: dict[str, Any] = {"subLabel": sub_label[:100]}
        if score is not None:
            body["subLabelScore"] = round(max(0.0, min(1.0, score)), 3)
        try:
            r = self._client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"POST {url}: {exc}") from exc
        if r.status_code not in (200, 201):
            raise FrigateAPIError(f"POST {url}: HTTP {r.status_code}")

    def train_face(self, name: str, training_file: str) -> str:
        """Promote a `train/` crop into a named library via Frigate's API.

        Calls POST /api/faces/train/{name}/classify, which `shutil.move`s the
        crop out of train/ into the {name}/ dir and clears the classifier so the
        new image is picked up. Raises FrigateAPIError on any non-success.

        `name` is percent-encoded with no safe characters: unescaped, a name
        containing `../` or `?` rewrote the whole upstream path (httpx
        normalises dot segments and honours a query separator), which turned
        this into an arbitrary-endpoint POST against Frigate's unauthenticated
        origin.
        """
        url = f"{self.base_url}/api/faces/train/{quote(name, safe='')}/classify"
        try:
            r = self._client.post(url, json={"training_file": training_file})
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"POST {url}: {exc}") from exc
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code == 200 and payload.get("success", True):
            return str(payload.get("message") or "ok")
        msg = payload.get("message") or f"HTTP {r.status_code}"
        raise FrigateAPIError(f"train_face({name}, {training_file}): {msg}")

    def activity_motion(
        self, camera: str, start: float, end: float, scale: float
    ) -> list[dict[str, Any]]:
        """Re-fetch of `/api/review/activity/motion` (docs spec §4.6) --
        Frigate's own motion-activity series, normalised 0-100, ~1s
        resolution. Caller aggregates/zero-fills; this is a thin passthrough
        with our own timeout discipline.
        """
        url = f"{self.base_url}/api/review/activity/motion"
        params: dict[str, str | float] = {
            "cameras": camera, "after": start, "before": end, "scale": scale,
        }
        try:
            r = self._client.get(url, params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        data = r.json()
        return data if isinstance(data, list) else []

    def plus_enabled(self) -> bool:
        """Best-effort: returns True if Frigate reports a configured Plus API key.

        Reads `config.plus.enabled` from `/api/config`. Returns False if the
        config can't be fetched (Frigate down, network error) so the UI hides
        the Plus controls gracefully instead of showing a button that errors.
        """
        try:
            return bool(self.config().get("plus", {}).get("enabled"))
        except FrigateAPIError:
            return False

    def submit_plus(self, event_id: str, *, include_annotation: bool = True) -> str:
        """Submit an event to Frigate+ as a true positive. Returns the plus_id.

        `include_annotation=True` also uploads the bounding box with the event's
        label, so the user doesn't have to draw it on the Plus side. Raises
        FrigatePlusError on any non-success response.
        """
        url = f"{self.base_url}/api/events/{event_id}/plus"
        body: dict[str, Any] = {}
        if include_annotation:
            body["include_annotation"] = 1
        try:
            r = self._plus_client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise FrigatePlusError(f"POST {url}: {exc}", status_code=0) from exc
        return _parse_plus_response(r, "submit_plus")

    def submit_false_positive(self, event_id: str) -> str:
        """Submit an event as a false positive. Returns the plus_id.

        Frigate's endpoint is PUT (not POST) and internally calls /plus first
        if the event hasn't been submitted yet, so we don't need a two-step
        dance on this side.
        """
        url = f"{self.base_url}/api/events/{event_id}/false_positive"
        try:
            r = self._plus_client.put(url)
        except httpx.HTTPError as exc:
            raise FrigatePlusError(f"PUT {url}: {exc}", status_code=0) from exc
        return _parse_plus_response(r, "submit_false_positive")


def _parse_plus_response(r: httpx.Response, op: str) -> str:
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code == 200 and payload.get("success"):
        plus_id = payload.get("plus_id")
        if not plus_id:
            raise FrigatePlusError(f"{op}: 200 OK but no plus_id in response", r.status_code)
        return str(plus_id)
    msg = payload.get("message") or f"HTTP {r.status_code}"
    raise FrigatePlusError(f"{op}: {msg}", r.status_code)
