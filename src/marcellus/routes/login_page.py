"""Login page: a browser hitting a gated sidecar page without a live Frigate
session is redirected here (`auth.FrigateAuthMiddleware`). The form posts the
credentials directly to Frigate's own `/api/login` *through the sidecar's
reverse proxy*, so Frigate's `Set-Cookie: frigate_token=...` lands on the
sidecar's origin and every gated page works from then on. The sidecar itself
never sees or stores the password — same trust model as the rest of
`auth.py`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from marcellus.auth import REMEMBER_COOKIE, mint_remember_token

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login/remember", status_code=204)
def login_remember(request: Request, response: Response) -> Response:
    """Mint the "stay signed in" cookie.

    Gated by FrigateAuthMiddleware like every owned route, so it only works
    for a caller who *just* proved a live Frigate session — the cookie is a
    signed expiry token, no credentials involved.
    """
    ttl = request.app.state.settings.sidecar.remember_ttl_s
    response.set_cookie(
        REMEMBER_COOKIE,
        mint_remember_token(request.app, ttl),
        max_age=int(ttl),
        httponly=True,
        samesite="lax",
        path="/",
    )
    response.status_code = 204
    return response
