"""Event-loop lag monitor (loop_lag.py), its `/healthz` field, and the
proxy's per-phase slow-acquire line."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.loop_lag import LoopLagMonitor
from marcellus.routes.proxy import _SendPhases
from marcellus.server import create_app


def test_max_lag_covers_only_the_window() -> None:
    m = LoopLagMonitor(window_s=60.0)
    m.record(0.0, 0.9)
    m.record(30.0, 0.1)
    assert m.max_lag_s(now=30.0) == pytest.approx(0.9)
    m.record(61.0, 0.05)
    assert m.max_lag_s(now=61.0) == pytest.approx(0.1)
    assert m.max_lag_s(now=200.0) == 0.0


def test_warnings_are_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    m = LoopLagMonitor(warn_s=0.25, warn_every_s=30.0)
    with caplog.at_level("WARNING", logger="marcellus.loop_lag"):
        m.record(0.0, 0.1)  # under the threshold: silent
        m.record(1.0, 0.4)  # logged
        m.record(2.0, 1.5)  # suppressed
        m.record(3.0, 0.3)  # suppressed
        m.record(40.0, 0.5)  # logged, carrying the suppressed worst
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert "400 ms" in lines[0]
    assert "worst 1500 ms over 3 late wake-up(s)" in lines[1]


async def test_monitor_sees_a_blocked_loop() -> None:
    m = LoopLagMonitor(interval_s=0.02)
    task = asyncio.create_task(m.run())
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.3)  # hold the loop, like a synchronous sqlite read would
        await asyncio.sleep(0.05)
    finally:
        task.cancel()
    assert m.max_lag_s() >= 0.2


def _app(tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path) -> object:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return create_app(
        Settings(
            frigate=FrigateSection(
                base_url="http://frigate.test:5000",
                config_path=fake_config,
                db_path=frigate_db_path,
            ),
            sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        )
    )


def test_healthz_reports_lag_without_changing_the_status(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    app = _app(tmp_path, frigate_db_path, sidecar_db_path)
    client = TestClient(app)  # type: ignore[arg-type]
    before = client.get("/healthz")
    assert "loop_lag_max_ms" not in before.json()["checks"]  # lifespan not running

    lagging = LoopLagMonitor()
    lagging.record(time.monotonic(), 7.5)  # far past any threshold
    app.state.loop_lag = lagging  # type: ignore[attr-defined]
    after = client.get("/healthz")
    assert after.json()["checks"]["loop_lag_max_ms"] == 7500
    assert after.status_code == before.status_code
    assert after.json()["status"] == before.json()["status"]


def test_lifespan_starts_the_monitor(
    tmp_path: Path, frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    app = _app(tmp_path, frigate_db_path, sidecar_db_path)
    with TestClient(app) as client:  # type: ignore[arg-type]
        assert isinstance(app.state.loop_lag, LoopLagMonitor)  # type: ignore[attr-defined]
        assert "loop_lag_max_ms" in client.get("/healthz").json()["checks"]


async def test_send_phases_split_a_slow_send() -> None:
    loop = asyncio.get_running_loop()
    phases = _SendPhases(loop.time())
    assert phases.describe() == "phases=untraced"
    await asyncio.sleep(0.05)  # waiting on the pool: no event yet
    for step in (
        "connection.connect_tcp.started",
        "connection.connect_tcp.complete",
        "http11.send_request_headers.started",
        "http11.send_request_headers.complete",
        "http11.send_request_body.started",
        "http11.send_request_body.complete",
        "http11.receive_response_headers.started",
    ):
        await phases(step, {})
    await asyncio.sleep(0.05)
    await phases("http11.receive_response_headers.complete", {})
    line = phases.describe()
    fields = dict(part.split("=") for part in line.split())
    assert set(fields) == {"pool_wait", "connect", "write", "headers"}
    assert float(fields["pool_wait"]) >= 0.04
    assert float(fields["headers"]) >= 0.04
    assert float(fields["connect"]) < 0.04


async def test_send_phases_on_a_reused_connection() -> None:
    phases = _SendPhases(asyncio.get_running_loop().time())
    await phases("http2.send_request_headers.started", {})
    await phases("http2.receive_response_headers.started", {})
    await phases("http2.receive_response_headers.complete", {})
    assert "connect=-" in phases.describe()
