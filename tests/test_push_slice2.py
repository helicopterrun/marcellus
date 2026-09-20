"""Alerts-slice2 §A/§B/§C: `POST /v1/push/receipts`, `GET
/v1/push/devices/{token}` (device detail), and `GET /v1/push/status`'s
`relay` block."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import receipts as receipts_store
from marcellus.push import store
from marcellus.push.store import epoch_to_iso
from marcellus.push.transport import RELAY_HEALTH, reset_relay_health_for_tests
from marcellus.server import create_app

TOKEN = "tok-abc123"


@pytest.fixture(autouse=True)
def _reset_relay_health():
    reset_relay_health_for_tests()
    yield
    reset_relay_health_for_tests()


def _settings(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> Settings:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(yaml.safe_dump({"cameras": {}}))
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        push=PushSection(enabled=False, push_settings_path=str(tmp_path / "push_settings.json")),
    )


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    return TestClient(create_app(_settings(frigate_db_path, sidecar_db_path, tmp_path)))


@pytest.fixture
def sidecar_conn(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    yield conn
    conn.close()


def _register(client: TestClient, token: str = TOKEN, **overrides: object) -> None:
    body: dict[str, object] = {
        "bundle_id": "com.pondhouse.Elsinore", "environment": "sandbox", "min_severity": "alert",
    }
    body.update(overrides)
    assert client.put(f"/v1/push/devices/{token}", json=body).status_code == 200


# ---------------------------------------------------------------------------
# §A: receipts
# ---------------------------------------------------------------------------


def _receipt(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "apns_token": TOKEN,
        "card_key": "doorbell:package:t1",
        "mutation": "create",
        "state_since_ts": 1000.0,
        "received_ts": 1001.5,
        "media_attached": True,
        "source": "nse",
    }
    body.update(overrides)
    return body


def test_receipt_pairs_with_the_matching_send(client: TestClient, sidecar_conn: Any) -> None:
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="create",
        sent_at=1000.0,
    )
    resp = client.post("/v1/push/receipts", json={"receipts": [_receipt()]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "matched": 1}

    row = sidecar_conn.execute(
        "SELECT * FROM push_receipts WHERE card_key = 'doorbell:package:t1'"
    ).fetchone()
    assert row["sent_at"] == 1000.0
    assert row["latency_s"] == pytest.approx(1.5)
    assert bool(row["media_attached"]) is True
    assert row["source"] == "nse"


def test_receipt_with_no_matching_send_is_still_accepted(client: TestClient) -> None:
    resp = client.post("/v1/push/receipts", json={"receipts": [_receipt()]})
    assert resp.status_code == 200
    assert resp.json() == {"accepted": 1, "matched": 0}


def test_duplicate_receipt_is_ignored_not_an_error(client: TestClient, sidecar_conn: Any) -> None:
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="create",
        sent_at=1000.0,
    )
    r1 = client.post("/v1/push/receipts", json={"receipts": [_receipt()]})
    r2 = client.post("/v1/push/receipts", json={"receipts": [_receipt()]})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == {"accepted": 1, "matched": 1}
    # Duplicate: still accepted (not an error), but not re-counted as matched.
    assert r2.json() == {"accepted": 1, "matched": 0}

    n = sidecar_conn.execute(
        "SELECT COUNT(*) AS n FROM push_receipts WHERE card_key = 'doorbell:package:t1'"
    ).fetchone()["n"]
    assert n == 1, "the unique index must have deduped the second insert"


def test_receipt_for_unknown_token_is_stored_not_404(client: TestClient) -> None:
    resp = client.post(
        "/v1/push/receipts", json={"receipts": [_receipt(apns_token="never-registered")]}
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_receipt_pairing_only_looks_back_24h(client: TestClient, sidecar_conn: Any) -> None:
    """A send older than 24h before `received_ts` doesn't pair -- the
    fallback rule (no `state_since_ts` on the send side) is bounded so a
    years-old send for the same card_key/mutation never falsely pairs."""
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="create",
        sent_at=0.0,
    )
    resp = client.post(
        "/v1/push/receipts", json={"receipts": [_receipt(received_ts=1_000_000.0)]}
    )
    assert resp.json() == {"accepted": 1, "matched": 0}


# ---------------------------------------------------------------------------
# §B: device detail
# ---------------------------------------------------------------------------


def test_device_detail_404_for_unknown_token(client: TestClient) -> None:
    resp = client.get("/v1/push/devices/never-registered")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "device_not_found"


def test_device_detail_stats(client: TestClient, sidecar_conn: Any) -> None:
    import time as _time

    _register(client, environment="prod", app_version="1.0 (309)")
    now = _time.time()
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="create",
        sent_at=now - 10.0,
    )
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="escalate",
        sent_at=now - 5.0,
    )
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t2", mutation="create",
        sent_at=now - 3.0, ok=False, error="HTTP 500",
    )
    receipts_store.record(
        sidecar_conn,
        [
            {
                "apns_token": TOKEN, "card_key": "doorbell:package:t1", "mutation": "create",
                "state_since_ts": now - 10.0, "received_ts": now - 8.5,
                "media_attached": True, "source": "nse",
            }
        ],
    )

    resp = client.get(f"/v1/push/devices/{TOKEN}", params={"window_days": 90})
    assert resp.status_code == 200
    body = resp.json()
    assert body["registered"] is True
    assert body["environment"] == "prod"
    assert body["app_version"] == "1.0 (309)"
    assert body["sent"] == 3
    assert body["received"] == 1
    assert body["median_latency_s"] == pytest.approx(1.5)
    assert body["p90_latency_s"] == pytest.approx(1.5)
    assert body["last_sent_at"] == epoch_to_iso(now - 3.0)
    assert body["last_received_at"] == epoch_to_iso(now - 8.5)
    assert body["last_send_error"] == "HTTP 500"
    assert body["last_send_error_at"] == epoch_to_iso(now - 3.0)


def test_device_detail_stats_default_zero(client: TestClient) -> None:
    _register(client)
    resp = client.get(f"/v1/push/devices/{TOKEN}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 0
    assert body["received"] == 0
    assert body["median_latency_s"] is None
    assert body["p90_latency_s"] is None
    assert body["last_send_error"] is None


def test_device_detail_relay_is_device_scoped(client: TestClient, sidecar_conn: Any) -> None:
    """The device-detail `relay` block must reflect *this* device's own
    sends, not the process-global `RELAY_HEALTH` singleton -- otherwise a
    healthy device's Connection Doctor shows an unrelated device's failure
    (the bug this test guards against)."""
    import time as _time

    other_token = "tok-other-device"
    _register(client, environment="prod")
    store.upsert_device(
        sidecar_conn, apns_token=other_token, bundle_id="com.pondhouse.Elsinore",
        environment="prod", min_severity="detection",
    )
    now = _time.time()

    # This device (TOKEN) has only ever sent successfully.
    store.record_card_send(
        sidecar_conn, apns_token=TOKEN, card_key="doorbell:package:t1", mutation="create",
        sent_at=now - 5.0,
    )
    # A *different* device's send failed -- and updates the global singleton.
    store.record_card_send(
        sidecar_conn, apns_token=other_token, card_key="doorbell:package:t2", mutation="create",
        sent_at=now - 3.0, ok=False, error="HTTP 422: device_token must be hex",
    )
    RELAY_HEALTH.last_error = "HTTP 422: device_token must be hex"
    RELAY_HEALTH.last_error_at = now - 3.0
    RELAY_HEALTH.last_status_code = 422

    resp = client.get(f"/v1/push/devices/{TOKEN}", params={"window_days": 90})
    assert resp.status_code == 200
    relay = resp.json()["relay"]
    assert relay["last_error"] is None
    assert relay["last_error_at"] is None
    assert relay["last_ok_at"] == epoch_to_iso(now - 5.0)

    other_resp = client.get(f"/v1/push/devices/{other_token}", params={"window_days": 90})
    other_relay = other_resp.json()["relay"]
    assert other_relay["last_error"] == "HTTP 422: device_token must be hex"
    assert other_relay["last_ok_at"] is None


# ---------------------------------------------------------------------------
# §C: relay status
# ---------------------------------------------------------------------------


def test_status_carries_relay_health(client: TestClient) -> None:
    resp = client.get("/v1/push/status")
    assert resp.status_code == 200
    relay = resp.json()["relay"]
    assert relay == {
        "last_ok_at": None, "last_error": None, "last_error_at": None, "last_status_code": None,
    }

    RELAY_HEALTH.last_ok_at = 123.0
    RELAY_HEALTH.last_status_code = 200
    resp2 = client.get("/v1/push/status")
    relay2 = resp2.json()["relay"]
    assert relay2["last_ok_at"] == "1970-01-01T00:02:03Z"
    assert relay2["last_status_code"] == 200


@pytest.mark.asyncio
async def test_relay_transport_updates_health_on_success_and_failure() -> None:
    import httpx

    from marcellus.push.models import Device
    from marcellus.push.transport import RelayTransport

    device = Device(
        apns_token="tok", device_id="d_1", bundle_id="com.x", environment="prod",
        cameras=(), labels=(), min_severity="alert",
    )

    async def _handler(request: httpx.Request) -> httpx.Response:
        if "situation" in request.url.path:
            return httpx.Response(200)
        return httpx.Response(500, text="boom")

    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    transport = RelayTransport("http://relay.test", client=client, retry_attempts=1)
    try:
        ok = await transport.send_situation(device, payload={"aps": {}}, collapse_id="c1")
        assert ok.ok is True
        assert RELAY_HEALTH.last_ok_at is not None
        assert RELAY_HEALTH.last_status_code == 200

        bad = await transport.send(
            device, handle="h", server_id="s", severity="alert", collapse_id="c2",
        )
        assert bad.ok is False
        assert RELAY_HEALTH.last_error is not None
        assert RELAY_HEALTH.last_error_at is not None
    finally:
        await client.aclose()
