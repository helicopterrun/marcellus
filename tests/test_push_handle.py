from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.push import store
from marcellus.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
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


def test_redeem_handle(client: TestClient, sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="1785123902.717381-2joc0p",
        review_id="r1", ttl_s=3600,
    )
    conn.commit()
    conn.close()

    r = client.get(f"/v1/push/handle/{handle}")
    assert r.status_code == 200
    body = r.json()
    assert body["camera"] == "doorbell"
    assert body["event_id"] == "1785123902.717381-2joc0p"
    assert body["snapshot_url"] == "/api/events/1785123902.717381-2joc0p/snapshot.jpg"


def test_redeem_unknown_handle_404(client: TestClient) -> None:
    r = client.get("/v1/push/handle/h_doesnotexist")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "handle_not_found"


def test_redeem_expired_handle_404(client: TestClient, sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="ev1", review_id="r1", ttl_s=1, now=1000.0,
    )
    conn.commit()
    conn.close()

    conn = db.open_sidecar(sidecar_db_path)
    data = store.redeem_handle(conn, handle, now=5000.0)
    conn.close()
    assert data is None

    r = client.get(f"/v1/push/handle/{handle}")
    assert r.status_code == 404


def test_no_raw_frigate_event_id_leak_in_review_id() -> None:
    """The handle maps to the actual Frigate event id, never the review id --
    they're deliberately distinct per the spec (a review id also embeds a
    wall-clock timestamp, same shape, but the two are not interchangeable)."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    from marcellus.db import SIDECAR_SCHEMA

    conn.row_factory = sqlite3.Row
    conn.executescript(SIDECAR_SCHEMA)
    handle = store.mint_handle(
        conn, camera="doorbell", event_id="event-123", review_id="review-456", ttl_s=3600
    )
    data = store.redeem_handle(conn, handle)
    assert data == {"camera": "doorbell", "event_id": "event-123"}


def test_prune_expired_handles() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from marcellus.db import SIDECAR_SCHEMA

    conn.executescript(SIDECAR_SCHEMA)
    store.mint_handle(conn, camera="a", event_id="e1", review_id="r1", ttl_s=1, now=0.0)
    store.mint_handle(conn, camera="b", event_id="e2", review_id="r2", ttl_s=1000, now=0.0)
    removed = store.prune_expired_handles(conn, now=500.0)
    assert removed == 1
