"""New UI surfaces: status dashboard, debug index, devices, zone-hits, nav."""

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


def test_settings_page_has_tuning_and_config_anchors(client: TestClient) -> None:
    """Part C: the Tuning + Effective configuration sections replaced the
    old read-only Services block, and the nav links added in Part B
    (`#tuning`, `#config`) must resolve to real ids."""
    r = client.get("/settings")
    assert r.status_code == 200
    assert 'id="tuning"' in r.text
    assert 'id="config"' in r.text
    assert 'id="tuning-sections"' in r.text
    assert 'id="tuning-save"' in r.text
    assert 'id="config-table"' in r.text
    assert "/static/js/tuning.js" in r.text
    # The old read-only Services block is gone.
    assert 'id="services"' not in r.text


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
    assert "Ignore" in r.text


def test_ladder_matrix_prefers_ladder_and_falls_back_to_outcomes(monkeypatch) -> None:
    from marcellus.routes import settings_page

    base = {"zone_overrides": {"yard_z": {"person": "weird"}}}
    ladder = {"person": {"street": "ignore", "yard": "urgent", "doors": "glance",
                         "off_limits": "mystery"}}
    monkeypatch.setattr(settings_page.policy_settings, "get_active",
                        lambda: {**base, "ladder": ladder})
    m = settings_page._ladder_matrix()
    assert m["places"] == ["Public", "Grounds", "Entrances", "Restricted"]
    assert [c["label"] for c in m["rows"][0]["cells"]] == [
        "Ignore", "Urgent", "Glance", "mystery"]
    assert m["overrides"][0]["label"] == "weird"

    legacy = {"person": {"street": "off", "yard": "glance", "private": "alarm",
                         "doors": "notify", "off_limits": "log"}}
    monkeypatch.setattr(settings_page.policy_settings, "get_active",
                        lambda: {**base, "outcomes": legacy})
    cells = settings_page._ladder_matrix()["rows"][0]["cells"]
    assert [c["label"] for c in cells] == ["Ignore", "Urgent", "Quiet", "Ignore"]


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


