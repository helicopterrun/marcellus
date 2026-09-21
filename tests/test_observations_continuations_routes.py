"""routes/observations.py: GET /v1/observations/{atom_id}/continuations
(docs/encounters.md "Suggested continuations")."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import EncountersSection, FrigateSection, Settings, SidecarSection
from marcellus.encounters.linker import Atom, LinkDecision
from marcellus.encounters.observations import Direction
from marcellus.encounters.store import add_decision, upsert_atom
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
        encounters=EncountersSection(adjacency=[["alley-wide", "shed"]]),
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _seed_transition(
    settings: Settings,
    *,
    cam_a: str,
    cam_b: str,
    family: str,
    p10: float,
    p50: float,
    p90: float,
    source: str = "learned",
    samples: int = 20,
) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO camera_transitions (cam_a, cam_b, family, samples, p10_s, "
            "p50_s, p90_s, source, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cam_a, cam_b, family, samples, p10, p50, p90, source, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_atom(
    settings: Settings,
    atom_id: str,
    *,
    camera: str,
    start_offset: float,
    end_offset: float,
    label: str = "person",
    encounter_id: str | None = None,
    reason: str = "new",
    direction: Direction | None = None,
    now: float | None = None,
) -> tuple[str, float, float]:
    now = now if now is not None else time.time()
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        start = now + start_offset
        end = now + end_offset
        atom = Atom(
            atom_id=atom_id,
            camera=camera,
            start_time=start,
            end_time=end,
            labels=(label,),
            zones=(),
            event_ids=(f"ev-{atom_id}",),
            sub_labels=(),
            severity="alert",
        )
        enc_id = upsert_atom(
            conn,
            atom,
            LinkDecision(encounter_id, reason, 1.0),
            now,
            direction=direction,
        )
        return enc_id, start, end
    finally:
        conn.close()


def test_404_for_unknown_atom(client: TestClient) -> None:
    resp = client.get("/v1/observations/does-not-exist/continuations")
    assert resp.status_code == 404


def test_linked_sibling_on_adjacent_camera_is_confirmed(
    settings: Settings, client: TestClient
) -> None:
    now = time.time()
    enc_id, _s0, _e0 = _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    _seed_atom(
        settings,
        "sib",
        camera="shed",
        start_offset=-80.0,
        end_offset=-70.0,
        encounter_id=enc_id,
        reason="adjacent",
        now=now,
    )
    _seed_transition(
        settings, cam_a="alley-wide", cam_b="shed", family="person", p10=5.0, p50=10.0, p90=20.0
    )

    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["from"]["id"] == "src"
    assert body["from"]["encounter_id"] == enc_id
    confirmed = [s for s in body["suggestions"] if s["bucket"] == "confirmed"]
    assert confirmed
    assert confirmed[0]["observation_id"] == "sib"
    assert confirmed[0]["encounter_id"] == enc_id


def test_overlapping_sibling_on_adjacent_camera_is_confirmed_no_duplicate_prediction(
    settings: Settings, client: TestClient
) -> None:
    """Overlapping hand-off: alley-wide starts seeing the same person ~2s
    after stairway-wide (source, 30s long) STARTED, while stairway-wide is
    still seeing them, and the linker already joined both atoms onto the
    same encounter. The old candidate search only started at the source's
    END, so this candidate -- and the whole overlap case -- was invisible
    to it and it would fall back to a pure prediction for that camera. The
    wider `candidate_search_window` (source start onward) must find it as a
    real candidate: exactly one row for the shed camera, bucketed
    `confirmed`, `observation_id` set, and no separate prediction row for
    that same camera."""
    now = time.time()
    enc_id, _s0, _e0 = _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-70.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    _seed_atom(
        settings,
        "sib",
        camera="shed",
        start_offset=-98.0,
        end_offset=-90.0,
        encounter_id=enc_id,
        reason="adjacent",
        now=now,
    )
    _seed_transition(
        settings, cam_a="alley-wide", cam_b="shed", family="person", p10=5.0, p50=10.0, p90=20.0
    )

    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    shed_rows = [s for s in body["suggestions"] if s["camera"] == "shed"]
    assert len(shed_rows) == 1
    row = shed_rows[0]
    assert row["bucket"] == "confirmed"
    assert row["observation_id"] == "sib"
    assert row["encounter_id"] == enc_id
    assert any("overlapped" in w for w in row["why"])


def test_member_on_different_encounter_is_likely_or_possible_with_ids(
    settings: Settings, client: TestClient
) -> None:
    now = time.time()
    _enc_id, _s0, _e0 = _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    _seed_atom(
        settings,
        "other",
        camera="shed",
        start_offset=-80.0,
        end_offset=-70.0,
        now=now,
    )

    _seed_transition(
        settings, cam_a="alley-wide", cam_b="shed", family="person", p10=5.0, p50=10.0, p90=20.0
    )
    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    matches = [s for s in body["suggestions"] if s["observation_id"] == "other"]
    assert matches
    match = matches[0]
    assert match["bucket"] in ("likely", "possible")
    assert match["encounter_id"] is not None


def test_pinned_candidate_is_confirmed(settings: Settings, client: TestClient) -> None:
    now = time.time()
    enc_id, _s0, _e0 = _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    _seed_atom(
        settings,
        "other",
        camera="shed",
        start_offset=-80.0,
        end_offset=-70.0,
        now=now,
    )
    _seed_transition(
        settings, cam_a="alley-wide", cam_b="shed", family="person", p10=5.0, p50=10.0, p90=20.0
    )
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        add_decision(conn, "other", "pin", enc_id, None, now)
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/v1/observations/src/continuations")
    body = resp.json()
    match = next(s for s in body["suggestions"] if s["observation_id"] == "other")
    assert match["bucket"] == "confirmed"


def test_no_member_in_window_yields_prediction_with_null_ids(
    settings: Settings, client: TestClient
) -> None:
    now = time.time()
    _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )

    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    predictions = [s for s in body["suggestions"] if s["observation_id"] is None]
    assert predictions
    pred = predictions[0]
    assert pred["encounter_id"] is None
    assert pred["start"] is None
    assert pred["bucket"] != "confirmed"


def test_no_transition_row_yields_default_timing_with_null_p50(
    settings: Settings, client: TestClient
) -> None:
    now = time.time()
    _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )

    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["suggestions"]
    for s in body["suggestions"]:
        assert s["timing"] == {"source": "default", "samples": 0, "p50": None}


def test_learned_transition_row_yields_learned_timing_with_samples(
    settings: Settings, client: TestClient
) -> None:
    now = time.time()
    _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    _seed_transition(
        settings,
        cam_a="alley-wide",
        cam_b="shed",
        family="person",
        p10=5.0,
        p50=10.0,
        p90=20.0,
        samples=15,
    )

    resp = client.get("/v1/observations/src/continuations")
    assert resp.status_code == 200
    body = resp.json()
    shed_rows = [s for s in body["suggestions"] if s["camera"] == "shed"]
    assert shed_rows
    for s in shed_rows:
        assert s["timing"]["source"] == "learned"
        assert s["timing"]["samples"] == 15
        assert s["timing"]["p50"] == 10.0


def test_limit_caps_suggestion_count(settings: Settings, client: TestClient) -> None:
    now = time.time()
    _seed_atom(
        settings,
        "src",
        camera="alley-wide",
        start_offset=-100.0,
        end_offset=-90.0,
        direction=Direction("front_garden", "back_walkway", "out:shed", None, "zones"),
        now=now,
    )
    for i in range(3):
        _seed_atom(
            settings,
            f"other{i}",
            camera="shed",
            start_offset=-80.0 + i,
            end_offset=-70.0 + i,
            now=now,
        )

    resp = client.get("/v1/observations/src/continuations?limit=1")
    assert resp.status_code == 200
    assert len(resp.json()["suggestions"]) <= 1
