"""GET /v1/encounters: `tag`, `label`, `place`, CSV `camera`, the stamp on the
wire, and how they combine with paging (docs/encounters.md "Feed API")."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.store import upsert_atom
from marcellus.models.wire import EncountersPeopleResponse as EncountersResponse
from marcellus.server import create_app


@pytest.fixture
def settings(tmp_path: Path, frigate_db_path: Path) -> Settings:
    cfg = tmp_path / "frigate-config.yml"
    cfg.write_text("cameras: {}\n")
    return Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=cfg, db_path=frigate_db_path
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", require_frigate_auth=False),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _seed(
    settings: Settings,
    atom_id: str,
    start: float,
    *,
    camera: str = "alley-wide",
    labels: tuple[str, ...] = ("person",),
    sub_labels: tuple[str, ...] = (),
    tag: str | None = None,
    place: str | None = None,
    outcome: str | None = None,
) -> str:
    """One single-atom encounter, then its stamp set directly (None = leave
    NULL, i.e. never stamped)."""
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        atom = Atom(
            atom_id=atom_id,
            camera=camera,
            start_time=start,
            end_time=start + 5,
            labels=labels,
            zones=(),
            event_ids=(f"ev-{atom_id}",),
            sub_labels=sub_labels,
            severity="detection",
        )
        enc = upsert_atom(conn, atom, LinkDecision(None, "new", 1.0), start)
        if tag is not None:
            conn.execute(
                "UPDATE encounters SET tag = ?, place = ?, outcome = ?, tag_reason = 'test' "
                "WHERE id = ?",
                (tag, place, outcome, enc),
            )
        conn.commit()
        return enc
    finally:
        conn.close()


def _ids(client: TestClient, **params: object) -> list[str]:
    resp = client.get("/v1/encounters", params=params)  # type: ignore[arg-type]
    assert resp.status_code == 200, resp.text
    return [e["id"] for e in resp.json()["encounters"]]


@pytest.fixture
def seeded(settings: Settings) -> dict[str, str]:
    now = time.time()
    return {
        "bg": _seed(settings, "r1", now - 500, tag="background", place="street", outcome="log"),
        "yard_cat": _seed(
            settings, "r2", now - 400, camera="shed", labels=("cat",),
            tag="notable", place="yard", outcome="glance",
        ),
        "unstamped": _seed(settings, "r3", now - 300, labels=("Car",)),
        "named": _seed(
            settings, "r4", now - 200, camera="gate", sub_labels=("Chris",),
            tag="notable", place="doors", outcome="notify",
        ),
    }


def test_stamp_is_on_the_wire(client: TestClient, seeded: dict[str, str]) -> None:
    resp = client.get("/v1/encounters")
    EncountersResponse.model_validate(resp.json())
    by_id = {e["id"]: e for e in resp.json()["encounters"]}
    assert {k: by_id[seeded["bg"]][k] for k in ("tag", "place", "outcome")} == {
        "tag": "background", "place": "street", "outcome": "log",
    }
    unstamped = by_id[seeded["unstamped"]]
    assert (unstamped["tag"], unstamped["place"], unstamped["outcome"]) == (None, None, None)
    assert "tag_reason" not in unstamped


def test_stamp_is_on_the_detail_wire(client: TestClient, seeded: dict[str, str]) -> None:
    body = client.get(f"/v1/encounters/{seeded['named']}").json()
    assert body["encounter"]["tag"] == "notable"
    assert body["encounter"]["place"] == "doors"


def test_tag_background(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, tag="background") == [seeded["bg"]]


def test_tag_notable_includes_unstamped(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, tag="notable") == [seeded["named"], seeded["unstamped"], seeded["yard_cat"]]


def test_tag_invalid_is_422(client: TestClient) -> None:
    assert client.get("/v1/encounters", params={"tag": "loud"}).status_code == 422


def test_label_matches_labels_case_insensitively(
    client: TestClient, seeded: dict[str, str]
) -> None:
    assert _ids(client, label="CAT") == [seeded["yard_cat"]]
    assert _ids(client, label="car") == [seeded["unstamped"]]


def test_label_matches_identities(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, label="chris") == [seeded["named"]]


def test_label_csv_is_any_of(client: TestClient, seeded: dict[str, str]) -> None:
    assert set(_ids(client, label="cat, chris")) == {seeded["yard_cat"], seeded["named"]}


def test_label_value_is_never_sql(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, label="x') OR 1=1 --") == []
    assert _ids(client, label="%") == []  # not a LIKE wildcard


def test_place_filter(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, place="yard") == [seeded["yard_cat"]]
    assert set(_ids(client, place="street,doors")) == {seeded["bg"], seeded["named"]}


def test_place_unknown_is_422(client: TestClient) -> None:
    assert client.get("/v1/encounters", params={"place": "yard,moon"}).status_code == 422


def test_camera_csv_and_single(client: TestClient, seeded: dict[str, str]) -> None:
    assert set(_ids(client, camera="shed,gate")) == {seeded["yard_cat"], seeded["named"]}
    assert _ids(client, camera="shed") == [seeded["yard_cat"]]
    assert set(_ids(client, camera="alley-wide")) == {seeded["bg"], seeded["unstamped"]}
    assert _ids(client, camera="alley") == []  # exact camera, not a substring


def test_camera_matches_any_camera_of_a_multi_camera_encounter(
    client: TestClient, settings: Settings
) -> None:
    now = time.time()
    enc = _seed(settings, "m1", now - 100)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        atom = Atom("m2", "porch", now - 90, now - 85, ("person",), (), ("ev-m2",), (), "detection")
        upsert_atom(conn, atom, LinkDecision(enc, "adjacent", 0.9), now)
    finally:
        conn.close()
    assert _ids(client, camera="porch") == [enc]
    assert _ids(client, camera="alley-wide") == [enc]


def test_filters_combine_with_and(client: TestClient, seeded: dict[str, str]) -> None:
    assert _ids(client, tag="notable", camera="shed") == [seeded["yard_cat"]]
    assert _ids(client, tag="notable", label="cat", place="doors") == []
    assert _ids(client, tag="background", camera="shed") == []
    assert _ids(client, severity="detection", tag="background", place="street") == [seeded["bg"]]


def test_filters_page_with_before_and_limit(client: TestClient, seeded: dict[str, str]) -> None:
    first = client.get("/v1/encounters", params={"tag": "notable", "limit": 2}).json()
    rows = first["encounters"]
    assert [r["id"] for r in rows] == [seeded["named"], seeded["unstamped"]]
    second = client.get(
        "/v1/encounters", params={"tag": "notable", "limit": 2, "before": rows[-1]["start"]}
    ).json()["encounters"]
    assert [r["id"] for r in second] == [seeded["yard_cat"]]  # limit counts filtered rows


def test_no_filters_unchanged(client: TestClient, seeded: dict[str, str]) -> None:
    assert len(_ids(client)) == 4


def test_decision_route_restamps_with_live_settings(
    settings: Settings, client: TestClient
) -> None:
    """Split goes through the production stamper: the new encounter comes
    back stamped, not NULL."""
    now = time.time()
    enc = _seed(settings, "s1", now - 100)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        atom = Atom("s2", "porch", now - 90, now - 85, ("person",), (), ("ev-s2",), (), "detection")
        upsert_atom(conn, atom, LinkDecision(enc, "adjacent", 0.9), now)
    finally:
        conn.close()
    resp = client.post(f"/v1/encounters/{enc}/atoms/s2/split")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()["encounter"]
    assert body["tag"] in ("background", "notable")
    assert body["place"] == "street"
    donor = client.get(f"/v1/encounters/{enc}").json()["encounter"]
    assert donor["tag"] in ("background", "notable")
