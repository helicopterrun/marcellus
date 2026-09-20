from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus.config import FrigateSection, Settings, SidecarSection
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


def test_toybox_page_renders(client: TestClient) -> None:
    r = client.get("/toybox")
    assert r.status_code == 200
    assert "50 states quiz" in r.text
    # Seeded example high score is on the board.
    assert "BOB1" in r.text


def test_scores_seeded(client: TestClient) -> None:
    r = client.get("/toybox/scores")
    assert r.status_code == 200
    board = r.json()["scores"]
    assert board[0]["name"] == "BOB1"
    assert board[0]["score"] == 30


def test_submit_orders_and_sanitizes(client: TestClient) -> None:
    # Lowercase + punctuation gets normalized to uppercase alnum, capped at 8.
    r = client.post("/toybox/scores", json={"name": "al ice!#9", "score": 47})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["name"] == "ALICE9"  # spaces/punct stripped
    # Higher score sorts above the seeded BOB1.
    assert body["scores"][0]["name"] == "ALICE9"
    assert body["rank"] == 1


def test_submit_rejects_out_of_range(client: TestClient) -> None:
    assert client.post("/toybox/scores", json={"name": "X", "score": 51}).status_code == 422
    assert client.post("/toybox/scores", json={"name": "X", "score": -1}).status_code == 422


def test_submit_rejects_empty_name(client: TestClient) -> None:
    # All-punctuation collapses to empty after sanitizing -> 400.
    assert client.post("/toybox/scores", json={"name": "!!!", "score": 10}).status_code == 400
