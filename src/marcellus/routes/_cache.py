"""A tiny TTL cache for the heavy analysis pages.

Those handlers (motion, score-histogram, zone-hits, fps-budget) recompute
365-day aggregations or probe Frigate live on every load. A refresh or a
back-navigation within the TTL serves the rendered HTML instantly instead.
Keyed by the full URL, so every distinct filter combination caches separately.
"""

from __future__ import annotations

import functools
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, Response

_MAX_ENTRIES = 64


_PageHandler = Callable[..., Response]


def ttl_page_cache(seconds: float = 60.0) -> Callable[[_PageHandler], _PageHandler]:
    def decorator(fn: _PageHandler) -> _PageHandler:
        cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

        @functools.wraps(fn)
        def wrapper(*args: Any, request: Request, **kwargs: Any) -> Response:
            key = str(request.url)
            now = time.monotonic()
            hit = cache.get(key)
            if hit is not None and now - hit[0] < seconds:
                cache.move_to_end(key)
                return HTMLResponse(hit[1])
            response = fn(*args, request=request, **kwargs)
            # TemplateResponse renders its body at construction; only cache
            # successes so an error page doesn't stick for the TTL. This
            # holds only because every decorated handler marks its own error
            # render non-200 (503 when Frigate/the DB is unreachable, 400 on
            # a bad query param) -- a handler that renders an error banner
            # but leaves the default 200 status would still get cached here.
            if getattr(response, "status_code", None) == 200:
                cache[key] = (now, bytes(response.body))
                cache.move_to_end(key)
                while len(cache) > _MAX_ENTRIES:
                    cache.popitem(last=False)
            return response

        return wrapper

    return decorator
