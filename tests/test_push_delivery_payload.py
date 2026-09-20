"""Payload construction and send orchestration for the delivery pipeline
(`push/delivery.py`), against `LogTransport` -- no new transport invented,
same mock the rest of the push suite tests against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marcellus import db
from marcellus.push import card_store
from marcellus.push.cards import Card
from marcellus.push.delivery import (
    CONTRACT_VERSION,
    build_card_key,
    build_card_payload,
    send_card_mutation,
    sweep_urgent_resound,
)
from marcellus.push.models import Device
from marcellus.push.transport import LogTransport


def make_device(token: str = "tok1") -> Device:
    return Device(apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
                  environment="sandbox")


def test_build_card_key_survives_redetection_scheme():
    kwargs = dict(camera="front", subject_kind="stranger", subject_id="track-1")
    assert build_card_key(**kwargs) == build_card_key(**kwargs) == "front:stranger:track-1"


def test_build_card_key_system_card_has_no_place():
    key = build_card_key(camera="front", subject_kind="", subject_id="offline", source="system")
    assert key == "front:system:offline"


def test_build_card_payload_contract_fields():
    card = Card(card_key="front:doors:stranger:1", level="notify", created_at=0.0, updated_at=5.0,
                state_since_at=5.0)
    payload = build_card_payload(
        card, "create", sound=True, subject_kind="stranger", place_class="doors",
        camera="front", zone_name="doors", glyph="person.identified",
        primary="Person at Front Door", secondary="Front Door · 0s", event_ts=5.0,
        media="https://sidecar.local/v1/push/thumbnail/h1", deep_link="elsinore://card/1",
    )
    assert payload["v"] == CONTRACT_VERSION == 1
    assert payload["card_key"] == "front:doors:stranger:1"
    assert payload["mutation"] == "create"
    assert payload["level"] == "notify"
    assert payload["subject_kind"] == "stranger"
    assert payload["place_class"] == "doors"
    assert payload["camera"] == "front"
    assert payload["zone_name"] == "doors"
    assert payload["glyph"] == "person.identified"
    assert payload["primary"] == "Person at Front Door"
    assert payload["secondary"] == "Front Door · 0s"
    assert payload["event_ts"] == 5.0
    assert payload["state_since_ts"] == 5.0
    assert payload["media"] == "https://sidecar.local/v1/push/thumbnail/h1"
    assert payload["deep_link"] == "elsinore://card/1?t=5.0"
    assert payload["deep_link"].split("?t=")[1] == str(payload["state_since_ts"])
    assert payload["aps"]["alert"] == {"title": "Person at Front Door", "body": "Front Door · 0s"}
    assert payload["aps"]["interruption-level"] == "active"
    assert payload["aps"]["sound"] == "general.caf"
    assert payload["aps"]["mutable-content"] == 1


def test_build_card_payload_deep_link_carries_state_since_ts_not_event_ts():
    # state_since_at deliberately differs from event_ts so the assertion
    # can't pass by accident if `t` were built off the wrong timestamp.
    card = Card(card_key="k", level="notify", created_at=0.0, updated_at=100.0, state_since_at=42.5)
    payload = build_card_payload(
        card, "escalate", sound=True, subject_kind="stranger", place_class="doors",
        camera="front", zone_name="doors", glyph="person.stranger",
        primary="Person at Front Door", secondary="Front Door · 57s", event_ts=100.0,
        deep_link="elsinore://card/k",
    )
    assert payload["state_since_ts"] == 42.5
    assert payload["deep_link"] == f"elsinore://card/k?t={payload['state_since_ts']}"


def test_build_card_payload_silent_omits_sound_key():
    card = Card(card_key="k", level="quiet", created_at=0.0, updated_at=0.0, state_since_at=0.0)
    payload = build_card_payload(
        card, "create", sound=False, subject_kind="thing", place_class="yard",
        camera="back", zone_name="yard", glyph="package.delivered",
        primary="Package delivered", secondary="Back Yard · 0s", event_ts=0.0,
    )
    assert "sound" not in payload["aps"]
    assert payload["aps"]["interruption-level"] == "passive"
    assert "media" not in payload
    assert "deep_link" not in payload


def test_build_card_payload_enrich_is_passive():
    card = Card(card_key="k", level="log", created_at=0.0, updated_at=0.0, state_since_at=0.0)
    payload = build_card_payload(
        card, "enrich", sound=False, subject_kind="animal", place_class="street",
        camera="front", zone_name="", glyph="animal.seen",
        primary="Animal seen", secondary="Street · 0s", event_ts=0.0,
    )
    assert payload["aps"]["interruption-level"] == "passive"
    assert "sound" not in payload["aps"]


@pytest.mark.asyncio
async def test_send_card_mutation_persists_and_sends(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(card_key="k1", level="notify", created_at=0.0, updated_at=0.0, state_since_at=0.0,
                sound_count=1)
    payload = build_card_payload(
        card, "create", sound=True, subject_kind="stranger", place_class="doors",
        camera="front", zone_name="doors", glyph="person.identified",
        primary="Person at Front Door", secondary="Front Door · 0s", event_ts=0.0,
    )
    sent = await send_card_mutation(conn, transport, [device], card, "create", payload)
    assert sent == 1
    assert len(transport.sent) == 1
    assert transport.sent[0]["collapse_id"] == "k1"
    assert card_store.get_card(conn, "k1") is not None


@pytest.mark.asyncio
async def test_send_card_mutation_with_no_payload_only_persists(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    card = Card(card_key="k2", level="log", created_at=0.0, updated_at=0.0, state_since_at=0.0)
    sent = await send_card_mutation(conn, transport, [make_device()], card, "enrich", None)
    assert sent == 0
    assert transport.sent == []
    assert card_store.get_card(conn, "k2") is not None


@pytest.mark.asyncio
async def test_sweep_urgent_resound_fires_once_and_persists(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    device = make_device()
    card = Card(card_key="k3", level="urgent", created_at=0.0, updated_at=0.0, state_since_at=0.0,
                sound_count=1, last_sound_at=0.0)
    card_store.upsert_card(conn, card)

    def payload_for(c: Card, context: dict) -> dict:
        return build_card_payload(
            c, "escalate", sound=True, subject_kind="stranger", place_class="off_limits",
            camera="pool", zone_name="off_limits", glyph="person.stranger",
            primary="Stranger at pool", secondary="Pool · 2m0s", event_ts=120.0,
        )

    resounded = await sweep_urgent_resound(
        conn, transport, [device], now=120.0, interval_s=120.0, enabled=True,
        payload_for_resound=payload_for,
    )
    assert resounded == 1
    assert len(transport.sent) == 1

    fetched = card_store.get_card(conn, "k3")
    assert fetched.resound_count == 1
    assert fetched.last_sound_at == 120.0

    # A second sweep at the same instant does not re-fire -- resound_count > 0.
    resounded_again = await sweep_urgent_resound(
        conn, transport, [device], now=120.0, interval_s=120.0, enabled=True,
        payload_for_resound=payload_for,
    )
    assert resounded_again == 0
    assert len(transport.sent) == 1


@pytest.mark.asyncio
async def test_sweep_urgent_resound_disabled_by_config(sidecar_db_path: Path):
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    card = Card(card_key="k4", level="urgent", created_at=0.0, updated_at=0.0, state_since_at=0.0,
                sound_count=1, last_sound_at=0.0)
    card_store.upsert_card(conn, card)
    resounded = await sweep_urgent_resound(
        conn, transport, [make_device()], now=999.0, interval_s=120.0, enabled=False,
        payload_for_resound=lambda c, ctx: {},
    )
    assert resounded == 0
    assert transport.sent == []
