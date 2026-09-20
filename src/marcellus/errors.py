"""Shared helper for the API's error-response envelope.

Every 4xx/5xx JSON body the app returns is expected to look like
``{"detail": {"error": <snake_case code>, "message": <human text>}}``. Use
`error_detail()` when raising `fastapi.HTTPException` so all routes agree on
the shape.
"""

from __future__ import annotations


def error_detail(code: str, message: str) -> dict[str, str]:
    """Build the ``detail`` payload for an `HTTPException`.

    ``code`` is a stable, snake_case machine-readable identifier (e.g.
    ``"not_found"``, ``"invalid_range"``); ``message`` is the human-readable
    text already used at the call site (kept unchanged when converting an
    existing bare-string `HTTPException`).
    """
    return {"error": code, "message": message}
