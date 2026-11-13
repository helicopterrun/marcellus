"""Per-install relay credential: storage, wire, secrecy, dead-token cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, PushSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.push.delivery_wire import handle_delivery_event
from marcellus.push.engine import PushEngine
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.transport import LogTransport, TransportResult
from marcellus.server import create_app

TOKEN = "tok-cred-1"
CRED = "v1.AbCdEf_-123.GhIjKl_-456"


def _upsert(conn: Any, **kw: Any) -> str:
    base: dict[str, Any] = dict(apns_token=TOKEN, bundle_id="com.x", environment="sandbox")
    base.update(kw)
    return store.upsert_device(conn, **base)


# -- A. storage ---------------------------------------------------------------


def test_upsert_keeps_stored_credential_when_incoming_is_empty(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    _upsert(conn, relay_credential=CRED)
    assert store.get_device(conn, TOKEN).relay_credential == CRED  # type: ignore[union-attr]
    _upsert(conn)  # no credential on this PUT
    assert store.get_device(conn, TOKEN).relay_credential == CRED  # type: ignore[union-attr]
    _upsert(conn, relay_credential="v1.new.value")
    assert store.get_device(conn, TOKEN).relay_credential == "v1.new.value"  # type: ignore[union-attr]


def test_cleared_push_to_start_token_is_not_resurrected_by_empty_registration(
    sidecar_db_path: Path,
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    _upsert(conn, push_to_start_token="pts-live")
    assert store.clear_push_to_start_token(conn, TOKEN) is True
    assert store.get_device(conn, TOKEN).push_to_start_token == ""  # type: ignore[union-attr]
    _upsert(conn)  # empty keeps the (now empty) value
    stored = store.get_device(conn, TOKEN)
    assert stored is not None and stored.push_to_start_token == ""
    _upsert(conn, push_to_start_token="pts-fresh")
    assert store.get_device(conn, TOKEN).push_to_start_token == "pts-fresh"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        (CRED, True),
        ("", False),
        ("v2.a.b", False),
        ("v1.has space.b", False),
        ("v1.café.b", False),
        ("v1.tab\t.b", False),
        (None, False),
    ],
)
def test_valid_relay_credential(value: Any, ok: bool) -> None:
    assert store.valid_relay_credential(value) is ok


def test_valid_relay_credential_length_boundary() -> None:
    assert store.valid_relay_credential("v1." + "a" * 2045) is True  # exactly 2048
    assert store.valid_relay_credential("v1." + "a" * 2046) is False


# -- B. wire + secrecy --------------------------------------------------------


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False),
        push=PushSection(enabled=False),
    )
    app = create_app(settings)
    app.state.push_engine = PushEngine(
        db_path=str(sidecar_db_path), transport=LogTransport(), server_id="s_test",
        frigate_base_url="",
    )
    return TestClient(app)


def _put(client: TestClient, **extra: Any) -> Any:
    body = {"bundle_id": "com.x", "environment": "sandbox", **extra}
    return client.put(f"/v1/push/devices/{TOKEN}", json=body)


def _stored(sidecar_db_path: Path) -> str:
    conn = db.open_sidecar(sidecar_db_path)
    try:
        return store.get_device(conn, TOKEN).relay_credential  # type: ignore[union-attr]
    finally:
        conn.close()


def test_registration_stores_credential(client: TestClient, sidecar_db_path: Path) -> None:
    assert _put(client, relay_credential=CRED).status_code == 200
    assert _stored(sidecar_db_path) == CRED
    # A later PUT without it keeps it.
    assert _put(client).status_code == 200
    assert _stored(sidecar_db_path) == CRED


@pytest.mark.parametrize("bad", ["garbage", "v1.has space", 12345, "v1." + "a" * 3000])
def test_invalid_credential_is_ignored_not_422(
    client: TestClient, sidecar_db_path: Path, bad: Any, caplog: pytest.LogCaptureFixture
) -> None:
    r = _put(client, relay_credential=bad)
    assert r.status_code == 200
    assert _stored(sidecar_db_path) == ""
    assert "garbage" not in caplog.text and "has space" not in caplog.text


def test_credential_never_in_any_response_or_log(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    put = _put(client, relay_credential=CRED)
    assert put.status_code == 200
    responses = [
        put.text,
        client.get(f"/v1/push/devices/{TOKEN}").text,
        client.get("/v1/push/status").text,
        client.get("/v1/push/decisions").text,
        client.get("/v1/capabilities").text,
        client.post(f"/v1/push/devices/{TOKEN}/test").text,
    ]
    for text in responses:
        assert CRED not in text
        assert "relay_credential" not in text.replace('"relay_credentials"', "")
    assert CRED not in caplog.text
    assert "relay_credential=present" in caplog.text


def test_capabilities_advertise_relay_credentials(client: TestClient) -> None:
    assert client.get("/v1/capabilities").json()["push"]["relay_credentials"] is True


# -- F. dead push-to-start / activity tokens -----------------------------------


class _DeadLaTransport(LogTransport):
    def __init__(self, *, dead_events: set[str]) -> None:
        super().__init__()
        self.dead_events = dead_events

    async def send_live_activity(self, device: Device, *, token: str, payload: Any,
                                 collapse_id: str, event: str, **kw: Any) -> TransportResult:
        await super().send_live_activity(
            device, token=token, payload=payload, collapse_id=collapse_id, event=event, **kw
        )
        if event in self.dead_events:
            return TransportResult(ok=False, unregistered=True, error="HTTP 410")
        return TransportResult(ok=True)


def _event() -> ReviewEvent:
    return ReviewEvent(
        review_id="r_doorbell_trk1", camera="doorbell", severity="alert",
        labels=("package",), track_ids=("trk1",), zones=("pool",),
    )


def _device_row(conn: Any) -> Device:
    _upsert(conn, push_to_start_token="pts1", min_severity="detection",
            frequent_pushes_enabled=True, relay_credential=CRED)
    dev = store.get_device(conn, TOKEN)
    assert dev is not None
    return dev


@pytest.mark.asyncio
async def test_dead_push_to_start_token_is_cleared_and_not_retried(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = _DeadLaTransport(dead_events={"start"})
    config = PushSection(delivery_enabled=True)
    device = _device_row(conn)
    await handle_delivery_event(
        _event(), conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    starts = [r for r in transport.sent if r.get("event") == "start"]
    assert len(starts) == 1
    stored = store.get_device(conn, TOKEN)
    assert stored is not None  # row survives
    assert stored.push_to_start_token == ""
    assert stored.relay_credential == CRED
    # Reloaded device now has no token: no further start attempt.
    await handle_delivery_event(
        _event(), conn=conn, devices=[stored], transport=transport, config=config, now=10.0,
    )
    assert len([r for r in transport.sent if r.get("event") == "start"]) == 1


@pytest.mark.asyncio
async def test_dead_activity_update_token_drops_the_activity_record(
    sidecar_db_path: Path,
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = _DeadLaTransport(dead_events={"update"})
    config = PushSection(delivery_enabled=True)
    device = _device_row(conn)
    await handle_delivery_event(
        _event(), conn=conn, devices=[device], transport=transport, config=config, now=0.0,
    )
    row = conn.execute("SELECT * FROM push_activities WHERE apns_token = ?", (TOKEN,)).fetchone()
    assert row is not None
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=TOKEN,
        situation_id=row["situation_id"], track_id=row["track_id"], token="perActivity1",
    )
    await handle_delivery_event(
        _event(), conn=conn, devices=[device], transport=transport, config=config, now=10.0,
    )
    updates = [r for r in transport.sent if r.get("event") == "update"]
    assert len(updates) == 1 and updates[0]["token"] == "perActivity1"
    assert conn.execute(
        "SELECT COUNT(*) FROM push_activities WHERE apns_token = ?", (TOKEN,)
    ).fetchone()[0] == 0
    assert store.get_device(conn, TOKEN) is not None
