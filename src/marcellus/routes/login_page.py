"""Login page: a browser hitting a gated sidecar page without a live Frigate
session is redirected here (`auth.FrigateAuthMiddleware`). The form posts the
credentials directly to Frigate's own `/api/login` *through the sidecar's
reverse proxy*, so Frigate's `Set-Cookie: frigate_token=...` lands on the
sidecar's origin and every gated page works from then on. The sidecar itself
never sees or stores the password — same trust model as the rest of
`auth.py`.

When the sidecar cannot reach Frigate's sign-in port because of a certificate
or scheme mistake in its own config (`frigate_tls.py`), the page says so
instead of a bare "login failed": a diagnosis already known renders with the
page, and a failed login asks `/login/diagnosis`, which probes the port at most
once per `_DIAGNOSIS_PROBE_INTERVAL_S`."""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from marcellus import frigate_tls
from marcellus.auth import REMEMBER_COOKIE, mint_remember_token

router = APIRouter(tags=["auth"])

# `/login/diagnosis` is reachable without a session, so its probe of Frigate is
# rate-limited per app: repeated calls inside the window reuse the last answer.
_DIAGNOSIS_PROBE_INTERVAL_S = 10.0
_DIAGNOSIS_PROBE_TIMEOUT_S = 3.0


def _diagnosis_context(diagnosis: str | None) -> dict[str, Any]:
    return {
        "frigate_diagnosis": diagnosis,
        "frigate_diagnosis_message": frigate_tls.login_message(diagnosis) if diagnosis else "",
    }


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request) -> Any:
    templates = request.app.state.templates
    from marcellus import __version__

    diagnosis = frigate_tls.monitor(request.app).current(frigate_tls.PROXY_ORIGIN)
    return templates.TemplateResponse(
        request, "login.html", {"version": __version__, **_diagnosis_context(diagnosis)}
    )


@router.get("/login/diagnosis")
async def login_diagnosis(request: Request) -> dict[str, Any]:
    """Why sign-in cannot work, when the cause is the sidecar's own Frigate
    URL or certificate setting: `{"diagnosis": "tls_untrusted" |
    "https_required" | null, "message": str}`. Probes the sign-in port's
    `/api/version` with no credential (rate-limited)."""
    from marcellus.frigate_api import get_async_client

    app = request.app
    now = time.monotonic()
    last = getattr(app.state, "login_diagnosis_probed_at", None)
    if last is None or now - last >= _DIAGNOSIS_PROBE_INTERVAL_S:
        app.state.login_diagnosis_probed_at = now
        url = app.state.settings.frigate.proxy_base_url.rstrip("/") + "/api/version"
        # Built by hand and sent with `send()` so the pooled client's cookie
        # jar is never merged in (as in `auth._check_anonymous_access`).
        req = httpx.Request(
            "GET", url, extensions={"timeout": httpx.Timeout(_DIAGNOSIS_PROBE_TIMEOUT_S).as_dict()}
        )
        try:
            resp = await get_async_client(app).send(req)
        except httpx.HTTPError as exc:
            frigate_tls.note_error(app, frigate_tls.PROXY_ORIGIN, url, exc)
        else:
            frigate_tls.note_response(app, frigate_tls.PROXY_ORIGIN, url, resp)
    diagnosis = frigate_tls.monitor(app).current(frigate_tls.PROXY_ORIGIN)
    context = _diagnosis_context(diagnosis)
    return {"diagnosis": diagnosis, "message": context["frigate_diagnosis_message"]}


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
