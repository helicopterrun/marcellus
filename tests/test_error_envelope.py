"""Every 4xx/5xx JSON body the app returns must use the shared error
envelope: `{"detail": {"error": <str>, "message": <str>}}`. FastAPI's own
default (`{"detail": "<str>"}`) or a raw dict missing either key is a bug.

One representative failing request per router is exercised here, plus the
FastAPI-generated `RequestValidationError` path (422, additive `errors`
list) and the custom Frigate-DB-missing 503 handler in `server.py`.
"""

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
    return TestClient(create_app(settings), raise_server_exceptions=False)


def assert_envelope(response, expect_status: int | None = None) -> None:
    """Shared assertion: `detail.error` and `detail.message` are both str."""
    if expect_status is not None:
        assert response.status_code == expect_status
    assert 400 <= response.status_code < 600
    body = response.json()
    assert "detail" in body, body
    detail = body["detail"]
    assert isinstance(detail, dict), detail
    assert isinstance(detail.get("error"), str), detail
    assert isinstance(detail.get("message"), str), detail


# One known-failing request per router that raises `HTTPException`.
_CASES = [
    ("enrich", "GET", "/enrich/thumb/does-not-exist", None, 404),
    ("face_captures", "GET", "/faces/captures/999999/thumb.jpg", None, 404),
    ("guide", "GET", "/guide/not-a-real-topic", None, 404),
    ("push_map", "GET", "/v1/push/map/track?camera=nope&event_id=e1", None, 404),
    (
        "push",
        "POST",
        "/v1/push/feedback",
        {"json": {"card_key": "x"}},
        400,
    ),
    ("proxy", "GET", "/v1/does/not/exist", None, 404),
    ("toybox", "GET", "/toybox/scores?game=not-a-game", None, 404),
    ("triage", "GET", "/event/does-not-exist", None, 404),
    ("status", "GET", "/live/not-a-camera", None, 404),
]


@pytest.mark.parametrize("name,method,path,kwargs,status", _CASES, ids=[c[0] for c in _CASES])
def test_error_envelope_per_router(
    client: TestClient, name: str, method: str, path: str, kwargs: dict | None, status: int
) -> None:
    r = client.request(method, path, **(kwargs or {}))
    assert_envelope(r, expect_status=status)


def test_validation_error_envelope(client: TestClient) -> None:
    # `days` is declared `ge=1` on this endpoint; -1 fails FastAPI's own
    # request validation before the route body ever runs.
    r = client.get("/analysis/score-histogram", params={"days": -1})
    assert_envelope(r, expect_status=422)
    detail = r.json()["detail"]
    assert detail["error"] == "validation"
    assert isinstance(detail["errors"], list) and detail["errors"]


def test_frigate_db_missing_envelope(tmp_path: Path, sidecar_db_path: Path) -> None:
    # A frigate.db_path pointing nowhere: routes that open it hit
    # `FrigateDBMissingError`, and the custom handler in server.py must
    # return the same envelope for a non-HTML (API) caller.
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=tmp_path / "missing-frigate.db",
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    missing_client = TestClient(create_app(settings), raise_server_exceptions=False)
    r = missing_client.get("/analysis/pull-events", headers={"accept": "application/json"})
    assert_envelope(r, expect_status=503)
    assert r.json()["detail"]["error"] == "frigate_db_missing"
