"""Accessors for the per-app objects `server.create_app` hangs on `app.state`.

Route modules used to each carry their own copy of these two-liners; keep
the one definition here so a rename of the state attribute is one edit.
"""

from __future__ import annotations

from typing import cast

from fastapi import Request
from fastapi.templating import Jinja2Templates

from marcellus.config import Settings


def settings_of(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def templates_of(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)
