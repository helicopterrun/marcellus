from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from marcellus import db
from marcellus.config import BernardoSourceConfig, ExternalSourcesSection, Settings
from marcellus.external import store
from marcellus.external.media import SourceHttp
from marcellus.external.registry import build_sources
from marcellus.external.service import ExternalSourceService
from marcellus.external.types import (
    AlertSignal,
    Availability,
    Sighting,
    SightingHint,
    StateReport,
)
from marcellus.models.wire import ExternalSourcesCapabilities

FIX = Path(__file__).parent / "fixtures" / "external" / "bernardo"
ROWS: list[dict[str, Any]] = json.loads((FIX / "api_events_list.json").read_text())
ONE: dict[str, Any] = json.loads((FIX / "api_event_one.json").read_text())
NOW = ROWS[0]["start_time"] + 120.0


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHub:
    def __init__(self, sources: Any, **kw: Any) -> None:
        self.sources = sources
        self.kw = kw

    async def run(self) -> None:
        await asyncio.sleep(3600)

    def status(self) -> dict[str, Any]:
        return {"state": "connected"}


class Upstream:
    def __init__(self) -> None:
        self.list_calls = 0
        self.one: dict[str, Any] = {}  # native_id -> row or None(404)
        self.fail = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError("down")
        path = request.url.path
        if path == "/api/events":
            self.list_calls += 1
            return httpx.Response(200, json=ROWS)
        nid = path.rsplit("/", 1)[-1]
        row = self.one.get(nid)
        return httpx.Response(404) if row is None else httpx.Response(200, json=row)


def make(tmp_path: Path, **section: Any) -> tuple[ExternalSourceService, Upstream, Clock]:
    cfg = BernardoSourceConfig(id="cam1", base_url="http://pi:8000", name="Cam One")
    off = BernardoSourceConfig(id="cam2", base_url="http://pi2:8000", enabled=False, name="Off")
    s = Settings()
    s.sidecar.db_path = tmp_path / "side.db"
    s.external_sources = ExternalSourcesSection(enabled=True, sources=[cfg, off], **section)
    clock = Clock()
    up = Upstream()
    sources = build_sources(s.external_sources, clock=clock)
    http = SourceHttp(sources, transport=httpx.MockTransport(up.handler))
    svc = ExternalSourceService(s, sources=sources, http=http, hub_factory=FakeHub, clock=clock)
    return svc, up, clock


def rows(svc: ExternalSourceService) -> dict[str, Sighting]:
    conn = db.open_sidecar(svc.db_path)
    try:
        return {s.native_key: s for s in store.list_sightings(conn, limit=100)}
    finally:
        conn.close()


async def healthy(svc: ExternalSourceService, clock: Clock) -> None:
    await svc.poll_once("cam1", backfill=True)
    await svc.on_signal(StateReport("cam1", {"ok": True}))


def test_registry_unknown_kind_and_disabled(tmp_path: Path) -> None:
    svc, _, _ = make(tmp_path)
    assert [s.id for s in svc.sources] == ["cam1"]
    sec = ExternalSourcesSection(sources=[BernardoSourceConfig(id="a", base_url="http://x")])
    sec.sources[0].kind = "weird"  # type: ignore[assignment]
    with pytest.raises(ValueError):
        build_sources(sec)


async def test_backfill_upserts_three(tmp_path: Path) -> None:
    svc, up, _ = make(tmp_path)
    await svc.poll_once("cam1", backfill=True)
    assert len(rows(svc)) == 3
    assert svc.health("cam1")["consecutive_failures"] == 0
    assert svc.health("cam1")["last_event_age_s"] == pytest.approx(120.0)
    await svc.aclose()


async def test_hint_then_poll_supersedes(tmp_path: Path) -> None:
    svc, _, _ = make(tmp_path)
    key = ROWS[0]["event_key"]
    hint = Sighting(source_id="cam1", native_key=key, start=ROWS[0]["start_time"])
    await svc.on_signal(SightingHint("cam1", native_key=key, sighting=hint))
    assert rows(svc)[key].native_id is None
    await svc.poll_once("cam1")
    got = rows(svc)[key]
    assert got.native_id == str(ROWS[0]["id"]) and got.subject == "animal"
    await svc.aclose()


async def test_404_on_open_row_settles(tmp_path: Path) -> None:
    svc, up, _ = make(tmp_path)
    ghost = Sighting(
        source_id="cam1",
        native_key="20261015_200000",
        native_id="999",
        start=NOW - 1000,
        status="pending",
    )
    await svc._store([ghost], "poll")
    await svc.poll_once("cam1")
    got = rows(svc)["20261015_200000"]
    assert got.final and got.status == "failed"
    await svc.aclose()


async def test_key_mismatch_not_accepted(tmp_path: Path) -> None:
    svc, up, _ = make(tmp_path)
    ghost = Sighting(
        source_id="cam1",
        native_key="20261015_200000",
        native_id="2000",
        start=NOW - 1000,
        status="pending",
    )
    await svc._store([ghost], "poll")
    up.one["2000"] = ONE  # a different event now owns id 2000
    await svc.poll_once("cam1")
    got = rows(svc)["20261015_200000"]
    assert got.final and got.status == "failed" and got.start == NOW - 1000
    assert got.subject == "unknown"
    await svc.aclose()


async def test_refresh_debounce_coalesces(tmp_path: Path) -> None:
    svc, up, _ = make(tmp_path)
    svc.debounce_s = 0.02
    svc.followup_s = 3600
    for _ in range(5):
        svc.request_refresh("cam1")
    await asyncio.wait_for(svc._debounce["cam1"], 2)
    assert up.list_calls == 1
    assert len(svc._followup) == 1
    svc.request_refresh("cam1")
    await asyncio.wait_for(svc._debounce["cam1"], 2)
    assert up.list_calls == 2
    assert len(svc._followup) == 1  # never stacked
    await svc.aclose()
    assert not svc._followup


