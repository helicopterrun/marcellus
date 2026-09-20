"""`POST /v1/push/devices/{token}/test` -- alerts-slice2 §D "Round-trip test".

Changed from the plain-ping contract: the endpoint now sends a real **card**
payload (`mutation: "test"`) through the same relay call real cards use
(`transport.send_situation`, not `send_test`), so the NSE processes it and
can report a receipt exactly like a real alert. 404 must still mean *token
not registered* (a released client contract).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.push.engine import PushEngine
from marcellus.push.models import Device
from marcellus.push.transport import LogTransport, TransportResult
from marcellus.routes.push import reset_test_rate_limit_for_tests
from marcellus.server import create_app

TOKEN = "tok-abc123"


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> None:
    # The 10s rate limit dict is process-wide (module-level in routes/push.py)
    # so tests reusing TOKEN across test functions don't see stale entries.
    reset_test_rate_limit_for_tests()


class _RejectingTransport:
    """Transport whose card send fails, optionally as a dead token (410/400)."""

    def __init__(self, *, unregistered: bool) -> None:
        self.unregistered = unregistered
        self.calls = 0

    async def send(self, device: Device, **kw: object) -> TransportResult:
        raise AssertionError("the test endpoint must not use the plain send() path")

    async def send_test(self, device: Device) -> TransportResult:
        raise AssertionError(
            "the round-trip test endpoint must not use send_test() -- "
            "/v1/relay/test carries no handle/mutable-content"
        )

    async def send_situation(self, device: Device, **kw: object) -> TransportResult:
        self.calls += 1
        return TransportResult(ok=False, unregistered=self.unregistered, error="HTTP 410")


def _settings(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path, *, auth: bool = False
) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=auth
        ),
        # Left disabled so the lifespan doesn't start a real MQTT subscriber;
        # the engine is injected below, which is all the route reads.
        push=PushSection(enabled=False),
    )


def _client_with_engine(
    settings: Settings, transport: object | None = None
) -> tuple[TestClient, object]:
    transport = transport if transport is not None else LogTransport()
    app = create_app(settings)
    app.state.push_engine = PushEngine(
        db_path=str(settings.sidecar.db_path),
        transport=transport,  # type: ignore[arg-type]
        server_id="s_test",
    )
    return TestClient(app), transport


def _register(client: TestClient, token: str = TOKEN, **overrides: object) -> None:
    body: dict[str, object] = {
        "bundle_id": "com.pondhouse.Elsinore",
        "environment": "sandbox",
        "min_severity": "alert",
    }
    body.update(overrides)
    assert client.put(f"/v1/push/devices/{token}", json=body).status_code == 200


@pytest.fixture
def client(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> tuple[TestClient, LogTransport]:
    c, t = _client_with_engine(_settings(frigate_db_path, sidecar_db_path, tmp_path))
    return c, t  # type: ignore[return-value]


def test_test_push_to_a_registered_device(client: tuple[TestClient, LogTransport]) -> None:
    c, transport = client
    _register(c)
    r = c.post(f"/v1/push/devices/{TOKEN}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["card_key"].startswith(f"test:{TOKEN[:8]}:")
    assert isinstance(body["sent_ts"], (int, float))

    sends = [s for s in transport.sent if "test:" in str(s.get("collapse_id", ""))]
    assert sends, "no card send reached the transport"
    payload = sends[-1]["payload"]
    assert payload["v"] == 1
    assert payload["mutation"] == "test"
    assert payload["level"] == "notify"
    assert payload["camera"] == "elsinore"
    assert payload["glyph"] == "checkmark.seal"
    assert payload["primary"] == "Test alert"
    assert payload["secondary"] == "Round trip from your server"
    assert payload["aps"]["mutable-content"] == 1
    assert payload["aps"]["interruption-level"] == "active"
    assert "sound" in payload["aps"]
    assert payload["deep_link"].startswith("elsinore://doctor")
    assert sends[-1]["collapse_id"] == body["card_key"]


def test_test_push_is_recorded_for_receipt_pairing(client: tuple[TestClient, LogTransport]) -> None:
    from marcellus import db

    c, transport = client
    _register(c)
    body = c.post(f"/v1/push/devices/{TOKEN}/test").json()

    settings = c.app.state.settings
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM push_card_sends WHERE card_key = ?", (body["card_key"],)
        ).fetchone()
        assert row is not None
        assert row["apns_token"] == TOKEN
        assert row["mutation"] == "test"
        assert bool(row["ok"]) is True
        # No decision-trace row: a test push isn't a routing decision.
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM push_decisions WHERE card_key = ?",
                (body["card_key"],),
            ).fetchone()["n"]
            == 0
        )
    finally:
        conn.close()


def test_second_test_push_within_10s_is_rate_limited(
    client: tuple[TestClient, LogTransport],
) -> None:
    c, _ = client
    _register(c)
    assert c.post(f"/v1/push/devices/{TOKEN}/test").status_code == 200
    r = c.post(f"/v1/push/devices/{TOKEN}/test")
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "rate_limited"


def test_unknown_token_is_404(client: tuple[TestClient, LogTransport]) -> None:
    c, transport = client
    r = c.post("/v1/push/devices/never-registered/test")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "device_not_found"
    assert not transport.sent, "nothing should be sent for an unregistered token"


def test_filters_are_bypassed(client: tuple[TestClient, LogTransport]) -> None:
    """The point is to verify the APNs pipe, not the subscription (spec §1), so
    a device subscribed to one camera still gets its own test push."""
    c, transport = client
    _register(c, cameras=["garden"], labels=["car"], min_severity="alert")
    assert c.post(f"/v1/push/devices/{TOKEN}/test").status_code == 200


def test_environment_routing_is_not_bypassed(client: tuple[TestClient, LogTransport]) -> None:
    """A sandbox token sent to the production endpoint is silently black-holed,
    so the device row's own environment has to pick the endpoint exactly as a
    real send would -- otherwise the button proves nothing."""
    c, transport = client
    _register(c, environment="prod")
    assert c.post(f"/v1/push/devices/{TOKEN}/test").status_code == 200


def test_a_dead_token_is_reported(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """410/400 is permanent (spec §5), but `send_situation` (unlike the
    engine's real send path) doesn't itself prune the device row -- the
    round-trip test endpoint calls the transport directly. A real send later
    still discovers and prunes the same dead token."""
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    c, transport = _client_with_engine(settings, _RejectingTransport(unregistered=True))
    _register(c)
    r = c.post(f"/v1/push/devices/{TOKEN}/test")

    assert r.status_code == 502, "not 404 -- that means 'token not registered' to the client"
    assert r.json()["detail"]["error"] == "test_send_failed"


def test_a_transient_failure_keeps_the_device(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    c, _ = _client_with_engine(settings, _RejectingTransport(unregistered=False))
    _register(c)
    assert c.post(f"/v1/push/devices/{TOKEN}/test").status_code == 502

    from marcellus import db

    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert store.get_device(conn, TOKEN) is not None, (
            "a transient send failure must not unregister the device"
        )
    finally:
        conn.close()


def test_push_disabled_is_not_a_404(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    """Registration writes to the DB even with push off, so a registered token
    can exist with no engine running. Reporting that as 404 would tell the
    client the endpoint doesn't exist, when the truth is push is switched off."""
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path)
    c = TestClient(create_app(settings))  # no engine injected
    _register(c)
    r = c.post(f"/v1/push/devices/{TOKEN}/test")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "push_disabled"


def test_test_push_requires_auth_like_every_other_push_route(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    settings = _settings(frigate_db_path, sidecar_db_path, tmp_path, auth=True)
    c, _ = _client_with_engine(settings)
    test_push = c.post(f"/v1/push/devices/{TOKEN}/test")
    register = c.put(f"/v1/push/devices/{TOKEN}", json={"bundle_id": "x", "environment": "sandbox"})
    assert test_push.status_code == 401
    assert test_push.status_code == register.status_code, (
        "the test route must be gated exactly like the rest of /v1/push/*"
    )
