"""Media relay for external event sources.

One `httpx.AsyncClient` per source (separate from Frigate's pool) serves two
jobs: the service polls through `client()`, and `relay()` streams a stored
sighting's media (poster / still / clip) to the client without ever buffering
a whole clip. Upstream paths come from the stored sighting, never from client
input, and the upstream URL/host never appears in a response.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence

import httpx
from starlette.responses import JSONResponse, Response

from marcellus.external.types import ExternalEventSource
from marcellus.routes.proxy import _IDLE_CHUNK_TIMEOUT_S, _BoundedStreamingResponse

logger = logging.getLogger(__name__)

_REQ_FORWARD = ("range", "if-none-match", "if-modified-since")
_RESP_PASS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
    "cache-control",
)
_PASSTHROUGH_STATUS = frozenset({200, 206, 304, 416})


class SourceHttp:
    """Owns one AsyncClient per external source."""

    def __init__(
        self,
        sources: Sequence[ExternalEventSource],
        *,
        timeout_s: float | Mapping[str, float] = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        for source in sources:
            timeout = (
                timeout_s if isinstance(timeout_s, (int, float)) else timeout_s.get(source.id, 10.0)
            )
            self._clients[source.id] = httpx.AsyncClient(
                base_url=source.base_url,
                limits=httpx.Limits(
                    max_connections=source.max_connections,
                    max_keepalive_connections=source.max_connections,
                ),
                timeout=timeout,
                transport=transport,
            )

    def client(self, source_id: str) -> httpx.AsyncClient:
        return self._clients[source_id]

    async def relay(
        self, source_id: str, upstream_path: str, request_headers: Mapping[str, str]
    ) -> Response:
        if not upstream_path.startswith("/") or upstream_path.startswith("//"):
            raise ValueError("upstream_path must be a relative path starting with '/'")
        client = self._clients[source_id]
        lowered = {k.lower(): v for k, v in request_headers.items()}
        fwd = {k: lowered[k] for k in _REQ_FORWARD if k in lowered}
        fwd["accept-encoding"] = "identity"

        try:
            request = client.build_request("GET", upstream_path, headers=fwd)
            resp = await client.send(request, stream=True)
        except httpx.HTTPError:
            logger.warning("external media: source %s unavailable", source_id)
            return JSONResponse({"error": "upstream_unavailable"}, status_code=502)

        status = resp.status_code
        if status not in _PASSTHROUGH_STATUS:
            await resp.aclose()
            if status == 404:
                return JSONResponse({"error": "media_gone"}, status_code=404)
            if status >= 400:
                return JSONResponse({"error": "upstream_error", "status": status}, status_code=502)
            return JSONResponse({"error": "upstream_error", "status": status}, status_code=502)

        headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

        # Already-read responses (non-streaming transports) are buffered and
        # decoded; same handling as the Frigate proxy.
        buffered = getattr(resp, "is_stream_consumed", False)
        if buffered:
            headers.pop("content-length", None)

        async def body() -> AsyncIterator[bytes]:
            try:
                if buffered:
                    yield resp.content
                    return
                chunks = resp.aiter_raw()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            chunks.__anext__(), timeout=_IDLE_CHUNK_TIMEOUT_S
                        )
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, httpx.HTTPError):
                        break
                    yield chunk
            finally:
                try:
                    await resp.aclose()
                except Exception:
                    logger.debug("external media: aclose raised", exc_info=True)

        return _BoundedStreamingResponse(
            body(),
            status_code=status,
            headers=headers,
            media_type=resp.headers.get("content-type"),
            log_path=f"external/{source_id}",
        )

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
