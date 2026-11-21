"""Response compression that leaves media alone.

Starlette's `GZipMiddleware` decides what it skips by version: recent releases
pass `video/*`, `audio/*`, common image types and 206 responses through, older
ones (still inside this package's FastAPI range) only `text/event-stream`.
Compressing an MP4 fragment or a JPEG costs CPU on the event loop every proxied
recorded-video request waits on, and saves nothing. This middleware makes the
choice here, the same on every Starlette:

* Paths under `SKIP_PATH_PREFIXES` (Frigate's recorded and live video, served
  through the proxy) are never touched.
* A response is passed through unchanged when it already has a
  `Content-Encoding`, is a 206, or its media type is in `SKIP_CONTENT_TYPES`
  (`type/*` entries match a whole family; `image/svg+xml` is text and still
  compresses).
* Everything else (HTML, JSON, CSS, JS) of at least `minimum_size` bytes is
  gzipped at `compresslevel` (6 by default: within a few percent of 9's size
  for this kind of text, for a fraction of the CPU).
"""

from __future__ import annotations

import asyncio
import zlib
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SKIP_PATH_PREFIXES = ("/vod/", "/live/")

SKIP_CONTENT_TYPES = frozenset(
    {
        "video/*",
        "audio/*",
        "image/*",
        "font/woff",
        "font/woff2",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/octet-stream",
        "application/zip",
        "application/gzip",
        "application/x-gzip",
        "text/event-stream",
    }
)

#: Media types under a skipped family that are text and do compress.
_COMPRESSIBLE_EXCEPTIONS = frozenset({"image/svg+xml"})

#: Single bodies at least this large are compressed on a worker thread.
_THREAD_MINIMUM_SIZE = 128 * 1024


def skips_content_type(content_type: str) -> bool:
    media_type = content_type.partition(";")[0].strip().lower()
    if not media_type or media_type in _COMPRESSIBLE_EXCEPTIONS:
        return False
    family = media_type.partition("/")[0] + "/*"
    return media_type in SKIP_CONTENT_TYPES or family in SKIP_CONTENT_TYPES


def _gzip(body: bytes, level: int) -> bytes:
    compressor = zlib.compressobj(level, zlib.DEFLATED, 31)
    return compressor.compress(body) + compressor.flush()


class MediaSafeGZipMiddleware:
    def __init__(self, app: ASGIApp, minimum_size: int = 1024, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or "gzip" not in Headers(scope=scope).get("accept-encoding", "")
            or str(scope.get("path", "")).startswith(SKIP_PATH_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        await _GZipResponder(send, self.minimum_size, self.compresslevel).run(
            self.app, scope, receive
        )


class _GZipResponder:
    def __init__(self, send: Send, minimum_size: int, level: int) -> None:
        self.send = send
        self.minimum_size = minimum_size
        self.level = level
        self.start: Message | None = None
        # None until the first body message decides; then "pass" or "gzip".
        self.mode: str | None = None
        self.compressor: Any = None

    async def run(self, app: ASGIApp, scope: Scope, receive: Receive) -> None:
        await app(scope, receive, self.on_message)

    async def on_message(self, message: Message) -> None:
        kind = message["type"]
        if kind == "http.response.start":
            self.start = message
            headers = Headers(raw=message["headers"])
            if (
                "content-encoding" in headers
                or message["status"] == 206
                or skips_content_type(headers.get("content-type", ""))
            ):
                self.mode = "pass"
                await self.send(message)
            return
        if kind != "http.response.body":
            # `http.response.pathsend` (a file the server sends itself) and
            # anything else that isn't a body chunk: never compressed. If the
            # start is still held, it goes out first, unchanged.
            if self.mode is None and self.start is not None:
                self.mode = "pass"
                await self.send(self.start)
            await self.send(message)
            return
        if self.mode == "pass":
            await self.send(message)
            return

        body = message.get("body", b"")
        more_body = message.get("more_body", False)
        if self.mode is None:
            assert self.start is not None
            if not more_body:
                # The whole response in one message: compress it if it's
                # worth it, framed with its real length.
                self.mode = "pass"
                if len(body) < self.minimum_size:
                    await self.send(self.start)
                    await self.send(message)
                    return
                if len(body) >= _THREAD_MINIMUM_SIZE:
                    compressed = await asyncio.to_thread(_gzip, body, self.level)
                else:
                    compressed = _gzip(body, self.level)
                headers = MutableHeaders(raw=self.start["headers"])
                headers["Content-Encoding"] = "gzip"
                headers["Content-Length"] = str(len(compressed))
                headers.add_vary_header("Accept-Encoding")
                await self.send(self.start)
                await self.send({"type": "http.response.body", "body": compressed})
                return
            # Streaming: compress as it goes; the length isn't known.
            self.mode = "gzip"
            self.compressor = zlib.compressobj(self.level, zlib.DEFLATED, 31)
            headers = MutableHeaders(raw=self.start["headers"])
            headers["Content-Encoding"] = "gzip"
            del headers["Content-Length"]
            headers.add_vary_header("Accept-Encoding")
            await self.send(self.start)

        chunk = self.compressor.compress(body)
        if not more_body:
            chunk += self.compressor.flush()
        await self.send({"type": "http.response.body", "body": chunk, "more_body": more_body})
