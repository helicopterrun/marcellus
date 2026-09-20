from fastapi.testclient import TestClient

from marcellus import __version__
from marcellus.server import create_app


def test_version_string() -> None:
    assert __version__.count(".") >= 1


def test_healthz() -> None:
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_version_endpoint() -> None:
    client = TestClient(create_app())
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["version"] == __version__
