"""User guide: loader unit tests, endpoints, and the docs-coverage contract.

The coverage tests are the maintenance mechanism the guide was built around:
adding an HTML page or a config section without documenting it in some
guide_content/*.md frontmatter fails here.
"""

from __future__ import annotations

import re
from html import escape as html_escape
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.guide import (
    GUIDE_DIR,
    SECTION_TITLES,
    GuideError,
    GuideRegistry,
    load_guide,
)
from marcellus.routes.guide import STAT_KEYS
from marcellus.server import create_app

# Pages that legitimately need no guide topic: the guide's own pages.
UNDOCUMENTED_OK = {
    "/guide/{slug}",
}

# Fields the tightened `test_every_config_field_is_documented` still can't
# see real documentation for: `"section.field" -> reason`. Every entry is a
# pre-existing prose-only mention, not a newly undocumented knob -- the
# encounters and push.encounter_* families are deliberately empty here.
UNDOCUMENTED_OK_FIELDS: dict[str, str] = {
    "face_capture.apply_annotation_offset": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.capture_delay_s": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.crop_pad": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.crop_to_bbox": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.dedup_window_s": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.head_fraction": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.http_timeout_s": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.lookback_s": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.max_attempts": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.max_captures_per_run": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.max_visit_s": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.retention_days": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.thumb_max_edge": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_capture.thumb_quality": (
        "pre-existing gap: faces-pipeline.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.best_n": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.cameras": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.cluster_threshold": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.cluster_ttl_days": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.enabled": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.http_timeout_s": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.lookback_s": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.match_threshold": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.max_attempts": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.max_frames": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.min_face_area_px": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.min_quality": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.min_sample_gap_s": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "face_enrich.process_delay_s": (
        "pre-existing gap: identities.md is prose, per-knob defaults untabulated"
    ),
    "frigate.config_refresh_enabled": (
        "pre-existing gap: first-run.md covers this in prose, no stated default"
    ),
    "push.activity_dismissal_tail_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.activity_reap_after_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.activity_resolution_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.activity_sweep_interval_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.capture_enabled": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.capture_max_bytes": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_backfill_staleness_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_enabled": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_la_enabled": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_resound_sweep_interval_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_urgent_resound_enabled": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_urgent_resound_max": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.delivery_urgent_resound_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.enabled": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.mqtt_client_id": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.mqtt_password": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.mqtt_port": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.mqtt_topic_events": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.mqtt_username": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.offline_silence_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.reconnect_backoff_max_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.reconnect_backoff_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.relay_base_url": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.relay_breaker_failures": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.relay_breaker_open_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.relay_retry_attempts": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.relay_timeout_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.situation_handle_ttl_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.thumbnail_max_edge": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.thumbnail_quality": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "push.thumbnail_timeout_s": (
        "pre-existing gap: push-notifications.md covers the pipeline, not this knob"
    ),
    "scrub.aged_after_h": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.aged_interval_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.backfill_segments_per_cycle": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.backfill_time_budget_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.cell_h": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.derive_time_reserve_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.enabled": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.generate_interval_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.live_edge_interval_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.live_edge_lookback_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.live_edge_segments": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.match_keyframe_cadence": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.min_free_bytes": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.preserve_source_aspect": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.prune_interval_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.recent_interval_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.retention_days": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "scrub.sheet_version_grace_s": (
        "pre-existing gap: scrub.md is conceptual, per-knob defaults untabulated"
    ),
    "sidecar.auth_cache_max_entries": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.auth_cache_ttl_s": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.login_rate_limit_attempts": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.login_rate_limit_window_s": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.remember_cache_ttl_s": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.remember_ttl_s": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "sidecar.require_frigate_auth": (
        "pre-existing gap: first-run/troubleshooting give behaviour, not defaults"
    ),
    "watchdog.cooldown_s": (
        "pre-existing gap: settings.md gives behaviour, per-knob defaults untabulated"
    ),
    "watchdog.failures_before_restart": (
        "pre-existing gap: settings.md gives behaviour, per-knob defaults untabulated"
    ),
    "watchdog.interval_s": (
        "pre-existing gap: settings.md gives behaviour, per-knob defaults untabulated"
    ),
    "watchdog.max_restarts_per_hour": (
        "pre-existing gap: settings.md gives behaviour, per-knob defaults untabulated"
    ),
    "watchdog.timeout_s": (
        "pre-existing gap: settings.md gives behaviour, per-knob defaults untabulated"
    ),
}

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)