def test_scrub_page_redirects_to_status(client: TestClient) -> None:
    r = client.get("/scrub", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/"


def test_nav_has_tools_dropdown_and_no_scrub_link(client: TestClient) -> None:
    html = client.get("/").text
    assert 'href="/scrub"' not in html
    nav = html[html.index('<nav class="pages"') : html.index("</header>")]
    tools = nav[nav.index(">Tools</summary>") :]
    # Tuning is one entry; its five pages are tabs under /tuning.
    for href in ("/tuning", "/wildlife", "/replay", "/guide", "/debug"):
        assert f'href="{href}"' in tools, href
    for href in ("/score-histogram", "/motion", "/fps-budget", "/zone-hits", "/placement"):
        assert f'href="{href}"' not in tools, href
    assert ">Tuning</summary>" not in html
    tabbar = html[html.index('<nav class="tabbar"') : html.index('class="more-backdrop"')]
    assert 'href="/encounters"' in tabbar
    assert "i-scrub" not in html
    # Phone More sheet: one Tools group, no separate Tuning group.
    sheet = html[html.index('<nav class="more-sheet"') : html.index('class="search-backdrop"')]
    assert '>Tools</div>' in sheet and '>Tuning</div>' not in sheet
    for href in ("/tuning", "/wildlife", "/replay", "/guide", "/debug"):
        assert f'href="{href}"' in sheet, href
    assert 'href="/score-histogram"' not in sheet


def test_tuning_landing_lists_the_five_pages(client: TestClient) -> None:
    r = client.get("/tuning")
    assert r.status_code == 200
    assert "<h1" in r.text and "Tuning" in r.text
    body = r.text[r.text.index('<main id="main-content"') :]
    for href in ("/score-histogram", "/motion", "/fps-budget", "/zone-hits", "/placement"):
        assert f'class="tuning-name" href="{href}"' in body, href
    assert "/wildlife" not in body  # Wildlife is a Tools page, not a tuning tab
    assert 'aria-label="Tuning"' in body  # the tab row


@pytest.mark.parametrize(
    ("path", "label"),
    [
        ("/tuning", "Overview"),
        ("/score-histogram", "Scores"),
        ("/motion", "Motion"),
        ("/fps-budget", "FPS budget"),
        ("/zone-hits", "Zone hits"),
        ("/placement", "Placement"),
    ],
)
def test_tuning_tab_row_marks_current_page(client: TestClient, path: str, label: str) -> None:
    html = client.get(path).text
    tabs = html[html.index('<nav class="pp-tabs" aria-label="Tuning">') :]
    tabs = tabs[: tabs.index("</nav>")]
    assert tabs.count('aria-current="page"') == 1
    assert f'class="pp-tab active" aria-current="page">{label}</a>' in tabs
    assert "Wildlife" not in tabs


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



_TITLED_PAGES = [
    ("/", "Status"),
    ("/cameras", "Cameras"),
    ("/triage", "All events"),
    ("/encounters", "Encounters"),
    ("/faces/captures", "Face captures"),
    ("/enrich/clusters", "Identities"),
    ("/enrich/accuracy", "Face accuracy"),
    ("/score-histogram", "Scores"),
    ("/motion", "Motion"),
    ("/fps-budget", "FPS budget"),
    ("/zone-hits", "Zone hits"),
    ("/placement", "Placement"),
    ("/wildlife", "Wildlife"),
    ("/settings", "Settings"),
    ("/replay", "Push replay"),
    ("/debug", "Debug"),
    ("/inbox/coverage", "Inbox coverage"),
]


@pytest.mark.parametrize(("path", "title"), _TITLED_PAGES)
def test_titled_pages_render_page_title(client: TestClient, path: str, title: str) -> None:
    r = client.get(path)
    # /fps-budget answers 503 (still rendering the page) when Frigate is down.
    assert r.status_code == (503 if path == "/fps-budget" else 200)
    assert r.text.count('class="page-title"') == 1
    assert f'<h1 class="page-title">{title}</h1>' in r.text


@pytest.mark.parametrize("path", ["/inbox", "/map", "/guide", "/toybox"])
def test_exempt_pages_have_no_page_title(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert 'class="page-title"' not in r.text


_SETTINGS_PANELS = [
    "zones", "routing", "notifications", "cameras", "alignment",
    "push", "tuning", "config", "data", "about",
]


def test_settings_page_has_tab_row_and_all_panels(client: TestClient) -> None:
    """Settings shows one panel at a time (settings_tabs.js), but the server
    renders every panel and a plain-anchor tab per panel, so the page still
    works without JS and `/settings#<panel>` deep links resolve."""
    import re

    r = client.get("/settings")
    assert r.status_code == 200
    html = r.text
    assert 'id="settings-nav"' in html
    assert "/static/js/settings_tabs.js" in html
    for pid in _SETTINGS_PANELS:
        assert f'href="#{pid}"' in html, pid
        assert re.search(rf'<section id="{pid}"[^>]*data-settings-panel', html), pid
    assert html.count("data-settings-panel") == len(_SETTINGS_PANELS)
    # Help folds into About rather than being its own (nav-less) section.
    about = html[html.index('<section id="about"'):]
    assert 'id="help"' in about
    # Ids are unique: the tuning "push" section must not collide with the
    # Push panel.
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_settings_tuning_section_ids_sit_inside_tuning_panel(client: TestClient) -> None:
    html = client.get("/settings").text
    start = html.index('<section id="tuning"')
    end = html.index('<section id="config"')
    tuning_panel = html[start:end]
    # `/settings#encounters` (encounters.html) targets a Tuning section.
    assert 'id="encounters"' in tuning_panel
    assert 'id="tuning-push"' in tuning_panel
    assert 'id="tuning-sections"' in tuning_panel
    assert 'id="tuning-save"' in tuning_panel


def test_settings_shared_save_bar_outside_panels(client: TestClient) -> None:
    """The one Save button PUTs zones + neighbors + routing + notifications,
    so it must not live inside a single panel."""
    html = client.get("/settings").text
    assert html.count('id="save-btn"') == 1
    pos = html.index('id="save-btn"')
    assert pos > html.rindex("</section>")
