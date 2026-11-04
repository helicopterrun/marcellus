"""`build_verdict` (pure) and the verdict wiring on /status.json and `/`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.server import create_app
from marcellus.verdict import build_verdict


def test_all_ok() -> None:
    v = build_verdict(
        {
            "frigate": "ok",
            "mqtt": "connected",
            "db": "ok",
            "scrub": "ok",
            "scrub_last_cycle_age_s": 2.0,
            "encounters": "disabled",
            "recording": {"status": "ok", "stalled": [], "last_check_age_s": 3.0},
        },
        ok=True,
        failing=[],
    )
    assert v == {"level": "ok", "problems": [], "notes": []}


def test_one_failure_gates_bad() -> None:
    v = build_verdict({"frigate": "ok", "mqtt": "disconnected"}, ok=False, failing=["mqtt"])
    assert v["level"] == "bad"
    assert [p["check"] for p in v["problems"]] == ["mqtt"]
    assert "MQTT" in v["problems"][0]["message"]


def test_several_failures_most_serious_first() -> None:
    checks = {
        "frigate": "unreachable",  # informational -> warn
        "recording": {"status": "stalled", "stalled": [{"camera": "gate", "since": 1.0}]},
        "db": "error",  # gating -> bad
    }
    v = build_verdict(checks, ok=False, failing=["db"])
    assert v["level"] == "bad"
    levels = [p["level"] for p in v["problems"]]
    assert levels == sorted(levels, key=lambda lv: {"bad": 0, "warn": 1}[lv])
    assert v["problems"][0]["check"] == "db"
    rec = next(p for p in v["problems"] if p["check"] == "recording")
    assert rec["href"] == "/cameras"
    assert "gate" in rec["message"]


def test_informational_only_is_warn_when_healthz_ok() -> None:
    v = build_verdict({"frigate": "unreachable"}, ok=True, failing=[])
    assert v["level"] == "warn"


def test_notices_are_notes_not_problems() -> None:
    checks = {
        "source:cam": "ok",
        "source:cam:alerts": [],
        "source:cam:notices": ["missed an event"],
        "source:cam:last_event_age_s": 12.0,
    }
    v = build_verdict(checks, ok=True, failing=[])
    assert v["level"] == "ok"
    assert v["problems"] == []
    assert v["notes"] == ["Cam: missed an event"]


def test_unknown_check_falls_back_to_humanised_name_and_status() -> None:
    v = build_verdict({"new_widget": "wobbly", "other_thing": "ok"}, ok=True, failing=[])
    assert v["level"] == "warn"
    assert v["problems"][0]["message"] == "New widget: wobbly."


def test_degraded_without_known_cause_still_not_ok() -> None:
    v = build_verdict({}, ok=False, failing=[])
    assert v["level"] == "bad"


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        frigate=FrigateSection(base_url="http://127.0.0.1:1", db_path=tmp_path / "f.db"),
        sidecar=SidecarSection(
            db_path=tmp_path / "s.db", bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_status_json_has_verdict(client: TestClient) -> None:
    body = client.get("/status.json").json()
    v = body["verdict"]
    assert v["level"] in ("ok", "warn", "bad")
    assert isinstance(v["problems"], list)
    assert isinstance(v["notes"], list)


def test_verdict_agrees_with_healthz(client: TestClient) -> None:
    healthz = client.get("/healthz")
    v = client.get("/status.json").json()["verdict"]
    if healthz.status_code == 503:
        assert v["level"] == "bad"
    else:
        assert v["level"] in ("ok", "warn")


def test_status_page_renders_banner_and_groups(client: TestClient) -> None:
    html = client.get("/").text
    assert 'id="verdict"' in html
    for group in ("scrub", "hardware", "counters"):
        assert f'data-group="{group}"' in html
    assert "<details" in html and "open" not in html.split('data-group="counters"')[0].split(
        "<details"
    )[-1]
    assert 'data-k="recording"' in html
    assert 'data-k="devices"' in html