def _section_models() -> dict[str, type[BaseModel]]:
    """Settings fields that are themselves nested config sections (as
    opposed to `log_level`, a bare scalar already covered by
    `test_every_config_section_is_documented`)."""
    out: dict[str, type[BaseModel]] = {}
    for name, info in Settings.model_fields.items():
        ann = info.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out[name] = ann
    return out


def _topic_sources() -> list[tuple[str, list[str], str]]:
    """(slug, config-frontmatter list, raw markdown body) per topic file.

    Reads guide_content/*.md directly rather than through `GuideRegistry`:
    the registry only keeps rendered HTML (backticks already expanded to
    `<code>`), and this check needs the raw `` `field_name` `` markdown
    source to tell a literal code-span mention from prose.
    """
    out = []
    for path in sorted(GUIDE_DIR.glob("*.md")):
        text = path.read_text()
        m = _FRONTMATTER_RE.match(text)
        assert m, f"{path.name}: missing frontmatter"
        fm = yaml.safe_load(m.group(1)) or {}
        out.append((path.stem, list(fm.get("config", [])), m.group(2)))
    return out


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


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def registry(app: FastAPI) -> GuideRegistry:
    return app.state.guide  # type: ignore[no-any-return]


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


def _html_page_paths(app: FastAPI) -> set[str]:
    out = set()
    for route in _api_routes(app):
        if "GET" not in route.methods:
            continue
        rc = getattr(route.response_class, "value", route.response_class)
        if isinstance(rc, type) and issubclass(rc, HTMLResponse):
            out.add(route.path)
    return out


# --- Coverage contract ------------------------------------------------------


def test_every_html_page_is_documented(app: FastAPI, registry: GuideRegistry) -> None:
    documented = {r for t in registry.topics.values() for r in t.meta.routes}
    missing = _html_page_paths(app) - documented - UNDOCUMENTED_OK
    assert not missing, (
        f"HTML pages without a guide topic: {sorted(missing)} — add each to some "
        "guide_content/*.md `routes:` frontmatter (or UNDOCUMENTED_OK with a reason)"
    )


def test_every_config_section_is_documented(registry: GuideRegistry) -> None:
    documented = {c for t in registry.topics.values() for c in t.meta.config}
    missing = set(Settings.model_fields) - documented
    assert not missing, (
        f"config sections without a guide topic: {sorted(missing)} — add each to "
        "some guide_content/*.md `config:` frontmatter"
    )


_NUMERIC_OR_BOOL = (int, float, bool)
_BULLET_PREFIXES = ("- ", "* ", "+ ")


def _is_documenting_line(line: str, field_name: str) -> tuple[bool, bool]:
    """(is a real documenting line, line is a table row) for one field.

    A bare `` `field` `` mention anywhere in a body no longer counts: the
    line must be a bullet or a table row, must name the field in backticks,
    and must carry at least 25 characters of prose after that mention.
    """
    stripped = line.strip()
    is_row = stripped.startswith("|")
    if not is_row and not stripped.startswith(_BULLET_PREFIXES):
        return False, False
    token = f"`{field_name}`"
    idx = stripped.find(token)
    if idx < 0:
        return False, is_row
    rest = stripped[idx + len(token) :].strip(" |-\t")
    return len(rest) >= 25, is_row


