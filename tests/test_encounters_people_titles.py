"""Encounter titles/identities use read-time recognized people (name, else handle)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FaceEnrichSection, FrigateSection, Settings, SidecarSection
from marcellus.faces import enrich
from marcellus.faces.handles import handle_for
from marcellus.server import create_app
from tests.test_encounters_person_filter import _face
from tests.test_encounters_routes import _seed_at

NOW = time.time()


@pytest.fixture
def settings(tmp_path: Path, frigate_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text(
        "cameras:\n  alley-wide:\n    zones:\n      back_walkway:\n"
        "        coordinates: '0,0,1,0,1,1,0,1'\n"
    )
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
        face_enrich=FaceEnrichSection(enabled=True),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _name(settings: Settings, cid: int, name: str) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert enrich.rename_cluster(conn, cid, name)
        conn.commit()
    finally:
        conn.close()


def _first(client: TestClient) -> dict:  # type: ignore[type-arg]
    return client.get("/v1/encounters").json()["encounters"][0]  # type: ignore[no-any-return]


def test_named_person_in_title(settings: Settings, client: TestClient) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    row = _first(client)
    assert row["title"] == "Sam near Back Walkway"
    assert row["identities"] == ["Sam"]


def test_unnamed_person_shows_handle_not_identity(settings: Settings, client: TestClient) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _face(settings, "a1", 1)
    row = _first(client)
    handle = handle_for(1)
    assert row["title"] == f"{handle} near Back Walkway"
    assert row["identities"] == []  # handles are not names


def test_two_people_named_first(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _seed_at(settings, "a2", start=NOW - 95, zones=("back_walkway",), encounter_id=enc)
    _face(settings, "a1", 1)  # unnamed
    _face(settings, "a2", 2)
    _name(settings, 2, "Sam")
    row = _first(client)
    assert row["title"] == f"Sam and {handle_for(1)} near Back Walkway"
    assert [p["id"] for p in row["people"]] == [2, 1]


def test_person_with_dog(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _seed_at(
        settings, "a2", start=NOW - 95, labels=("dog",), zones=("back_walkway",), encounter_id=enc
    )
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    assert _first(client)["title"] == "Sam and dog near Back Walkway"


def test_identities_dedupe_case_insensitive_against_stored(
    settings: Settings, client: TestClient
) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",), sub_labels=("sam",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    row = _first(client)
    assert row["identities"] == ["sam"]  # stored string kept, no duplicate
    assert row["title"] == "Sam near Back Walkway"


def test_brand_identity_still_works_with_person(settings: Settings, client: TestClient) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",), sub_labels=("amazon",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    row = _first(client)
    assert row["title"] == "Sam and Amazon near Back Walkway"
    assert row["identities"] == ["amazon", "Sam"]


def test_detail_route_title_uses_people(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    detail = client.get(f"/v1/encounters/{enc}").json()["encounter"]
    assert detail["title"] == "Sam near Back Walkway"
    assert detail["identities"] == ["Sam"]
    assert detail["people"][0]["name"] == "Sam"


def test_html_pages_use_people_titles(settings: Settings, client: TestClient) -> None:
    enc = _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    assert "Sam near Back Walkway" in client.get("/encounters").text
    assert "Sam near Back Walkway" in client.get(f"/encounters/{enc}").text


def test_no_people_when_face_enrich_disabled(settings: Settings) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",))
    _face(settings, "a1", 1)
    _name(settings, 1, "Sam")
    off = settings.model_copy(update={"face_enrich": FaceEnrichSection(enabled=False)})
    row = _first(TestClient(create_app(off)))
    assert row["title"] == "Person near Back Walkway"
    assert row["identities"] == []


def test_no_faces_keeps_old_title(settings: Settings, client: TestClient) -> None:
    _seed_at(settings, "a1", start=NOW - 100, zones=("back_walkway",), sub_labels=("alice",))
    row = _first(client)
    assert row["title"] == "Alice near Back Walkway"
    assert row["identities"] == ["alice"]
