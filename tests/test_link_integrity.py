"""Link integrity: every href in a template must resolve to a real route."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.server import create_app

TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "marcellus" / "templates"
)

_HREF_RE = re.compile(r'<a\b[^>]*\bhref="([^"]*)"', re.IGNORECASE)
_ID_RE = re.compile(r'\bid="([^"]*)"')


@pytest.fixture
def app(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> FastAPI:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return create_app(settings)


def _api_routes(app: FastAPI) -> list[APIRoute]:
    """All APIRoutes, flattening included-router wrappers."""
    out: list[APIRoute] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, APIRoute):
            out.append(route)
            continue
        # FastAPI may wrap included routers (_IncludedRouter) instead of
        # flattening them into app.routes; reach through to the real router.
        inner = getattr(route, "original_router", None)
        stack.extend(getattr(inner, "routes", []) or getattr(route, "routes", []))
    return out


def test_template_links_resolve(app: FastAPI) -> None:
    routes = _api_routes(app)
    exact_paths = {r.path for r in routes}
    errors: list[str] = []

    tpl_paths = sorted(TEMPLATES_DIR.glob("*.html"))
    tpl_texts = {tpl: tpl.read_text() for tpl in tpl_paths}
    # base.html is a Jinja layout every page `{% extends %}`; a fragment link
    # defined there (e.g. the skip-to-content link) resolves against an id
    # supplied by whichever child template fills `{% block body %}` at render
    # time, so it can't be checked against base.html alone. It is checked
    # instead by test_layout_fragments_exist_in_every_page below, which
    # requires *every* extending template to supply the id -- pooling ids
    # across all templates would let one page satisfy the anchor for all of
    # them (which is how status.html shipped with a dead skip link).
    for tpl, text in tpl_texts.items():
        ids_in_file = set(_ID_RE.findall(text))

        for href in _HREF_RE.findall(text):
            if tpl.name == "base.html" and href.startswith("#"):
                continue  # covered by the layout-fragment test below
            if "{{" in href:
                continue
            if href.startswith("/static") or href.startswith("http"):
                continue

            if href.startswith("#"):
                anchor = href[1:]
                if anchor and anchor not in ids_in_file:
                    errors.append(f"{tpl.name}: fragment {href!r} has no matching id")
                continue

            path = href.split("?")[0].split("#")[0]
            if not path.startswith("/"):
                continue
            if path in exact_paths:
                continue
            if any(r.path_regex.match(path) for r in routes):
                continue
            errors.append(f"{tpl.name}: href {path!r} matches no route")

    assert not errors, "Dead links in templates:\n" + "\n".join(errors)


def test_layout_fragments_exist_in_every_page() -> None:
    """Every fragment link in base.html must resolve on every page.

    base.html's skip-to-content anchor targets an id that each child template
    supplies in its own `{% block body %}`. One template missing it is a dead
    anchor on that page only, which is invisible to a whole-corpus id scan --
    status.html shipped exactly that way.
    """
    base = (TEMPLATES_DIR / "base.html").read_text()
    fragments = {h[1:] for h in _HREF_RE.findall(base) if h.startswith("#") and len(h) > 1}
    assert fragments, "base.html declares no fragment links -- update this test"

    errors: list[str] = []
    for tpl in sorted(TEMPLATES_DIR.glob("*.html")):
        text = tpl.read_text()
        if 'extends "base.html"' not in text:
            continue
        ids = set(_ID_RE.findall(text))
        for frag in sorted(fragments):
            if frag not in ids:
                errors.append(f"{tpl.name}: no id={frag!r} for base.html's #{frag} link")

    assert not errors, "Layout fragment links with no target:\n" + "\n".join(errors)