async def test_availability_offline_and_recovery_refresh(tmp_path: Path) -> None:
    svc, up, clock = make(tmp_path)
    svc.debounce_s = 0.01
    svc.followup_s = 3600
    await healthy(svc, clock)
    assert svc.health("cam1")["state"] == "ok"
    await svc.on_signal(Availability("cam1", False))
    assert svc.health("cam1")["state"] == "offline"
    before = up.list_calls
    await svc.on_signal(Availability("cam1", True))
    await asyncio.wait_for(svc._debounce["cam1"], 2)
    assert up.list_calls == before + 1
    assert svc.health("cam1")["state"] == "ok"
    conn = db.open_sidecar(svc.db_path)
    assert store.get_state(conn, "cam1")["availability"][0] == {"online": True}
    conn.close()
    await svc.aclose()


async def test_alerts_persist_and_show_in_health(tmp_path: Path) -> None:
    svc, _, clock = make(tmp_path)
    await healthy(svc, clock)
    await svc.on_signal(AlertSignal("cam1", "disk_low", True, "low"))
    assert svc.health("cam1")["state"] == "alert"
    assert svc.health_checks()["source:cam1:alerts"] == ["disk_low"]
    conn = db.open_sidecar(svc.db_path)
    assert "alert:disk_low" in store.get_state(conn, "cam1")
    conn.close()

    # A fresh service (restart) restores the alert.
    svc2, _, _ = make(tmp_path)
    await svc2.load_persisted_state()
    assert svc2.health("cam1")["alerts"] == ["disk_low"]

    await svc.on_signal(AlertSignal("cam1", "disk_low", False))
    assert svc.health("cam1")["state"] == "ok"
    conn = db.open_sidecar(svc.db_path)
    assert "alert:disk_low" not in store.get_state(conn, "cam1")
    conn.close()
    await svc.aclose()
    await svc2.aclose()


async def test_unreachable_after_two_failures(tmp_path: Path) -> None:
    svc, up, clock = make(tmp_path)
    await healthy(svc, clock)
    up.fail = True
    await svc.poll_once("cam1")
    assert svc.health("cam1")["state"] == "ok"
    await svc.poll_once("cam1")
    h = svc.health("cam1")
    assert h["state"] == "unreachable" and h["consecutive_failures"] == 2
    assert "ConnectError" in h["last_poll_error"]
    up.fail = False
    await svc.poll_once("cam1")
    assert svc.health("cam1")["state"] == "ok"
    await svc.aclose()


async def test_starting_then_stale_and_never_polled(tmp_path: Path) -> None:
    svc, up, clock = make(tmp_path)
    assert svc.health("cam1")["state"] == "starting"
    clock.t += 3 * svc.section.poll_interval_s
    assert svc.health("cam1")["state"] == "unreachable"  # never polled, past grace
    await svc.poll_once("cam1")
    assert svc.health("cam1")["state"] == "stale"  # no state report ever
    await svc.on_signal(StateReport("cam1", {"x": 1}))
    assert svc.health("cam1")["state"] == "ok"
    clock.t += svc.section.state_stale_s + 1
    assert svc.health("cam1")["state"] == "stale"
    assert svc.health_checks()["source:cam1:state_age_s"] > svc.section.state_stale_s
    await svc.aclose()


async def test_capabilities_validate(tmp_path: Path) -> None:
    svc, _, clock = make(tmp_path)
    await healthy(svc, clock)
    caps = ExternalSourcesCapabilities.model_validate(svc.capabilities())
    by_id = {s.id: s for s in caps.sources}
    assert by_id["cam1"].enabled and by_id["cam1"].healthy
    assert "poster" in by_id["cam1"].media
    assert not by_id["cam2"].enabled and not by_id["cam2"].healthy
    assert by_id["cam2"].subjects == []
    assert svc.source_name("cam1") == "Cam One"
    assert svc.source_name("cam2") is None
    assert svc.source_names() == {"cam1": "Cam One"}
    assert svc.health("cam2")["state"] == "disabled"
    assert svc.health_checks()["source:cam2"] == "disabled"
    await svc.aclose()


async def test_health_checks_no_db(tmp_path: Path) -> None:
    svc, _, clock = make(tmp_path)
    await healthy(svc, clock)
    Path(svc.db_path).unlink()
    checks = svc.health_checks()
    assert checks["source:cam1"] == "ok"
    assert svc.capabilities()["sources"]
    await svc.aclose()


async def test_start_and_aclose_cancels_tasks(tmp_path: Path) -> None:
    svc, up, _ = make(tmp_path)
    await svc.start()
    assert svc.hub is not None and svc.mqtt_status() == {"state": "connected"}
    tasks = list(svc._tasks)
    assert len(tasks) == 3  # poll, hub, prune
    await asyncio.sleep(0.2)
    assert up.list_calls >= 1
    await svc.aclose()
    assert all(t.done() for t in tasks)


async def test_prune_uses_retention(tmp_path: Path) -> None:
    svc, _, clock = make(tmp_path)
    await svc.poll_once("cam1")
    clock.t += 31 * 86400
    assert await svc.prune_once() == 3
    await svc.aclose()


async def test_mqtt_disabled_status(tmp_path: Path) -> None:
    svc, _, _ = make(tmp_path, mqtt_enabled=False)
    assert svc.mqtt_status() == {"state": "disabled"}
    await svc.poll_once("cam1")
    assert svc.health("cam1")["state"] == "ok"  # no state report needed
    await svc.aclose()
