"""No person's name, handle or face ID may leave the sidecar in a device payload.

Runs the real delivery paths with a fake transport and asserts that a distinctive
name never appears anywhere in the serialized payloads handed to the transport
(card pushes, Live Activity start/update/end, situation pushes). A recognized
person's text is exactly what an unrecognized person's would be.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from marcellus import db
from marcellus.config import PushSection
from marcellus.faces.handles import handle_for
from marcellus.push import activity, policy_settings, store
from marcellus.push import payload as situation_payload
from marcellus.push.delivery_wire import (
    handle_delivery_event,
    handle_delivery_resolve,
    handle_recognition_event,
)
from marcellus.push.models import Device, ReviewEvent
from marcellus.push.situations import STAGE_ARRIVING, STAGE_ENDING, Match, Situation
from marcellus.push.transport import LogTransport

NAME = "Zzyzxname"


def _device(token: str, *, push_to_start: str) -> Device:
    return Device(
        apns_token=token, device_id=f"d_{token}", bundle_id="com.pondhouse.Elsinore",
        environment="sandbox", push_to_start_token=push_to_start,
        min_severity="detection", frequent_pushes_enabled=True,
    )


def _event(zones: tuple[str, ...], sub_labels: tuple[str, ...]) -> ReviewEvent:
    return ReviewEvent(
        review_id="r_doorbell_trk1", camera="doorbell", severity="alert",
        labels=("person",), track_ids=("trk1",), zones=zones, sub_labels=sub_labels,
    )


def _forbidden() -> list[str]:
    # The name and the handle of any id a cluster could have, as whole phrases.
    # (Push code never sees face ids, so an id could only appear by being
    # plumbed in; handle_for is the id's only stable text form.)
    return [NAME, *(handle_for(i) for i in range(0, 2000))]


def _assert_clean(transport: LogTransport) -> None:
    lowered = json.dumps(transport.sent, default=str).lower()
    for needle in _forbidden():
        assert needle.lower() not in lowered, f"{needle!r} leaked into a device payload"


@pytest.mark.asyncio
async def test_named_person_never_reaches_device_payloads(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    transport = LogTransport()
    la_device = _device("tokLA", push_to_start="pts1")
    card_device = _device("tokCard", push_to_start="")
    devices = [la_device, card_device]
    config = PushSection(delivery_enabled=True)

    settings = policy_settings.default_settings()
    settings["mute_sounds"] = False
    settings["recognition"] = {"known_person": "relax_one", "known_vehicle": "relax_one"}
    settings["ladder"]["person"]["doors"] = "quiet"
    settings["ladder"]["person"]["off_limits"] = "urgent"
    policy_settings.apply_settings(policy_settings.normalize_settings(settings))

    async def deliver(event: ReviewEvent, now: float) -> None:
        await handle_delivery_event(
            event, conn=conn, devices=devices, transport=transport, config=config, now=now,
        )

    # create (front door, quiet): Live Activity start for one device, a card for the other
    await deliver(_event(("front_door",), (NAME,)), 0.0)
    row = conn.execute(
        "SELECT activity_id FROM push_activities WHERE apns_token = ?", (la_device.apns_token,)
    ).fetchone()
    assert row is not None
    store.attach_activity_token(
        conn, activity_id=row["activity_id"], apns_token=la_device.apns_token,
        situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID, token="perAct1",
    )
    # update (enrich), then escalate (pool, off limits -> urgent)
    await deliver(_event(("front_door",), (NAME,)), 10.0)
    await deliver(_event(("pool",), (NAME,)), 20.0)
    # recognition relax (de-escalate), then another update, then resolve
    await handle_recognition_event(
        "doorbell", "trk1", NAME, conn=conn, devices=devices, transport=transport,
        config=config, label="person", now=30.0,
    )
    await deliver(_event(("pool",), (NAME,)), 40.0)
    await handle_delivery_resolve(
        "doorbell", "trk1", conn=conn, devices=devices, transport=transport, config=config,
        subject_kind="person", now=50.0,
    )

    mutations: set[str] = set()
    la_events: set[str] = set()
    for sent in transport.sent:
        payload = sent["payload"]
        if sent.get("live_activity"):
            la_events.add(sent["event"])
            mutations.add(payload["aps"]["content-state"].get("mutation", ""))
        else:
            mutations.add(payload.get("mutation", ""))
    assert {"create", "escalate", "deescalate", "resolve"} <= mutations, mutations
    assert {"start", "update", "end"} <= la_events, la_events
    assert any(
        "Person" in json.dumps(s["payload"]) for s in transport.sent
    ), "expected generic Person copy"
    _assert_clean(transport)


def test_situation_pushes_carry_no_identity() -> None:
    situation = Situation(
        id="front_door", name="Front door visitors", sub_label_allow=(NAME,),
        sub_label_deny=(NAME,),
    )
    transport = LogTransport()
    for stage in (STAGE_ARRIVING, "present", STAGE_ENDING):
        match = Match(situation=situation, track_id="trk1", dwell_s=42.0, label="person", zone="")
        transport.sent.append({"payload": situation_payload.build_payload(
            match, handle="h_abc", server_id="s_1", now=0.0,
        )})
        transport.sent.append({"payload": activity.content_state(match, stage=stage)})
    transport.sent.append({"payload": activity.build_end(
        Match(situation=situation, track_id="trk1", dwell_s=1.0, label="person", zone=""),
        now=0.0,
    )})
    _assert_clean(transport)
