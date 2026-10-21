"""Review inbox: ranking, verdicts, undo, the Frigate+ outbox, and the routes."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from marcellus import db
from marcellus.config import FrigateSection, Settings, SidecarSection, TriageSection
from marcellus.frigate_api import FrigatePlusError
from marcellus.server import create_app
from marcellus.triage import inbox, outbox, plusmeta


def _meta(**over: Any) -> plusmeta.PlusMeta:
    meta = plusmeta.PlusMeta(
        ok=True,
        default_thresholds={"person": 0.6, "dog": 0.7, "car": 0.7},
        cameras=["alley-overview", "alley-east", "street-overview"],
        model_name="yolov9s",
        train_date="2026-01-01T00:00:00Z",
        labels={"person", "dog", "car", "squirrel"},
        verified_images={"alley-overview": 500, "alley-east": 500, "street-overview": 20,
                         "old-cam": 40},
    )
    for k, v in over.items():
        setattr(meta, k, v)
    return meta


@pytest.fixture
def settings(frigate_db_path: Path, sidecar_db_path: Path, monkeypatch: pytest.MonkeyPatch
             ) -> Settings:
    monkeypatch.setattr(plusmeta, "get", lambda s, refresh=False: _meta())
    return Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000", db_path=frigate_db_path),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001,
                               require_frigate_auth=False),
        triage=TriageSection(plus_delay_s=60.0),
    )


def _outbox_rows(settings: Settings) -> list[dict[str, Any]]:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM plus_outbox ORDER BY id")]
    finally:
        conn.close()


def _label(settings: Settings, event_id: str) -> dict[str, Any] | None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        r = conn.execute("SELECT * FROM triage_labels WHERE event_id = ?", (event_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


# ----- ranking -----


def _item(key: str, camera: str, *, prio: float, kind: str = "event", label: str = "person",
          light: str = "day") -> inbox.Item:
    return inbox.Item(key=key, kind=kind, camera=camera, label=label, ts=0.0, light=light,
                      priority=prio)


def test_select_caps_one_camera_at_forty_percent() -> None:
    items = [_item(f"a{i}", "a", prio=90, light=str(i)) for i in range(10)]
    for cam in "bcd":
        items += [_item(f"{cam}{i}", cam, prio=10, light=str(i)) for i in range(10)]
    picked = inbox.select(items, 10)
    assert sum(1 for i in picked if i.camera == "a") == 4
    assert len(picked) == 10


def test_select_fills_past_the_caps_rather_than_come_up_short() -> None:
    items = [_item(f"a{i}", "a", prio=90, light=str(i)) for i in range(10)]
    items += [_item(f"v{i}", f"c{i}", prio=95, kind="visit") for i in range(3)]
    assert len(inbox.select(items, 10)) == 10


def test_select_caps_wildlife_visits() -> None:
    items = [_item(f"v{i}", f"c{i}", prio=95, kind="visit") for i in range(10)]
    items += [_item(f"e{i}", f"d{i}", prio=50) for i in range(10)]
    picked = inbox.select(items, 10)
    assert sum(1 for i in picked if i.kind == "visit") == 6
    assert len(picked) == 10


def test_select_prefers_a_new_bucket_over_a_repeat() -> None:
    items = [
        _item("day1", "a", prio=80), _item("day2", "a", prio=79),
        _item("night", "a", prio=60, light="night"),
    ]
    assert [i.key for i in inbox.select(items, 2)] == ["day1", "night"]


def test_build_offers_borderline_rare_and_coverage_but_not_confident_or_old(
    settings: Settings,
) -> None:
    settings.triage.rare_label_max = 1  # the fixture's two alley-overview persons aren't rare
    data = inbox.build(settings)
    by_id = {i["event_id"]: {r["code"] for r in i["reasons"]} for i in data["items"]}
    # e1: person 0.94 on a well-covered camera, threshold 0.6 -> nothing to learn.
    assert "e1" not in by_id
    # e2: person 0.65 vs 0.6 -> borderline.
    assert "borderline" in by_id["e2"]
    # e3: the only dog on alley-east this week -> rare.
    assert "rare" in by_id["e3"]
    # e4: street-overview has 20 of 100 verified images -> coverage.
    assert "coverage" in by_id["e4"]
    # e5 is 30 days old, outside lookback_days.
    assert "e5" not in by_id


def test_build_budget_counts_down_through_the_day(settings: Settings) -> None:
    settings.triage.daily_budget = 3
    first = inbox.build(settings)
    assert len(first["items"]) == 3
    for it in first["items"][:2]:
        inbox.apply_verdict(settings, key=it["key"], verdict="skip", plus_enabled=True)
    second = inbox.build(settings)
    assert second["done_today"] == 2
    assert len(second["items"]) == 1
    assert len(inbox.build(settings, more=True)["items"]) <= 3


def test_phone_feedback_becomes_a_top_reason(settings: Settings) -> None:
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        conn.execute(
            "INSERT INTO push_decisions (ts, camera, label, subject, place, level, event_id, "
            "card_key) VALUES ('now', 'alley-overview', 'person', 'person', 'yard', 'glance', "
            "'e1', 'card-1')"
        )
        conn.execute(
            "INSERT INTO push_feedback (card_key, verdict, received_at) "
            "VALUES ('card-1', 'not_useful', ?)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()
    first = inbox.build(settings)["items"][0]
    assert first["event_id"] == "e1"
    assert first["reasons"][0]["code"] == "phone"


# ----- verdicts -----


def test_real_verdict_records_label_and_queues_a_boxed_upload(settings: Settings) -> None:
    res = inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    assert res["status"] == "queued"
    assert res["send_in_s"] == 60
    assert _label(settings, "e2")["label"] == "tp"
    assert _label(settings, "e2")["source"] == "inbox"
    rows = _outbox_rows(settings)
    assert [(r["kind"], r["status"], r["event_id"]) for r in rows] == [("tp", "queued", "e2")]


def test_wrong_label_is_a_false_positive_with_the_right_name(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e3", verdict="wrong", correct_label="cat",
                        plus_enabled=True)
    lab = _label(settings, "e3")
    assert (lab["label"], lab["correct_label"]) == ("fp", "cat")
    assert _outbox_rows(settings)[0]["kind"] == "fp"


def test_relabel_replaces_the_queued_upload(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    inbox.apply_verdict(settings, key="event:e2", verdict="fp", plus_enabled=True)
    assert [(r["kind"], r["status"]) for r in _outbox_rows(settings)] == [
        ("tp", "cancelled"), ("fp", "queued"),
    ]


def test_undo_cancels_the_upload_and_clears_the_label(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    assert inbox.undo(settings, key="event:e2") == {"ok": True}
    assert _label(settings, "e2") is None
    assert _outbox_rows(settings)[0]["status"] == "cancelled"


def test_unsubmittable_event_keeps_the_label_and_says_why(
    settings: Settings, frigate_db_path: Path
) -> None:
    import sqlite3

    conn = sqlite3.connect(frigate_db_path)
    conn.execute("UPDATE event SET has_snapshot = 0 WHERE id = 'e2'")
    conn.commit()
    conn.close()
    res = inbox.apply_verdict(settings, key="event:e2", verdict="fp", plus_enabled=True)
    assert (res["status"], res["reason"]) == ("not_sent", "no_snapshot")
    assert _label(settings, "e2")["label"] == "fp"
    assert _outbox_rows(settings) == []


def test_false_alarm_on_an_event_already_sent_as_real_is_refused(
    settings: Settings, frigate_db_path: Path
) -> None:
    import sqlite3

    conn = sqlite3.connect(frigate_db_path)
    conn.execute("UPDATE event SET plus_id = 'p1' WHERE id = 'e2'")
    conn.commit()
    conn.close()
    res = inbox.apply_verdict(settings, key="event:e2", verdict="fp", plus_enabled=True)
    assert res["reason"] == "sent_as_real"


def test_plus_disabled_records_only(settings: Settings) -> None:
    res = inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=False)
    assert res["status"] == "off"
    assert _outbox_rows(settings) == []


def test_bad_key_and_verdict_are_rejected(settings: Settings) -> None:
    with pytest.raises(inbox.VerdictError):
        inbox.apply_verdict(settings, key="nope", verdict="tp", plus_enabled=True)
    with pytest.raises(inbox.VerdictError):
        inbox.apply_verdict(settings, key="event:e2", verdict="maybe", plus_enabled=True)


def _seed_visit(settings: Settings) -> int:
    now = time.time()
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        vid = int(conn.execute(
            "INSERT INTO wildlife_visits (camera, start_ts, end_ts, hits, created_at) "
            "VALUES ('alley-east', ?, ?, 3, 'now')", (now - 600, now - 560),
        ).lastrowid or 0)
        for i, det in enumerate((0.9, 0.6, 0.3)):
            conn.execute(
                "INSERT INTO wildlife_boxes (camera, segment_ts, frame_ts, x1, y1, x2, y2, "
                "det_score, verdict, suggestion, suggestion_score, visit_id, crop_path, "
                "frame_path, created_at) VALUES ('alley-east', ?, ?, .1, .1, .3, .4, ?, "
                "'animal', 'squirrel', .9, ?, 'c.jpg', 'f.jpg', 'now')",
                (now - 600, now - 600 + i, det, vid),
            )
        conn.commit()
        return vid
    finally:
        conn.close()


def test_visit_is_offered_and_keeping_it_queues_its_best_frames(settings: Settings) -> None:
    vid = _seed_visit(settings)
    items = inbox.build(settings)["items"]
    visit = next(i for i in items if i["kind"] == "visit")
    assert visit["label"] == "squirrel"
    assert visit["reasons"][0]["code"] == "missed"
    res = inbox.apply_verdict(settings, key=f"visit:{vid}", verdict="tp", plus_enabled=True)
    assert res["frames"] == 2  # wildlife_frames_per_visit
    rows = _outbox_rows(settings)
    assert {r["kind"] for r in rows} == {"frame"}
    # Undo while still queued puts the visit back.
    assert inbox.undo(settings, key=f"visit:{vid}") == {"ok": True}
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert conn.execute("SELECT review FROM wildlife_visits").fetchone()[0] == "pending"
        assert {r[0] for r in conn.execute("SELECT plus_status FROM wildlife_boxes")} == {None}
    finally:
        conn.close()


# ----- outbox -----


class _FakeClient:
    def __init__(self, fail: list[FrigatePlusError] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail = list(fail or [])

    def _maybe_fail(self) -> None:
        if self.fail:
            raise self.fail.pop(0)

    def submit_plus(self, event_id: str, *, include_annotation: bool = True) -> str:
        self.calls.append(("tp", event_id))
        self._maybe_fail()
        return "plus-" + event_id

    def submit_false_positive(self, event_id: str) -> str:
        self.calls.append(("fp", event_id))
        self._maybe_fail()
        return "plus-" + event_id

    def submit_plus_frame(self, camera: str, frame_ts: float, *, timeout: float = 60.0) -> str:
        self.calls.append(("frame", camera))
        self._maybe_fail()
        return "submitted"


def test_drain_waits_out_the_undo_window(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    fake = _FakeClient()
    assert outbox.drain(settings, client=fake)["sent"] == 0
    assert outbox.drain(settings, client=fake, now=time.time() + 61)["sent"] == 1
    row = _outbox_rows(settings)[0]
    assert (row["status"], row["plus_id"]) == ("sent", "plus-e2")
    assert fake.calls == [("tp", "e2")]


def test_drain_retries_transient_failures_with_backoff(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    fake = _FakeClient([FrigatePlusError("submit_plus: Error uploading image", 400)])
    later = time.time() + 61
    assert outbox.drain(settings, client=fake, now=later)["retry"] == 1
    row = _outbox_rows(settings)[0]
    assert (row["status"], row["attempts"]) == ("queued", 1)
    assert row["not_before"] > later
    assert outbox.drain(settings, client=fake, now=row["not_before"] + 1)["sent"] == 1


def test_drain_treats_already_submitted_as_done(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    fake = _FakeClient([FrigatePlusError("submit_plus: Already submitted to plus", 400)])
    assert outbox.drain(settings, client=fake, now=time.time() + 61)["sent"] == 1


def test_drain_gives_up_on_a_permanent_error_and_retry_requeues(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    fake = _FakeClient([FrigatePlusError("submit_plus: Event e2 not found", 404)])
    assert outbox.drain(settings, client=fake, now=time.time() + 61)["error"] == 1
    row = _outbox_rows(settings)[0]
    assert row["status"] == "error"
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert outbox.retry(conn, row["id"])
        conn.commit()
    finally:
        conn.close()
    assert outbox.drain(settings, client=fake)["sent"] == 1


def test_drain_stops_retrying_at_max_attempts(settings: Settings) -> None:
    settings.triage.outbox_max_attempts = 2
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    fake = _FakeClient([FrigatePlusError("timeout", 0)] * 5)
    outbox.drain(settings, client=fake, now=time.time() + 61)
    outbox.drain(settings, client=fake, now=time.time() + 10_000)
    assert _outbox_rows(settings)[0]["status"] == "error"


def test_drain_skips_a_row_undone_meanwhile(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    inbox.undo(settings, key="event:e2")
    fake = _FakeClient()
    outbox.drain(settings, client=fake, now=time.time() + 61)
    assert fake.calls == []


def test_drain_marks_wildlife_frames_sent(settings: Settings) -> None:
    vid = _seed_visit(settings)
    inbox.apply_verdict(settings, key=f"visit:{vid}", verdict="tp", plus_enabled=True)
    outbox.drain(settings, client=_FakeClient(), now=time.time() + 61)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        statuses = sorted(str(r[0]) for r in conn.execute(
            "SELECT plus_status FROM wildlife_boxes"))
    finally:
        conn.close()
    assert statuses == ["None", "sent", "sent"]
    assert inbox.undo(settings, key=f"visit:{vid}")["reason"] == "already_sent"


def test_summary_reports_mix(settings: Settings) -> None:
    inbox.apply_verdict(settings, key="event:e2", verdict="tp", plus_enabled=True)
    inbox.apply_verdict(settings, key="event:e3", verdict="fp", plus_enabled=True)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        assert outbox.summary(conn)["queued"] == 2
    finally:
        conn.close()
    outbox.drain(settings, client=_FakeClient(), now=time.time() + 61)
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        s = outbox.summary(conn)
    finally:
        conn.close()
    assert (s["queued"], s["sent_30d"], s["real_share"]) == (0, 2, 0.5)


# ----- metadata -----


def test_parse_reads_thresholds_and_verified_counts() -> None:
    meta = plusmeta.parse({
        "objects": {"filters": {"person": {"threshold": 0.7}}},
        "cameras": {"gate": {"objects": {"filters": {"squirrel": {"threshold": 0.55}}}}},
        "model": {"plus": {"name": "m", "trainDate": "2026-10-16T18:04:42.171Z",
                           "labelMap": {"0": "person"},
                           "verifiedCounts": {"gate": {"images": 12, "person": 12}}}},
    })
    assert meta.threshold("gate", "squirrel") == 0.55
    assert meta.threshold("gate", "person") == 0.7
    assert meta.threshold("gate", "cat") == 0.7
    assert meta.verified_images == {"gate": 12}
    assert meta.labels == {"person"}
    assert meta.train_ts() is not None


def test_aliases_fold_renamed_cameras(settings: Settings) -> None:
    settings.triage.plus_camera_aliases = {"street-overview": ["old-cam"]}
    meta = _meta()
    assert plusmeta.camera_images(settings, meta)["street-overview"] == 60
    assert "old-cam" not in plusmeta.retired_cameras(settings, meta)


# ----- routes -----


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    tc = TestClient(app)
    app.state.plus_enabled = True
    return tc


def test_inbox_page_renders_cards(client: TestClient) -> None:
    r = client.get("/inbox")
    assert r.status_code == 200
    assert 'data-key="event:e2"' in r.text
    assert "reviewed today" in r.text


def test_verdict_and_undo_routes(client: TestClient) -> None:
    r = client.post("/inbox/verdict", json={"key": "event:e2", "verdict": "tp"})
    assert r.status_code == 200
    body = r.json()
    assert body["plus"]["status"] == "queued"
    assert body["outbox"]["queued"] == 1
    r = client.post("/inbox/undo", json={"key": "event:e2"})
    assert r.json()["ok"] is True
    bad = client.post("/inbox/verdict", json={"key": "event:e2", "verdict": "x"})
    assert bad.status_code == 400
    missing = client.post("/inbox/verdict", json={"key": "event:zz", "verdict": "tp"})
    assert missing.status_code == 404


def test_coverage_page(client: TestClient) -> None:
    client.post("/inbox/verdict", json={"key": "event:e4", "verdict": "fp"})
    r = client.get("/inbox/coverage")
    assert r.status_code == 200
    assert "street-overview" in r.text
    assert "old-cam" in r.text  # retired camera listed


def test_label_route_queues_through_the_outbox(client: TestClient, settings: Settings) -> None:
    r = client.post("/label", json={"event_id": "e2", "label": "tp", "submit_plus": True})
    assert r.json()["plus"]["status"] == "queued"
    client.post("/clear-label", json={"event_id": "e2"})
    assert _outbox_rows(settings)[0]["status"] == "cancelled"


def test_push_feedback_is_stored(client: TestClient, settings: Settings) -> None:
    r = client.post("/v1/push/feedback", json={"card_key": "c1", "verdict": "not_useful"})
    assert r.status_code == 200
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        row = conn.execute("SELECT card_key, verdict FROM push_feedback").fetchone()
    finally:
        conn.close()
    assert tuple(row) == ("c1", "not_useful")
