from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.config import (
    FrigateSection,
    Settings,
    SidecarSection,
)
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    # Frigate config.yml is optional; sidecar should handle a missing file.
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_list_html(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    assert "Marcellus" in r.text
    # Each fixture camera should appear in the camera filter.
    assert "alley-overview" in r.text
    assert "street-overview" in r.text


def test_list_filter_by_camera(client: TestClient) -> None:
    r = client.get("/triage", params={"camera": "alley-east", "days": 7})
    assert r.status_code == 200
    # `alley-east` is in the fixture but only as a dog event; should render.
    assert "alley-east" in r.text


def test_detail_html(client: TestClient) -> None:
    r = client.get("/event/e1")
    assert r.status_code == 200
    assert "alley-overview" in r.text
    # Detail page exposes label buttons.
    assert 'data-label="tp"' in r.text


def test_detail_404(client: TestClient) -> None:
    r = client.get("/event/does-not-exist")
    assert r.status_code == 404


def test_label_round_trip(client: TestClient) -> None:
    r = client.post(
        "/label",
        json={"event_id": "e1", "label": "fp", "note": "porch shadow"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Default `submit_plus=False` means no Plus call was attempted.
    assert body["plus"] == {"status": "not_requested"}
    # The list view filtered to fp should now include e1.
    r2 = client.get("/triage", params={"triage": "fp"})
    assert "e1" in r2.text


def test_label_with_plus_disabled_skips(client: TestClient) -> None:
    """If submit_plus=True but the app's plus_enabled is False, we return
    a `skipped/plus_not_enabled` status without making any HTTP calls."""
    r = client.post(
        "/label",
        json={"event_id": "e1", "label": "fp", "submit_plus": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["plus"]["status"] == "skipped"
    assert body["plus"]["reason"] == "plus_not_enabled"


def test_label_invalid(client: TestClient) -> None:
    r = client.post("/label", json={"event_id": "e1", "label": "garbage"})
    assert r.status_code == 400


def test_clear_label(client: TestClient) -> None:
    client.post("/label", json={"event_id": "e1", "label": "fp"})
    r = client.post("/clear-label", json={"event_id": "e1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_degraded_when_mqtt_disconnected(client: TestClient) -> None:
    """A dead subscriber must surface as 503 -- the 2026-08-11 outage sat
    invisible for 41 hours behind a static-ok healthcheck."""

    class _DeadSubscriber:
        connected = False

    client.app.state.settings.push.enabled = True
    client.app.state.push_subscriber = _DeadSubscriber()
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["mqtt"] == "disconnected"


def test_healthz_reports_frigate_ok_without_gating_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: list[str] = []

    class _FakeStreamClient:
        is_closed = False

        async def get(self, url: str, timeout: object = None) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

    client.app.state.stream_http_client = _FakeStreamClient()
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["frigate"] == "ok"
    # Probed through `frigate.proxy_base_url` (the proxy's own origin), not
    # `frigate.base_url` -- see the 2026-09-12 proxy-stall gating change.
    assert calls == ["http://frigate.lan:8971/api/version"]


def test_healthz_frigate_unreachable_is_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frigate being unreachable is still informational (2026-09-10 gating
    fix): watchdog.py restarts the Frigate container directly, and only the
    sidecar's *own* pool being wedged (`pool_exhausted`) gates /healthz --
    an ordinary connect failure to Frigate itself must not flip the sidecar
    to 503."""
    import httpx

    class _FakeStreamClient:
        is_closed = False

        async def get(self, url: str, timeout: object = None) -> httpx.Response:
            raise httpx.ConnectError("boom")

    client.app.state.stream_http_client = _FakeStreamClient()
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["frigate"] == "unreachable"


def test_healthz_frigate_pool_exhausted_is_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout or `httpx.PoolTimeout` on the proxy-path probe is the one
    frigate outcome that gates /healthz: it means the sidecar's own proxy is
    wedged and no proxied request can get through (the 2026-09-10 /
    2026-09-12 incidents), which a restart fixes."""
    import httpx

    class _FakeStreamClient:
        is_closed = False

        async def get(self, url: str, timeout: object = None) -> httpx.Response:
            raise httpx.PoolTimeout("pool")

    client.app.state.stream_http_client = _FakeStreamClient()
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["frigate"] == "proxy_stalled"
    assert body["reason"] == "proxy_stalled"


def test_healthz_upstream_pool_saturated_is_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from frigate_sidecar import frigate_api

    class _FakeClient:
        is_closed = False

        async def get(self, url: str, timeout: object = None) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(
        "frigate_sidecar.routes.health.pool_stats",
        lambda c: {
            "connections": frigate_api._STREAM_LIMITS.max_connections,
            "active": frigate_api._STREAM_LIMITS.max_connections,
            "idle": 0,
        },
    )
    client.app.state.stream_http_client = _FakeClient()
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["upstream_pool"]["idle"] == 0


def test_healthz_frigate_probe_is_cached_within_window(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    calls: list[str] = []

    class _FakeStreamClient:
        is_closed = False

        async def get(self, url: str, timeout: object = None) -> httpx.Response:
            calls.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))

    client.app.state.stream_http_client = _FakeStreamClient()
    client.get("/healthz")
    client.get("/healthz")
    assert len(calls) == 1  # second call served from the cached verdict

    # Force the cache to look stale: the third call re-probes.
    cache_ts, verdict = client.app.state._frigate_health_cache
    client.app.state._frigate_health_cache = (cache_ts - 3600, verdict)
    client.get("/healthz")
    assert len(calls) == 2


def test_healthz_scrub_low_disk_reflects_the_flag(client: TestClient) -> None:
    """Additive `scrub_low_disk` top-level field -- absent means an older
    sidecar, present and true means the generation loop is currently skipping
    cycles for lack of free space (server.py `_scrub_generation_loop`)."""
    client.app.state.settings.scrub.enabled = True
    client.app.state.scrub_last_cycle = time.time()
    client.app.state.scrub_low_disk = False

    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["scrub_low_disk"] is False

    client.app.state.scrub_low_disk = True
    r = client.get("/healthz")
    assert r.json()["scrub_low_disk"] is True


def test_healthz_scrub_locked_is_degraded(client: TestClient) -> None:
    """When another process holds the scrub cache lock, the service never
    starts the generation loop -- `/healthz` must surface that distinctly
    from `stale` so an operator knows to look for a stray process."""
    client.app.state.settings.scrub.enabled = True
    client.app.state.scrub_locked = True

    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["scrub"] == "locked"


def test_motion_blank_form(client: TestClient) -> None:
    # No baseline/target: page renders the form + a "set a target" empty state.
    r = client.get("/motion")
    assert r.status_code == 200
    assert "Marcellus" in r.text
    # Header nav renders both pages and motion is the active link.
    assert 'class="page-link active"' in r.text
    assert "Motion" in r.text
    assert "Triage" in r.text


def test_motion_error_on_unreachable_frigate(client: TestClient) -> None:
    # Live API not reachable -> error banner, and a non-200 so the page-level
    # TTL cache (routes/_cache.py) doesn't stick the "unreachable" banner past
    # the outage.
    r = client.get("/motion", params={"baseline": "yesterday", "target": "today"})
    assert r.status_code == 503
    assert "Frigate API unreachable" in r.text or "date parse error" in r.text


def _stub_motion_active(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replaces `motion_active.analyze` with one that records its `days` and
    `until` kwargs and returns an empty result -- so single-window `/motion`
    parsing can be checked without a live Frigate."""
    from frigate_sidecar.analysis import motion_active

    captured: dict[str, object] = {}

    def fake_analyze(
        *, frigate_base_url: str, days: int, until: str | None = None
    ) -> dict[str, object]:
        captured["days"] = days
        captured["until"] = until
        return {"since": "stub", "rows": []}

    monkeypatch.setattr(motion_active, "analyze", fake_analyze)
    return captured


def test_motion_single_mode_today_is_one_day(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date

    captured = _stub_motion_active(monkeypatch)
    r = client.get("/motion", params={"target": "today"})
    assert r.status_code == 200
    assert captured["days"] == 1
    assert captured["until"] == date.today().isoformat()


def test_motion_single_mode_yesterday_is_two_days_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`target=yesterday` used to fall through the old ternary to a flat 14
    days -- silently becoming "the last 14 days" instead of yesterday."""
    from datetime import date, timedelta

    captured = _stub_motion_active(monkeypatch)
    r = client.get("/motion", params={"target": "yesterday"})
    assert r.status_code == 200
    assert captured["days"] == 2
    # Upper bound must be yesterday, not today -- otherwise today's partial
    # data leaks into a "yesterday only" view.
    assert captured["until"] == (date.today() - timedelta(days=1)).isoformat()


def test_motion_single_mode_explicit_date_anchors_days_since(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    captured = _stub_motion_active(monkeypatch)
    target = (date.today() - timedelta(days=5)).isoformat()
    r = client.get("/motion", params={"target": target})
    assert r.status_code == 200
    assert captured["days"] == 6  # since = 5 days ago -> days-back = 5 + 1
    assert captured["until"] == target  # a single date bounds both ends to it


def test_motion_single_mode_range_anchors_on_the_start_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import date, timedelta

    captured = _stub_motion_active(monkeypatch)
    lo = (date.today() - timedelta(days=10)).isoformat()
    hi = (date.today() - timedelta(days=3)).isoformat()
    r = client.get("/motion", params={"target": f"{lo}..{hi}"})
    assert r.status_code == 200
    assert captured["days"] == 11
    assert captured["until"] == hi  # the range's end date bounds the query too


def test_motion_single_mode_rejects_an_unparseable_target(client: TestClient) -> None:
    r = client.get("/motion", params={"target": "not-a-date"})
    assert r.status_code == 400
    assert "bad target" in r.json()["detail"]["message"]


def test_list_has_active_nav(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    # Verify the nav with active=triage shows up.
    assert "page-link active" in r.text


def test_score_histogram_page(client: TestClient) -> None:
    r = client.get("/score-histogram", params={"days": 7})
    assert r.status_code == 200
    # Filter form is present + nav highlights scores.
    assert "Score Histogram" in r.text or "score-histogram" in r.text
    assert ">Scores<" in r.text
    # The fixture has events for alley-overview, etc.
    assert "alley-overview" in r.text


def test_score_histogram_camera_filter(client: TestClient) -> None:
    r = client.get(
        "/score-histogram",
        params={"days": 7, "camera": "alley-east"},
    )
    assert r.status_code == 200
    # Selected option markup present.
    assert 'value="alley-east" selected' in r.text


def test_score_histogram_page_degrades_instead_of_500ing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike its siblings (motion/zone-hits/fps-budget), this route had no
    try/except at all -- a locked/broken DB read 500ed here."""
    from frigate_sidecar.analysis import score_histogram

    def boom(**_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(score_histogram, "analyze", boom)
    # `ttl_page_cache`'s cache is keyed on the full URL and shared across every
    # app instance in this process (it lives on the decorated function, not
    # per-app) -- a `days=7` hit from another test's successful render would
    # otherwise serve that cached 200 here without ever calling `boom`.
    r = client.get("/score-histogram", params={"days": 13})
    # Degrades (503, so the TTL cache skips it) instead of a raw 500 -- and
    # now renders the same error banner as its siblings (motion/zone-hits/
    # fps-budget), with the exception text visible in the body.
    assert r.status_code == 503
    assert "Score Histogram" in r.text or "score-histogram" in r.text
    assert "boom" in r.text


def test_fps_budget_page_error_when_api_down(client: TestClient) -> None:
    # Test config points at a non-existent Frigate; page renders an error
    # banner with a non-200 status (so the TTL cache doesn't stick it).
    r = client.get("/fps-budget")
    assert r.status_code == 503
    assert "Frigate API unreachable" in r.text


def test_nav_appears_on_all_pages(client: TestClient) -> None:
    # Same nav on all pages. `/fps-budget` errors (no live Frigate in this
    # test) but still renders the shared nav around its error banner.
    for path in ("/", "/motion", "/score-histogram", "/fps-budget"):
        r = client.get(path)
        expected = 503 if path == "/fps-budget" else 200
        assert r.status_code == expected, path
        for label in ("Triage", "Motion", "Scores", "FPS budget"):
            assert label in r.text, f"{label} missing on {path}"


# ----- Analysis HTTP routes -----


def test_analysis_motion_rate(client: TestClient) -> None:
    r = client.get("/analysis/motion-rate", params={"days": 7})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row["camera"] == "alley-overview" for row in rows)


def test_analysis_score_histogram(client: TestClient) -> None:
    r = client.get("/analysis/score-histogram", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "buckets" in body


def test_analysis_zone_hits(client: TestClient) -> None:
    r = client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "hits" in body and "mask_candidates" in body


def test_analysis_zone_hits_maps_db_locked_to_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar import db
    from frigate_sidecar.analysis import zone_hits

    def always_locked(**_kwargs: object) -> object:
        raise db.DBLockedError("database is locked")

    monkeypatch.setattr(zone_hits, "analyze", always_locked)
    r = client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "error": "db_locked", "message": "database is locked",
    }


def test_analysis_pull_events_maps_db_locked_to_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar import db
    from frigate_sidecar.analysis import pull_events

    def always_locked(**_kwargs: object) -> object:
        raise db.DBLockedError("database is locked")
        yield  # pragma: no cover - makes this a generator function, like pull()

    monkeypatch.setattr(pull_events, "pull", always_locked)
    r = client.get("/analysis/pull-events", params={"days": 7, "limit": 100})
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "error": "db_locked", "message": "database is locked",
    }


def test_analysis_pull_events(client: TestClient) -> None:
    r = client.get("/analysis/pull-events", params={"days": 7, "limit": 100})
    assert r.status_code == 200
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert ids == {"e1", "e2", "e3", "e4"}


def test_analysis_pull_events_camera_filter(client: TestClient) -> None:
    r = client.get(
        "/analysis/pull-events", params={"days": 7, "camera": "alley-overview"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert {row["camera"] for row in rows} == {"alley-overview"}


def test_analysis_fps_budget_502_on_api_failure(client: TestClient) -> None:
    # No real Frigate API at the configured base URL in this test -> 502.
    r = client.get("/analysis/fps-budget")
    assert r.status_code == 502


def test_alignment_state_empty(client: TestClient) -> None:
    r = client.get("/analysis/annotation-offset/state")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["results"] is None
    assert body["applied_ms"] == {}


def test_alignment_apply_and_state_roundtrip(client: TestClient) -> None:
    r = client.post(
        "/analysis/annotation-offset/apply",
        json={"offsets": {"doorbell": -5000, "gate": 250}},
    )
    assert r.status_code == 200
    assert r.json()["applied"] == {"doorbell": -5000, "gate": 250}

    body = client.get("/analysis/annotation-offset/state").json()
    assert body["applied_ms"] == {"doorbell": -5000, "gate": 250}
    # Config has no annotation_offset for these cameras.
    assert body["config_ms"]["doorbell"] == 0

    # Zero clears the override.
    r = client.post("/analysis/annotation-offset/apply", json={"offsets": {"gate": 0}})
    assert r.status_code == 200
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["applied_ms"] == {"doorbell": -5000}


def test_alignment_apply_rejects_garbage(client: TestClient) -> None:
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {}}
    ).status_code == 422
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {"cam": "soon"}}
    ).status_code == 422
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {"cam": 120_000}}
    ).status_code == 422


def test_alignment_measure_runs_in_background(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar.analysis import annotation_offset as ao_mod

    def _fake_analyze(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["search_window_ms"] == 8000  # wider than the CLI default
        return [{"camera": "doorbell", "suggested_offset_ms": -4950,
                 "median_offset_ms": -4980, "iqr_ms": 120, "confidence": "high",
                 "n_qualifying_events": 12, "n_contributing_events": 11}]

    monkeypatch.setattr(ao_mod, "analyze", _fake_analyze)
    r = client.post("/analysis/annotation-offset/measure", params={"days": 3})
    assert r.status_code == 200
    # TestClient runs the loop to completion between requests, so the
    # background task has finished by the next call.
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["running"] is False
    assert body["results"][0]["camera"] == "doorbell"
    assert body["measured_at"] is not None


def test_alignment_state_lists_frigate_cameras(client: TestClient) -> None:
    body = client.get("/analysis/annotation-offset/state").json()
    # Frigate unreachable in tests -> falls back to event-history cameras,
    # sorted, even with no measurement or applied offset.
    assert body["cameras"] == ["alley-east", "alley-overview", "street-overview"]
    # config_ms covers them too (fixture config has no offsets -> 0).
    assert body["config_ms"]["alley-east"] == 0


def test_alignment_state_prefers_live_config_cameras(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar import frigate_api

    # Live config wins over event history: retired camera names (present in
    # old events but no longer in Frigate's config) must not be offered.
    def _fake_config(self: object) -> dict:
        return {"cameras": {"street-overview": {}, "porch-new": {}}}

    monkeypatch.setattr(frigate_api.FrigateClient, "config", _fake_config)
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["cameras"] == ["porch-new", "street-overview"]
    assert "alley-east" not in body["cameras"]


def test_alignment_state_derives_restart_pending(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar import frigate_api

    # A camera whose running annotation_offset differs from the saved config
    # file needs a restart — derived fresh each time, so a sidecar restart
    # can't lose it. The fixture config file has no offsets (-> 0).
    def _fake_config(self: object) -> dict:
        return {"cameras": {
            "street-overview": {"detect": {"annotation_offset": -500}},
            "porch-new": {"detect": {"annotation_offset": 0}},
        }}

    monkeypatch.setattr(frigate_api.FrigateClient, "config", _fake_config)
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["restart_pending"] == ["street-overview"]


def test_alignment_events_lists_recent_per_camera(client: TestClient) -> None:
    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "alley-overview"}
    )
    assert r.status_code == 200
    events = r.json()
    # e1 (-300 s), e2 (-600 s), e5 (-30 d) — no paths, so extent ties at 0 and
    # newest-first decides; other cameras absent.
    assert [e["id"] for e in events] == ["e1", "e2", "e5"]
    assert events[0]["label"] == "person"
    assert events[0]["end_time"] is not None
    assert all(e["extent"] == 0.0 for e in events)


def test_alignment_events_sort_by_movement(
    client: TestClient, frigate_db_path: Path
) -> None:
    """A subject crossing the frame is the calibrator's best anchor, so the
    picker leads with it — an older far-mover outranks a newer shuffler."""
    import json as json_mod
    import sqlite3
    import time

    now = time.time()
    conn = sqlite3.connect(frigate_db_path)
    # Older, but crosses most of the frame. Its snapshot box sits at x=0.5
    # (bottom-centre 0.5, 0.5) -- the path point 8 s into the walk.
    mover = [[[0.1 + 0.05 * i, 0.5], now - 2000 + i] for i in range(15)]
    # Newer, but barely moves; no box -> anchor falls back to start.
    shuffler = [[[0.5 + 0.002 * i, 0.5], now - 400 + i] for i in range(15)]
    for eid, dt, path, box in (
        ("mv", -2000, mover, [0.45, 0.3, 0.1, 0.2]),
        ("sh", -400, shuffler, None),
    ):
        data: dict = {"path_data": path}
        if box:
            data["box"] = box
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, zones, "
            "has_clip, has_snapshot, data) VALUES (?, 'alley-overview', 'person', "
            "?, ?, '[]', 1, 1, ?)",
            (eid, now + dt, now + dt + 30, json_mod.dumps(data)),
        )
    conn.commit()
    conn.close()

    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "alley-overview"}
    )
    ids = [e["id"] for e in r.json()]
    assert ids[0] == "mv"  # far-mover first despite being older
    assert ids.index("mv") < ids.index("sh")
    events = {e["id"]: e for e in r.json()}
    assert events["mv"]["extent"] > events["sh"]["extent"] > 0

    # The snapshot is the best-scoring frame, not the start frame: the anchor
    # is the path moment nearest the snapshot box (x=0.5 -> 8 s in), so the
    # measured offset excludes the start-to-peak delay (which varies with
    # travel direction and inflated per-event spread).
    assert events["mv"]["anchor_time"] == pytest.approx(now - 2000 + 8, abs=0.01)
    assert events["sh"]["anchor_time"] == pytest.approx(events["sh"]["start_time"])

    r = client.get(
        "/analysis/annotation-offset/events",
        params={"camera": "alley-overview", "limit": 1},
    )
    assert [e["id"] for e in r.json()] == ["mv"]  # limit trims after the sort

    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "no-such-camera"}
    )
    assert r.json() == []


def test_alignment_events_exclude_pruned_recordings(
    client: TestClient, frigate_db_path: Path
) -> None:
    """A quiet camera's best movers can predate recording retention -- offering
    them yields a filmstrip of nothing but 404s. Retention is sparse (old
    segments survive in patches), so each event's OWN window must still be
    covered, not merely fall after the oldest surviving segment."""
    import sqlite3
    import time

    now = time.time()
    conn = sqlite3.connect(frigate_db_path)
    conn.executescript(
        "CREATE TABLE recordings (id TEXT PRIMARY KEY, camera TEXT, "
        "start_time REAL, end_time REAL);"
    )
    # A stray ANCIENT segment survives (the sparse-retention trap: it makes
    # every event pass a MIN(start_time) bound), plus real coverage for e1
    # only. e2 sits after the ancient segment but has no coverage of its own.
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'alley-overview', ?, ?)",
        [
            ("ancient", now - 40 * 86400, now - 40 * 86400 + 10),
            ("r1", now - 310, now - 250),
        ],
    )
    conn.commit()
    conn.close()

    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "alley-overview"}
    )
    ids = [e["id"] for e in r.json()]
    assert "e1" in ids  # covered by r1
    assert "e2" not in ids  # inside the global range, but its window is bare
    assert "e5" not in ids  # long pruned


def test_alignment_frame_proxies_recording_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from frigate_sidecar import frigate_api

    calls: list[tuple[str, float]] = []

    def _fake_snapshot(
        self: object, camera: str, ts: float, *, timeout: float = 15.0
    ) -> tuple[bytes | None, int]:
        calls.append((camera, ts))
        if camera == "gone":
            return (None, 404)
        return (b"\xff\xd8jpeg", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "recording_snapshot", _fake_snapshot)
    ts = time.time() - 300
    r = client.get("/analysis/annotation-offset/frame/doorbell", params={"ts": ts})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]
    assert calls[0][0] == "doorbell"

    r = client.get("/analysis/annotation-offset/frame/gone", params={"ts": ts})
    assert r.status_code == 404


def test_alignment_thumbnail_proxies_frigate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Served by the sidecar, not the browser proxy: Frigate's nginx 401s
    proxied /api/events image requests when Frigate auth is on (the calibrator
    shipped with broken thumbnails because of exactly that)."""
    from frigate_sidecar import frigate_api

    def _fake_thumbnail(
        self: object, event_id: str, *, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        if event_id == "gone":
            return (None, 404)
        return (b"\xff\xd8thumb", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "event_thumbnail", _fake_thumbnail)
    r = client.get("/analysis/annotation-offset/thumbnail/ev1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8thumb"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]

    r = client.get("/analysis/annotation-offset/thumbnail/gone")
    assert r.status_code == 404


def test_alignment_snapshot_proxies_frigate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calibrator's reference pane: full frame with the bbox drawn, via the
    sidecar's authorized connection (the proxy path 401s, same as thumbnails)."""
    from frigate_sidecar import frigate_api

    def _fake_snapshot(
        self: object, event_id: str, *, height: int = 480, bbox: bool = True,
        timeout: float = 10.0,
    ) -> tuple[bytes | None, int]:
        if event_id == "gone":
            return (None, 404)
        return (b"\xff\xd8snap", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "event_snapshot_jpeg", _fake_snapshot)
    r = client.get("/analysis/annotation-offset/snapshot/ev1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8snap"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]

    r = client.get("/analysis/annotation-offset/snapshot/gone")
    assert r.status_code == 404


def test_alignment_apply_config_writes_frigate_and_clears_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config-pinned escalation path: value goes into Frigate's config
    (the authoritative source), Frigate restarts, and the sidecar override is
    cleared so the two sources cannot disagree."""
    from frigate_sidecar import frigate_api

    calls: list[tuple[str, object]] = []

    def _fake_set(self: object, camera: str, offset_ms: int) -> None:
        calls.append(("set", (camera, offset_ms)))

    def _fake_restart(self: object) -> None:
        calls.append(("restart", None))

    monkeypatch.setattr(frigate_api.FrigateClient, "set_annotation_offset", _fake_set)
    monkeypatch.setattr(frigate_api.FrigateClient, "restart", _fake_restart)

    # Seed a sidecar override that the config write must clear.
    r = client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {"doorbell": -3000}}
    )
    assert r.status_code == 200

    r = client.post(
        "/analysis/annotation-offset/apply-config",
        json={"camera": "doorbell", "offset_ms": -2250},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["config_ms"] == -2250
    assert body["committed"] is False  # test config dir is not a git repo
    assert body["restart_pending"] == ["doorbell"]
    # Saving must NOT restart: several calibrations share one restart.
    assert calls == [("set", ("doorbell", -2250))]

    state = client.get("/analysis/annotation-offset/state").json()
    assert "doorbell" not in state["applied_ms"]
    assert state["restart_pending"] == ["doorbell"]

    # A second camera queues behind the same restart; then the explicit
    # restart applies both and clears the queue.
    client.post(
        "/analysis/annotation-offset/apply-config",
        json={"camera": "gate-face", "offset_ms": -500},
    )
    r = client.post("/analysis/annotation-offset/restart-frigate")
    assert r.status_code == 200
    assert r.json() == {"restarted": True, "applied": ["doorbell", "gate-face"]}
    assert ("restart", None) in calls and calls.count(("restart", None)) == 1
    state = client.get("/analysis/annotation-offset/state").json()
    assert state["restart_pending"] == []


def test_alignment_apply_config_rejects_garbage(client: TestClient) -> None:
    r = client.post("/analysis/annotation-offset/apply-config", json={})
    assert r.status_code == 422
    r = client.post(
        "/analysis/annotation-offset/apply-config",
        json={"camera": "cam", "offset_ms": "soon"},
    )
    assert r.status_code == 422
    r = client.post(
        "/analysis/annotation-offset/apply-config",
        json={"camera": "cam", "offset_ms": 120_000},
    )
    assert r.status_code == 422


def test_alignment_frame_rejects_bad_ts(client: TestClient) -> None:
    import time

    for bad in (0, -5, time.time() + 3600, time.time() - 45 * 86400):
        r = client.get("/analysis/annotation-offset/frame/cam", params={"ts": bad})
        assert r.status_code == 422, bad