def _table_has_default_column(lines: list[str], index: int) -> bool:
    """Walk up from a table row to its header row and look for a Default column."""
    for i in range(index - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped.startswith("|"):
            return False
        if "default" in stripped.lower():
            return True
    return False


def test_every_config_field_is_documented() -> None:
    """Every field of every nested `Settings` section model must be really
    documented, not merely mentioned.

    A field counts as documented when some topic whose `config:` frontmatter
    lists the field's section has a body line that is a bullet or a table
    row, names the field in backticks, and follows it with at least 25
    characters of prose. Numeric and boolean fields additionally need the
    word "default" on that line, or a Default column on that table -- a knob
    with no stated default is not documented for an operator's purposes.

    Frontmatter alone never counts: a `section.field` entry in `config:`
    documents nothing a reader can see.

    `UNDOCUMENTED_OK_FIELDS` (as `"section.field"` strings) excuses fields
    with no honest one-line effect to document.
    """
    topics = _topic_sources()
    missing: list[str] = []
    for section, model in _section_models().items():
        bodies = [body for _slug, cfg, body in topics if section in cfg]
        for field_name, info in model.model_fields.items():
            dotted = f"{section}.{field_name}"
            if dotted in UNDOCUMENTED_OK_FIELDS:
                continue
            needs_default = isinstance(info.annotation, type) and issubclass(
                info.annotation, _NUMERIC_OR_BOOL
            )
            found = False
            for body in bodies:
                lines = body.splitlines()
                for i, line in enumerate(lines):
                    ok, is_row = _is_documenting_line(line, field_name)
                    if not ok:
                        continue
                    stated_default = "default" in line.lower() or (
                        is_row and _table_has_default_column(lines, i)
                    )
                    if needs_default and not stated_default:
                        continue
                    found = True
                    break
                if found:
                    break
            if not found:
                missing.append(dotted)
    assert not missing, (
        f"config fields without real guide documentation: {sorted(missing)} -- "
        "add a bullet or table row naming the field in backticks, with its "
        "effect (and its default, for a number or a flag), to the body of a "
        "topic that lists the section in `config:`, or excuse it in "
        "UNDOCUMENTED_OK_FIELDS with a reason"
    )


def test_documented_routes_and_stats_exist(app: FastAPI, registry: GuideRegistry) -> None:
    all_paths = {r.path for r in _api_routes(app)}
    for topic in registry.topics.values():
        for path in topic.meta.routes:
            assert path in all_paths, f"{topic.slug}: unknown route {path!r}"
        unknown_stats = topic.stats_used - STAT_KEYS
        assert not unknown_stats, f"{topic.slug}: unknown stats {sorted(unknown_stats)}"


def test_internal_links_resolve(app: FastAPI, registry: GuideRegistry) -> None:
    """Every root-relative markdown link must hit a real route."""
    routes = _api_routes(app)
    exact = {r.path for r in routes}
    for topic in registry.topics.values():
        for link in topic.internal_links:
            if link in exact:
                continue
            if link.startswith("/guide/"):
                assert link.removeprefix("/guide/") in registry.topics, (
                    f"{topic.slug}: broken guide link {link!r}"
                )
                continue
            assert any(r.path_regex.match(link) for r in routes), (
                f"{topic.slug}: link {link!r} matches no route"
            )


def test_sections_valid_and_orders_unique(registry: GuideRegistry) -> None:
    seen: set[tuple[str, int]] = set()
    for topic in registry.topics.values():
        assert topic.meta.section in SECTION_TITLES
        key = (topic.meta.section, topic.meta.order)
        assert key not in seen, f"duplicate order {key} in section"
        seen.add(key)


# --- Endpoints --------------------------------------------------------------


def test_index_lists_every_topic(client: TestClient, registry: GuideRegistry) -> None:
    resp = client.get("/guide")
    assert resp.status_code == 200
    for topic in registry.topics.values():
        assert f"/guide/{topic.slug}" in resp.text


def test_every_topic_page_renders(client: TestClient, registry: GuideRegistry) -> None:
    for topic in registry.topics.values():
        resp = client.get(f"/guide/{topic.slug}")
        assert resp.status_code == 200
        assert html_escape(topic.meta.title) in resp.text


def test_unknown_topic_404s(client: TestClient) -> None:
    assert client.get("/guide/no-such-topic").status_code == 404


def test_stats_json_covers_all_keys(client: TestClient) -> None:
    resp = client.get("/guide/stats.json")
    assert resp.status_code == 200
    stats = resp.json()["stats"]
    assert set(stats) == set(STAT_KEYS)
    assert all(isinstance(v, str) for v in stats.values())
    # Seeded sidecar DB is empty but present: counts are real zeros, not "?".
    assert stats["clusters_total"] == "0"


def test_search_json_indexes_every_topic(
    client: TestClient, registry: GuideRegistry
) -> None:
    resp = client.get("/guide/search.json")
    assert resp.status_code == 200
    entries = {t["slug"]: t for t in resp.json()["topics"]}
    assert set(entries) == set(registry.topics)
    for entry in entries.values():
        assert entry["text"], f"{entry['slug']}: empty search text"
        assert entry["number"].count(".") == 1


def test_topic_page_has_anchors_and_onpage_toc(
    client: TestClient, registry: GuideRegistry
) -> None:
    topic = next(t for t in registry.topics.values() if len(t.headings) >= 2)
    resp = client.get(f"/guide/{topic.slug}")
    assert "On this page" in resp.text
    for anchor, _text in topic.headings:
        assert f'id="{anchor}"' in resp.text
        assert f'href="#{anchor}"' in resp.text


def test_sidebar_marks_current_topic(client: TestClient, registry: GuideRegistry) -> None:
    slug = next(iter(registry.topics))
    resp = client.get(f"/guide/{slug}")
    assert 'class="current"' in resp.text
    numbers = registry.numbers()
    assert numbers[slug] in resp.text


def test_walkthrough_renders_checklist(client: TestClient, registry: GuideRegistry) -> None:
    walk_topics = [t for t in registry.topics.values() if t.walkthrough_steps]
    assert walk_topics, "expected at least one walkthrough in the guide"
    resp = client.get(f"/guide/{walk_topics[0].slug}")
    assert 'class="walkthrough"' in resp.text
    assert 'data-step="0"' in resp.text


# --- Loader unit tests ------------------------------------------------------


def _write_topic(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_loader_rejects_missing_frontmatter(tmp_path: Path) -> None:
    _write_topic(tmp_path, "bad.md", "no frontmatter here\n")
    with pytest.raises(GuideError, match="frontmatter"):
        load_guide(tmp_path)


def test_loader_rejects_unknown_section(tmp_path: Path) -> None:
    _write_topic(
        tmp_path, "bad.md", "---\ntitle: X\nsection: nonsense\norder: 1\n---\nbody\n"
    )
    with pytest.raises(GuideError, match="unknown section"):
        load_guide(tmp_path)


def test_loader_rejects_empty_walkthrough(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "bad.md",
        "---\ntitle: X\nsection: daily-review\norder: 1\n---\n```walkthrough\nnothing\n```\n",
    )
    with pytest.raises(GuideError, match="walkthrough"):
        load_guide(tmp_path)


def test_loader_renders_stats_and_walkthroughs(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "ok.md",
        "---\ntitle: X\nsection: daily-review\norder: 1\n---\n"
        "Count: {{stat:clusters_total}}\n\n"
        "```walkthrough\n- step one\n- step <two>\n```\n"
        "[link](/triage)\n",
    )
    topic = load_guide(tmp_path).topics["ok"]
    assert topic.stats_used == {"clusters_total"}
    assert 'data-stat="clusters_total"' in topic.html
    assert topic.walkthrough_steps == 1
    assert "step &lt;two&gt;" in topic.html
    assert topic.internal_links == {"/triage"}


def test_loader_extracts_headings_and_search_text(tmp_path: Path) -> None:
    _write_topic(
        tmp_path,
        "ok.md",
        "---\ntitle: X\nsection: daily-review\norder: 1\n---\n"
        "intro words\n\n## Using it\n\nbody\n\n## Using it\n\nagain\n",
    )
    topic = load_guide(tmp_path).topics["ok"]
    assert topic.headings == (("using-it", "Using it"), ("using-it-1", "Using it"))
    assert 'id="using-it"' in topic.html and 'id="using-it-1"' in topic.html
    assert "intro words" in topic.search_text and "<" not in topic.search_text


def test_neighbors_follow_section_order(tmp_path: Path) -> None:
    for i, name in enumerate(["a.md", "b.md"]):
        _write_topic(
            tmp_path, name, f"---\ntitle: T{i}\nsection: daily-review\norder: {i}\n---\nbody\n"
        )
    reg = load_guide(tmp_path)
    prev_t, next_t = reg.neighbors("a")
    assert prev_t is None and next_t is not None and next_t.slug == "b"
    assert reg.neighbors("missing") == (None, None)
