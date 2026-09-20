"""New UI surfaces: status dashboard, debug index, devices, zone-hits, scrub viewer."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.server import create_app

# Resolved off the imported package, so this checks whichever tree the suite is
# running against -- source under `pythonpath = ["src"]`, the wheel otherwise.
_PKG = Path(db.__file__).parent
TEMPLATES_DIR = _PKG / "templates"
JS_DIR = _PKG / "static" / "js"


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path) -> TestClient:
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",  # nothing listens: probes must degrade
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_status_page_is_home(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "Status" in r.text
    # Frigate probe must degrade to "unreachable", not 500.
    assert "unreachable" in r.text


def test_status_json_shape(client: TestClient) -> None:
    r = client.get("/status.json")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    assert body["frigate"]["reachable"] is False
    assert body["scrub"]["enabled"] is False
    assert body["push"]["enabled"] is False
    assert body["push"]["device_count"] == 0
    assert "sizes" in body


def test_debug_page(client: TestClient) -> None:
    r = client.get("/debug")
    assert r.status_code == 200
    # Utilities (toybox/docs/version links) live only in Settings › About now
    # — debug.html just points there instead of duplicating the list.
    assert "/settings#about" in r.text
    assert "Capabilities" in r.text


def test_toybox_not_in_main_nav(client: TestClient) -> None:
    r = client.get("/")
    assert 'class="page-link"' not in r.text or "Toybox" not in r.text


def test_zone_hits_page(client: TestClient) -> None:
    r = client.get("/zone-hits", params={"days": 7})
    assert r.status_code == 200


def test_devices_page_empty(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    assert "no devices registered" in r.text


def test_settings_page_renders_with_off_cell(client: TestClient) -> None:
    """A routing cell switched off makes the ladder answer "suppressed" for
    the notification examples; that level is outside LEVELS and used to
    KeyError the page (prod 2026-09-20)."""
    from marcellus.push import ladder_policy

    before = set(ladder_policy.OFF_CELLS)
    ladder_policy.set_off_cells({("animal", "yard"), ("stranger", "doors")})
    try:
        r = client.get("/settings")
    finally:
        ladder_policy.set_off_cells(before)
    assert r.status_code == 200
    assert "Off" in r.text


def test_devices_page_lists_registered(
    client: TestClient, sidecar_db_path: Path
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        store.upsert_device(
            conn,
            apns_token="ab" * 16,
            bundle_id="com.example.elsinore",
            environment="sandbox",
            app_version="1.0",
            cameras=["doorbell"],
            min_severity="alert",
        )
        conn.commit()
    finally:
        conn.close()
    r = client.get("/settings")
    assert r.status_code == 200
    assert "doorbell" in r.text
    assert "btn-primary" in r.text


def test_scrub_viewer_disabled(client: TestClient) -> None:
    r = client.get("/scrub")
    assert r.status_code == 200
    assert "disabled" in r.text


def test_scrub_page_carries_every_mount_point_the_reel_binds_to() -> None:
    """The template/JS contract, which fails silently in both directions.

    `scrub.js` returns early if `#sv-reel` is missing and throws on the rest,
    and neither shows up in a server-side test -- the page still renders 200
    with a dead canvas. The previous strip left exactly that trap behind: it
    bound `#sv-timeline`, an id no longer in the template.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR), autoescape=select_autoescape(["html"])
    )
    html = env.get_template("scrub.html").render(
        cameras=["gate-face"], camera="gate-face", enabled=True, counts={}, asset_v="t",
        request=None,
    )
    for element_id in (
        "sv-camera", "sv-frame", "sv-moment", "sv-reel",
        "sv-rungs", "sv-lanes", "sv-status", "sv-frigate-link", "sv-clock",
    ):
        assert f'id="{element_id}"' in html, element_id

    js = (JS_DIR / "scrub.js").read_text()
    for element_id in ("sv-reel", "sv-rungs", "sv-lanes", "sv-moment"):
        assert f'getElementById("{element_id}")' in js, element_id
    # The strip's id must be gone from both sides, not just one.
    assert "sv-timeline" not in html and "sv-timeline" not in js


def test_triage_moved_to_slash_triage(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    assert "filters" in r.text


def test_triage_q_filters_by_camera_label_or_sub_label(client: TestClient) -> None:
    # Header search box's q=, same substring match as /v1/events/search.
    # e3 is the only alley-east event, seeded with days=14 covered by default.
    r = client.get("/triage", params={"q": "alley-east"})
    assert r.status_code == 200
    assert "1 matching event" in r.text

    r = client.get("/triage", params={"q": "no-such-camera-or-label"})
    assert r.status_code == 200
    assert "0 matching event" in r.text


def test_triage_active_query_shows_removable_chip(client: TestClient) -> None:
    r = client.get("/triage", params={"q": "alley"})
    assert r.status_code == 200
    assert "“alley”" in r.text
    # The clear link drops q but keeps the other (default) filters.
    assert 'href="/triage?camera=&amp;label=&amp;triage=any' in r.text

    r = client.get("/triage")
    assert "Clear search" not in r.text


# ---- Frigate DB missing on this instance (dev host without the SQLite file) ----


@pytest.fixture
def db_less_client(tmp_path: Path, sidecar_db_path: Path) -> TestClient:
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://127.0.0.1:1",
            db_path=tmp_path / "nope" / "frigate.db",  # deliberately absent
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_triage_degrades_without_frigate_db(db_less_client: TestClient) -> None:
    r = db_less_client.get("/triage", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert "database isn't available on this instance" in r.text
    # The shared nav still renders, so the page is a dead end, not a trap.
    assert 'class="page-link active"' in r.text


def test_analysis_api_degrades_without_frigate_db(db_less_client: TestClient) -> None:
    r = db_less_client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 503
    assert "Frigate DB not found" in r.json()["detail"]["message"]


def test_settings_page_unifies_surfaces(client: TestClient) -> None:
    r = client.get("/settings")
    assert r.status_code == 200
    # One page, all the sections.
    for anchor in ("Zones &amp; routing", "Camera neighbors", "Push &amp; devices",
                   "Data", "About"):
        assert anchor in r.text, anchor
    # Zones editor ids so zones.js drives the section unchanged.
    for element_id in ("zones-list", "neighbors-list", "save-btn",
                       "export-btn", "import-file"):
        assert element_id in r.text, element_id

