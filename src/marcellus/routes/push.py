"""`/v1/push` -- device registration/unregistration and handle redemption.

Auth is the shared Frigate session (`marcellus.auth`), same as every
other sidecar-owned route -- there is no second credential for push (spec
§1: "the sidecar just attaches the device token to that session's
identity"). The one exception is `GET /v1/push/thumbnail/{handle}`
(`auth.EXEMPT_PREFIXES`): the iOS Notification Service Extension fetches it
and holds no Frigate session, so it's protected by the handle itself being
opaque, unguessable, and short-lived instead.
"""

from __future__ import annotations

import datetime as _datetime
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from marcellus import db
from marcellus.push import card_store, decision_trace, library, policy_settings, store
from marcellus.push import receipts as receipts_store
from marcellus.push.situations import Situation
from marcellus.push.transport import RELAY_HEALTH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/push", tags=["push"])

_ERR_HANDLE_NOT_FOUND = "handle_not_found"
_ERR_DEVICE_NOT_FOUND = "device_not_found"
_ERR_PUSH_DISABLED = "push_disabled"
_ERR_TEST_SEND_FAILED = "test_send_failed"
_ERR_SITUATION_NOT_FOUND = "situation_not_found"
_ERR_THUMBNAIL_NOT_FOUND = "thumbnail_not_found"
_ERR_BAD_SCOPE = "bad_scope"
_ERR_CARD_NOT_FOUND = "card_not_found"
_ERR_BAD_FEEDBACK = "invalid_feedback"
_ERR_RATE_LIMITED = "rate_limited"

#: Round-trip test push rate limit (alerts-slice2 §D): one per token per 10s.
_TEST_RATE_LIMIT_S = 10.0
#: token -> last test-push epoch. In-memory (like the sound-rate keys in
#: `delivery.py`'s `_SOUND_RATE_KEY`, but this one doesn't need to survive a
#: restart -- a fresh process just re-allows the very next tap).
_last_test_push_at: dict[str, float] = {}


def reset_test_rate_limit_for_tests() -> None:
    _last_test_push_at.clear()


class DeviceLocation(BaseModel):
    lat: float
    lon: float


class DeviceRegistration(BaseModel):
    """The v2 registration record (notification-experience plan §8).

    Every v1 field keeps its meaning and its default, so a phone running an
    older app build PUTs exactly what it PUT before and is stored exactly as
    it was stored before. Everything added below is optional; an omitted
    `situations` is what keeps that device on the v1 firing path.

    Unknown fields are ignored rather than rejected -- the app ships on its
    own cadence and a newer build must not 422 against an older sidecar. They
    are *logged* though (names only, never values): silently dropping a field
    the app believes it sent is how `push_to_start_token` spent a day looking
    like an app-side bug when the sidecar simply hadn't learned the name yet.
    """

    model_config = ConfigDict(extra="allow")

    bundle_id: str
    # Not optional, not inferred (spec §1) -- sandbox and production APNs are
    # different endpoints with different trust; the app must read its own
    # `aps-environment` entitlement and say so.
    environment: Literal["sandbox", "prod"]
    app_version: str = ""
    cameras: list[str] = Field(default_factory=list)  # [] = all cameras
    labels: list[str] = Field(default_factory=list)  # [] = all labels
    min_severity: Literal["alert", "detection"] = "alert"

    # -- v2 --
    schema_version: int = 1
    timezone: str = ""  # IANA name, e.g. "America/Los_Angeles"
    location: DeviceLocation | None = None
    situations: list[dict[str, Any]] = Field(default_factory=list)
    # None means "the client didn't mention snoozes", which must leave the
    # ones it set earlier alone -- see `store.replace_snoozes`. An explicit
    # [] is a request to clear them.
    snoozes: list[dict[str, Any]] | None = None

    # -- Phase 2 --
    # One per app install, rotates on reinstall; creates Live Activities.
    # Absent means this device isn't ready for them, and its Present-tier
    # situations fall back to alert pushes.
    push_to_start_token: str = ""

    # Accepted, persisted, and deliberately unread (Phase 4's digest and LLM).
    la_capable: bool = True
    # Phase A: opt this device into the fast (3s) Live Activity update
    # cadence instead of the default slow (15s) one. Absent/false keeps the
    # default -- most devices don't need tighter pacing and it costs more
    # APNs traffic.
    frequent_pushes_enabled: bool = False
    live_activity_token: str = ""
    morning_digest: dict[str, Any] | None = None
    llm: dict[str, Any] | None = None


class ReceiptEntry(BaseModel):
    """One entry of `POST /v1/push/receipts`' batch body (alerts-slice2 §A)."""

    model_config = ConfigDict(extra="ignore")

    apns_token: str
    card_key: str
    mutation: str
    state_since_ts: float
    received_ts: float
    media_attached: bool = False
    source: Literal["nse", "app_flush", "app_foreground"] = "app_flush"


class ReceiptsBatch(BaseModel):
    receipts: list[ReceiptEntry] = Field(default_factory=list)


class SnoozeRequest(BaseModel):
    """`POST /v1/push/snooze` (plan §8).

    `apns_token` identifies the device, because nothing else can: the sidecar's
    auth is the shared Frigate session, which is per-*user*, and snoozes are
    per-*device* by design (plan §6 -- snoozing on the iPad must not quiet the
    iPhone).
    """

    apns_token: str
    scope: str  # "situation:<id>" | "camera:<name>" | "global"
    until_epoch: float


class SituationTestRequest(BaseModel):
    apns_token: str


class ActivityTokenUpload(BaseModel):
    """`POST /v1/push/activity/token` (Phase 2 plan, "Push tokens").

    Carries both identities on purpose: `activity_id` is what the app knows
    and what the delete endpoint addresses, while
    `(apns_token, situation_id, track_id)` is what the MQTT stream can look an
    activity up by when it has an update to send.
    """

    apns_token: str
    situation_id: str
    track_id: str
    activity_id: str
    token: str


def _validate_scope(scope: str) -> str:
    scope = scope.strip()
    if scope == "global":
        return scope
    # A prefix with nothing after it ("situation:") would silence nothing
    # while looking exactly like a snooze that took.
    if scope.startswith(("situation:", "camera:")) and scope.split(":", 1)[1]:
        return scope
    raise HTTPException(
        status_code=422,
        detail={
            "error": _ERR_BAD_SCOPE,
            "message": "scope must be 'global', 'situation:<id>', or 'camera:<name>'",
        },
    )


@router.put("/devices/{apns_token}")
async def register_device(
    apns_token: Annotated[str, Path(min_length=1)],
    body: DeviceRegistration,
    request: Request,
) -> dict[str, Any]:
    """Idempotent PUT on the token (spec §1) -- re-registering (relaunch,
    entitlement refresh) overwrites this device's own filter state rather
    than accumulating duplicate rows that would double-fire alerts.

    The response echoes how the sidecar will actually evaluate this device:
    `schema_version: 1` (today's camera+label+severity firing) or `2`
    (situation-only), plus how many of the submitted situations parsed. A
    situation the sidecar silently discarded -- no `id`, say -- would
    otherwise look enabled in the app and never fire.
    """
    settings = request.app.state.settings
    extras = sorted(body.model_extra or ())
    if extras:
        logger.info(
            "push: registration for %s carried field(s) this sidecar does not "
            "know: %s -- accepted and dropped",
            store.device_id_for_token(apns_token),
            ", ".join(extras),
        )
    situations = [s for s in body.situations if isinstance(s, dict)]
    parsed = [s for s in (Situation.from_dict(s) for s in situations) if s is not None]
    schema_version = 2 if parsed else body.schema_version

    def _persist(conn: Any) -> tuple[Any, Any, Any]:
        previous = store.get_device(conn, apns_token)
        device_id = store.upsert_device(
            conn,
            apns_token=apns_token,
            bundle_id=body.bundle_id,
            environment=body.environment,
            app_version=body.app_version,
            cameras=body.cameras,
            labels=body.labels,
            min_severity=body.min_severity,
            schema_version=schema_version,
            timezone_name=body.timezone,
            location=body.location.model_dump() if body.location else None,
            situations=situations,
            live_activity_token=body.live_activity_token,
            morning_digest=body.morning_digest,
            llm=body.llm,
            push_to_start_token=body.push_to_start_token,
            la_capable=body.la_capable,
            frequent_pushes_enabled=body.frequent_pushes_enabled,
        )
        if body.snoozes is not None:
            store.replace_snoozes(conn, apns_token=apns_token, snoozes=body.snoozes)
        conn.commit()
        # Read back rather than echoing the request: a PUT that omits
        # `push_to_start_token` keeps the one already stored, so the body alone
        # can't answer "can this device run Live Activities".
        return previous, device_id, store.get_device(conn, apns_token)

    previous, device_id, stored = await db.with_sidecar(settings.sidecar.db_path, _persist)

    # Recorded for back-compat visibility only: the situations pipeline is
    # retired (Phase 5 §1, card pipeline is the sole alert path), but which
    # mode a device *registered* under still matters for tracing older app
    # builds, so the row keeps its situations and this line keeps logging.
    uses_situations = bool(parsed)
    logger.info(
        "push: registration apns_token=%s schema_version=%s uses_situations=%s "
        "(situations stored for back-compat; card pipeline is the alert path)",
        apns_token[:8],
        schema_version,
        uses_situations,
    )
    if previous is not None and previous.uses_situations != uses_situations:
        # The edge that actually matters: a device silently flipping mode
        # (e.g. an app reinstall wiping stored situations) looks identical to
        # a healthy re-registration from the outside. This is the line that
        # would have caught it in seconds instead of a four-hour trace.
        logger.info(
            "push: registration apns_token=%s transitioned uses_situations %s -> %s",
            apns_token[:8],
            previous.uses_situations,
            uses_situations,
        )

    # Echo back what the sidecar will actually do with this device, including
    # whether Live Activities are available to it -- the app-side token flow is
    # asynchronous, so "did my push-to-start token land" is a real question
    # with no other way to answer it.
    return {
        "registered": True,
        "device_id": device_id,
        "schema_version": schema_version,
        "situations_accepted": len(parsed),
        "la_capable": bool(stored.la_capable) if stored else True,
        "live_activities": bool(stored and stored.can_live_activity),
    }


@router.delete("/devices/{apns_token}")
async def unregister_device(
    apns_token: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """Explicit unregistration (notification-toggle-off, or best-effort after
    registration failure). Not the primary cleanup path -- that's the 410/400
    feedback loop in `push.engine` (spec §5) -- because the app can't promise
    to run this before uninstall. Idempotent: unregistering an unknown token
    is still a 200, not a 404, since the end state either way is "not
    registered"."""
    settings = request.app.state.settings

    def _delete(conn: Any) -> None:
        store.delete_device(conn, apns_token)
        conn.commit()

    await db.with_sidecar(settings.sidecar.db_path, _delete)
    return {"unregistered": True}


@router.post("/devices/{apns_token}/test")
async def test_push(
    apns_token: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """Round-trip test push (alerts-slice2 §D, changed from spec §1's plain
    ping). Sends a real **card** payload -- `mutation: "test"`, a synthetic
    `card_key`, `v:1`/`mutable-content:1` like any other card -- through the
    exact same relay call real cards use (`transport.send_situation`, i.e.
    `RelayTransport`'s `/v1/relay/situation`), *not* `send_test`'s
    `/v1/relay/test`: that endpoint carries no `handle` and no
    `mutable-content` (see `PushTransport.send_test`'s docstring), so the NSE
    never runs and there is nothing for it to report a receipt about. Routing
    through the card path is what makes this endpoint's own name ("round-trip
    test") true -- the NSE processes it and posts a receipt exactly like a
    real alert.

    Recorded in `push_card_sends` (so receipts can pair against it and the
    device-detail stats see it); explicitly skips `push_decisions` -- it
    isn't a routing decision.

    `{"sent": true}` means the relay *accepted* the request -- there is no
    delivery receipt in this response, so it can never mean "displayed on the
    device". 404 is reserved for "token not registered". 429 when called
    again for the same token within 10s. A rejected send is 502 and a server
    with push switched off is 503, both carrying the standard error envelope.
    """
    settings = request.app.state.settings
    device = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_device(conn, apns_token)
    )
    if device is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
        )

    now = _datetime.datetime.now(_datetime.timezone.utc).timestamp()
    last = _last_test_push_at.get(apns_token)
    if last is not None and now - last < _TEST_RATE_LIMIT_S:
        raise HTTPException(
            status_code=429,
            detail={
                "error": _ERR_RATE_LIMITED,
                "message": f"one test push per device per {_TEST_RATE_LIMIT_S:.0f}s",
            },
        )

    # Registration writes to the DB whether or not push is enabled, so a
    # registered token can exist on a server with no engine running. Say so
    # rather than reporting a send that no transport ever attempted.
    engine = getattr(request.app.state, "push_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _ERR_PUSH_DISABLED,
                "message": "push is not enabled on this server (push.enabled=false)",
            },
        )

    from marcellus.push.cards import Card
    from marcellus.push.delivery import build_card_payload

    card_key = f"test:{apns_token[:8]}:{int(now)}"
    card = Card(
        card_key=card_key, level="notify", peak_level="notify",
        created_at=now, updated_at=now, state_since_at=now,
    )
    payload = build_card_payload(
        card, "test", sound=True, subject_kind="", place_class="", label="",
        camera="elsinore", zone_name="", glyph="checkmark.seal",
        primary="Test alert", secondary="Round trip from your server",
        event_ts=now, deep_link="elsinore://doctor",
    )

    _last_test_push_at[apns_token] = now
    result = await engine.transport.send_situation(
        device, payload=payload, collapse_id=card_key, apns_priority=10,
    )

    def _record(conn: Any) -> None:
        store.record_card_send(
            conn, apns_token=apns_token, card_key=card_key, mutation="test",
            sent_at=now, ok=result.ok, error=result.error,
        )
        conn.commit()

    await db.with_sidecar(settings.sidecar.db_path, _record)

    if not result.ok:
        # Includes the 410/400 case, where a real send would prune the
        # device row (spec §5) -- `send_situation` doesn't do that pruning
        # itself (only the engine's own send path does), so the row is left
        # for the next real send to discover the same failure.
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_TEST_SEND_FAILED,
                "message": result.error or "push transport rejected the send",
            },
        )
    return {"sent": True, "card_key": card_key, "sent_ts": now}


@router.get("/situations/library")
async def situations_library() -> list[dict[str, Any]]:
    """The starter situations a new user can enable with one tap (plan §1).

    Cameras and zones are placeholders using the plan's own example names --
    the app's editor replaces them with real ones read from the user's Frigate
    `/api/config` before registering. A starter left unedited matches only if
    the user happens to have a zone by that name, which fails silent rather
    than firehose.
    """
    return library.starter_library()


@router.get("/sounds")
async def sounds(app_version: str = Query("")) -> list[dict[str, str]]:
    """The sound ids a situation's `sound` field may name (plan §3).

    The `.caf` assets ship in the *app* bundle, not here, so the catalog is
    keyed on `app_version`: advertising a sound an older build doesn't contain
    would deliver a silent notification, which reads as broken at exactly the
    wrong moment. Phase 1 ships one catalog for every version.
    """
    return library.sound_catalog(app_version)


@router.post("/snooze")
async def create_snooze(body: SnoozeRequest, request: Request) -> dict[str, Any]:
    """Silence one scope for one device until `until_epoch` (plan §6).

    Sidecar-side because it has to survive an app kill, and because the
    interactive widget's "Snooze all 15m" must reach the source of truth
    without the app running at all. Expiry is a timestamp, not a scheduled
    job: it re-enables itself with nothing to run and nothing to miss if the
    sidecar was restarted in the meantime.

    Deprecated (sidecar-snooze-and-v2-investigation handoff, Thread B):
    superseded by `registration.snoozes`, a full-state replace on every
    `PUT /v1/push/devices/{token}` -- point-updates writing the same store a
    whole-state sync writes let local and sidecar snooze state drift apart
    invisibly. Kept for one release so an app build that still calls this
    keeps working; slated for removal once that build has aged out.
    """
    logger.warning(
        "push: deprecated POST /v1/push/snooze called for device %s -- "
        "use registration.snoozes instead",
        body.apns_token,
    )
    scope = _validate_scope(body.scope)
    settings = request.app.state.settings

    def _snooze(conn: Any) -> Any:
        if store.get_device(conn, body.apns_token) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
            )
        store.set_snooze(
            conn, apns_token=body.apns_token, scope=scope, until_epoch=body.until_epoch
        )
        conn.commit()
        return store.list_snoozes(conn, body.apns_token)

    active = await db.with_sidecar(settings.sidecar.db_path, _snooze)
    return {"snoozed": True, "scope": scope, "until_epoch": body.until_epoch, "active": active}


@router.delete("/snooze/{scope:path}")
async def delete_snooze(
    scope: str, request: Request, apns_token: str = Query(..., min_length=1)
) -> dict[str, Any]:
    """Lift a snooze early. Idempotent: clearing one that already expired (or
    never existed) is still a 200, since the end state either way is "not
    snoozed".

    `{scope:path}` because a scope contains a colon (`situation:at-the-door`)
    and, for `camera:<name>`, whatever the user named their camera.

    Deprecated (sidecar-snooze-and-v2-investigation handoff, Thread B): same
    reasoning as `POST /v1/push/snooze` -- lifting a snooze is just
    re-registering with a shorter (or absent) `snoozes` array now.
    """
    logger.warning(
        "push: deprecated DELETE /v1/push/snooze/%s called for device %s -- "
        "use registration.snoozes instead",
        scope,
        apns_token,
    )
    settings = request.app.state.settings

    def _unsnooze(conn: Any) -> Any:
        store.clear_snooze(conn, apns_token=apns_token, scope=scope)
        conn.commit()
        return store.list_snoozes(conn, apns_token)

    active = await db.with_sidecar(settings.sidecar.db_path, _unsnooze)
    return {"unsnoozed": True, "scope": scope, "active": active}


@router.post("/test/{situation_id}")
async def test_situation_push(
    situation_id: Annotated[str, Path(min_length=1)],
    body: SituationTestRequest,
    request: Request,
) -> dict[str, Any]:
    """Fire one push for `situation_id` at the calling device (plan §8).

    The device is named in the body rather than inferred from the session:
    the sidecar's auth is the shared Frigate session, which identifies a
    *user*, and this endpoint is per-*device* -- there is nothing on the
    request that could tell two of a user's phones apart.

    Runs the whole real path (handle, thumbnail pre-warm, payload, collapse
    id, sound), because what the app's Settings button is verifying is that a
    real situation push arrives looking the way it should. Snooze and the
    rate-limit ceiling are bypassed and the send isn't charged against the
    hourly budget -- the user asked for this one.
    """
    settings = request.app.state.settings
    device = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_device(conn, body.apns_token)
    )
    if device is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
        )

    situation = next((s for s in device.situations if s.id == situation_id), None)
    if situation is None:
        # Falling back to the starter library would test a rule the device
        # isn't actually registered with, which is the one thing this button
        # must not quietly do.
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_SITUATION_NOT_FOUND,
                "message": f"device has no situation {situation_id!r}",
            },
        )

    engine = getattr(request.app.state, "push_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _ERR_PUSH_DISABLED,
                "message": "push is not enabled on this server (push.enabled=false)",
            },
        )

    result = await engine.send_situation_test(device, situation)
    if not result.ok:
        raise HTTPException(
            status_code=502,
            detail={
                "error": _ERR_TEST_SEND_FAILED,
                "message": result.error or "push transport rejected the send",
            },
        )
    return {"sent": True, "situation_id": situation_id}


@router.post("/activity/token")
async def upload_activity_token(body: ActivityTokenUpload, request: Request) -> dict[str, Any]:
    """The app hands over a Live Activity's own push token (Phase 2).

    iOS mints this token *after* creating the activity from the start push, so
    there is always a window where an activity is on screen that the sidecar
    cannot yet update. That is normal: updates resume on the next observation
    once this lands.

    Keyed on `activity_id` (what the app knows) and looked up on
    `(apns_token, situation_id, track_id)` (what the MQTT stream knows) --
    which is why the body carries both.
    """
    settings = request.app.state.settings

    def _attach(conn: Any) -> Any:
        device = store.get_device(conn, body.apns_token)
        if device is None:
            raise HTTPException(
                status_code=404,
                detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
            )
        store.attach_activity_token(
            conn,
            activity_id=body.activity_id,
            apns_token=body.apns_token,
            situation_id=body.situation_id,
            track_id=body.track_id,
            token=body.token,
        )
        conn.commit()
        return device

    device = await db.with_sidecar(settings.sidecar.db_path, _attach)

    # Fast create→resolve race: the card may already be closed by the time
    # this token arrives. End the activity now rather than leaving it
    # stranded on the lock screen until its stale-date. This part can't ride
    # in `_attach`'s worker thread: `end_activity_if_card_closed` awaits the
    # transport while holding the connection (sqlite connections are
    # thread-bound), so this rare path keeps a loop-thread connection.
    engine = getattr(request.app.state, "push_engine", None)
    if engine is not None:
        from marcellus.push.delivery_wire import end_activity_if_card_closed

        # Same `_pipeline_lock` the per-frame handlers hold across their own
        # `await transport.*` -- without it this rare race path can convoy
        # into `database is locked` against a concurrent handler.
        async with engine._pipeline_lock:
            conn = db.open_sidecar(settings.sidecar.db_path)
            try:
                await end_activity_if_card_closed(
                    conn,
                    device,
                    engine.transport,
                    token=body.token,
                )
                conn.commit()
            finally:
                conn.close()
    return {"accepted": True, "activity_id": body.activity_id}


@router.delete("/activity/token/{activity_id}")
async def delete_activity_token(
    activity_id: Annotated[str, Path(min_length=1)],
    request: Request,
    dismissed: bool = False,
) -> dict[str, Any]:
    """The app ended the activity locally (user swiped it away, or its own
    lifecycle finished).

    `dismissed=false` (default): drops the row outright -- there is nothing
    left to send an end push to, and leaving a tokened row behind would have
    the sweeper try. Idempotent -- an unknown id is still a 200, since the end
    state either way is "the sidecar isn't tracking that activity".

    `dismissed=true`: the user explicitly swiped the activity away, so
    instead of a hard delete the row is closed as a dismissal tombstone
    (Phase A §3) -- a future CREATE/UPDATE for the same (device, situation,
    track) is suppressed until an ESCALATE breaks through it.
    """
    settings = request.app.state.settings

    def _delete(conn: Any) -> Any:
        if dismissed:
            removed = store.dismiss_activity(conn, activity_id)
        else:
            removed = store.delete_activity(conn, activity_id)
        conn.commit()
        return removed

    removed = await db.with_sidecar(settings.sidecar.db_path, _delete)
    return {"deleted": True, "activity_id": activity_id, "was_tracked": removed}


@router.get("/thumbnail/{handle}")
async def get_thumbnail(handle: str, request: Request) -> Response:
    """The NSE's pre-warmed snapshot fetch (plan §8).

    Same auth as every other `/v1/` endpoint -- the extension reads the app's
    session credential from the shared Keychain access group, so there is no
    second credential and no second login flow (transport spec §3).

    A miss is a 404 and the app delivers the alert without an image: the
    visible push is the promise, the image is not.
    """
    settings = request.app.state.settings
    jpeg = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.get_thumbnail(conn, handle)
    )
    if jpeg is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_THUMBNAIL_NOT_FOUND,
                "message": "no thumbnail for that handle (expired, unknown, or never warmed)",
            },
        )
    return Response(
        content=jpeg,
        media_type="image/jpeg",
        # Immutable for the handle's life: a handle is minted per push and
        # never reused, so the bytes behind one can't change.
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/handle/{handle}")
async def redeem_handle(handle: str, request: Request) -> dict[str, Any]:
    """NSE / app handle redemption (spec §3 step 2). Returns the camera and
    Frigate event id a handle stands for, plus the `snapshot_url` to fetch
    next -- never the raw event id in the APNs payload itself."""
    settings = request.app.state.settings
    data = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: store.redeem_handle(conn, handle)
    )
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_HANDLE_NOT_FOUND, "message": "handle not found or expired"},
        )
    return {
        "camera": data["camera"],
        "event_id": data["event_id"],
        "snapshot_url": f"/api/events/{data['event_id']}/snapshot.jpg",
    }


@router.get("/card-for-event/{event_id}")
async def card_for_event(
    event_id: Annotated[str, Path(min_length=1)], request: Request
) -> dict[str, Any]:
    """The persisted push-card outcome for a Frigate event id (the event
    detail screen's "why did this alert me (or not)").

    `push_cards.card_key` has no dedicated event-id column -- it's built as
    `{camera}:{subject_kind}:{track_id}` and Frigate's tracked-object id IS
    the event id (`push/delivery.py`'s `build_card_key`). Resolution order:
    1. A `push_cards` row whose `card_key` ends with `:{event_id}` directly.
    2. `push_card_track_aliases` keyed by `track_id = event_id`, for a track
       that got folded into a card under a different track's identity
       (cross-camera dedup / label-flip merges).
    """
    settings = request.app.state.settings

    def _lookup(conn: Any) -> tuple[Any, str, list[str]] | None:
        row = card_store.find_card_row_by_event_suffix(conn, event_id)
        matched_via = "card_key"
        if row is None:
            alias_key = card_store.find_track_alias_card_key(conn, event_id)
            if alias_key is not None:
                row = card_store.get_card_row(conn, alias_key)
                matched_via = "alias"
        if row is None:
            return None
        # Best-effort only (docstring on `decision_trace.reasons_for`): the
        # durable log may not have a row for anything but a very recent
        # event, so the client must treat this as optional.
        reasons = decision_trace.reasons_for(conn, event_id)
        return row, matched_via, reasons

    found = await db.with_sidecar(settings.sidecar.db_path, _lookup)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": _ERR_CARD_NOT_FOUND,
                "message": "no push card found for that event id",
            },
        )
    row, matched_via, reasons = found
    zones_csv = row["zones_csv"] or ""
    zones = [z for z in zones_csv.split(",") if z]

    return {
        "card_key": row["card_key"],
        "camera": row["camera"],
        "subject_kind": row["subject_kind"],
        "place_class": row["place_class"],
        "label": row["label"],
        "family": row["family"],
        "level": row["level"],
        "peak_level": row["peak_level"],
        "zone_override_hit": bool(row["zone_override_hit"]),
        "zone_name": row["zone_name"],
        "zones": zones,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "state_since_at": row["state_since_at"],
        "handled": bool(row["handled"]),
        "resolved": bool(row["resolved"]),
        "closed": bool(row["closed"]),
        "matched_via": matched_via,
        "reasons": reasons,
    }


@router.post("/receipts")
async def post_receipts(body: ReceiptsBatch, request: Request) -> dict[str, Any]:
    """NSE/app delivery receipts (alerts-slice2 §A). Unknown tokens are still
    stored (no 404 -- the sidecar doesn't validate against `push_devices`
    here, since a receipt naming a token this sidecar never registered is
    still useful telemetry, not a client error). Duplicates
    (`apns_token`+`card_key`+`mutation`+`state_since_ts`) are ignored, not
    errors -- `INSERT OR IGNORE` against the unique index."""
    settings = request.app.state.settings
    payload = [r.model_dump() for r in body.receipts]
    result = await db.with_sidecar(
        settings.sidecar.db_path, lambda conn: receipts_store.record(conn, payload)
    )
    return result


@router.get("/devices/{apns_token}")
async def get_device_detail(
    apns_token: Annotated[str, Path(min_length=1)],
    request: Request,
    window_days: float = Query(default=7.0, gt=0, le=90),
) -> dict[str, Any]:
    """Device detail (alerts-slice2 §B): registration facts plus send/receipt
    stats over `window_days` (default 7). 404 when the token isn't
    registered -- same convention as every other `/devices/{token}` route."""
    settings = request.app.state.settings

    def _query(conn: Any) -> tuple[Any, dict[str, Any]] | None:
        device_row = store.get_device_row(conn, apns_token)
        if device_row is None:
            return None
        stats = store.device_stats(conn, apns_token, window_days=window_days)
        return device_row, stats

    found = await db.with_sidecar(settings.sidecar.db_path, _query)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"error": _ERR_DEVICE_NOT_FOUND, "message": "token not registered"},
        )
    device_row, stats = found
    # Device-scoped relay signal (alerts-slice2 §C follow-up): derived from
    # *this* device's own `push_card_sends` rows, not the process-global
    # `RELAY_HEALTH` singleton -- that singleton is shared across every
    # device, so a healthy device's doctor could otherwise show a failure
    # that actually belonged to some other device's send. `last_status_code`
    # isn't tracked per-send, so it stays null here (unlike the global
    # `/status` "relay" block).
    device_relay = {
        "last_ok_at": stats.pop("last_send_ok_at", None),
        "last_error": stats["last_send_error"],
        "last_error_at": stats["last_send_error_at"],
        "last_status_code": None,
    }
    return {
        "registered": True,
        "environment": device_row["environment"],
        "app_version": device_row["app_version"],
        "registered_at": device_row["registered_at"],
        "updated_at": device_row["updated_at"],
        "la_capable": bool(device_row["la_capable"]),
        "relay": device_relay,
        **stats,
    }


@router.get("/decisions")
async def get_decisions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
    card_key: str | None = Query(default=None),
) -> dict[str, Any]:
    """Recent routing decisions, newest first (alerts-slice1 §A). Durable
    (SQLite, 30-day retention) -- not lost on restart."""
    settings = request.app.state.settings
    decisions = await db.with_sidecar(
        settings.sidecar.db_path,
        lambda conn: decision_trace.recent(conn, limit, before=before, card_key=card_key),
    )
    return {"decisions": decisions}


@router.get("/status")
async def get_push_status(request: Request) -> dict[str, Any]:
    """One-glance push health (alerts-slice1 §B): MQTT/Frigate liveness plus
    the newest decision/send from the durable log. Cheap -- a couple of
    `push_decisions` queries and in-memory flags, no Frigate HTTP round-trip.
    """
    settings = request.app.state.settings
    subscriber = getattr(request.app.state, "push_subscriber", None)

    def _last_review_iso() -> str | None:
        if subscriber is None or subscriber.last_review_at is None:
            return None
        return _datetime.datetime.fromtimestamp(
            subscriber.last_review_at, tz=_datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _query(conn: Any) -> tuple[dict[str, Any], int]:
        return decision_trace.status(conn), len(store.list_devices(conn))

    trace_status, devices = await db.with_sidecar(settings.sidecar.db_path, _query)

    policy = policy_settings.get_active()
    local_now = _datetime.datetime.now()
    now_minutes = local_now.hour * 60 + local_now.minute
    quiet_hours_active, _qh_mode = policy_settings.is_quiet_hours(policy, now_minutes)

    return {
        "now": _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "frigate_available": bool(subscriber.frigate_online) if subscriber else False,
        "mqtt_connected": bool(subscriber.connected) if subscriber else False,
        "last_review_at": _last_review_iso(),
        **trace_status,
        "quiet_hours_active": quiet_hours_active,
        # No global timed-mute concept exists (spec is scoped to per-cell/
        # per-zone silence, `push_silences`) -- resolved as always null.
        "paused_until": None,
        "devices": devices,
        # Alerts-slice2 §C: in-memory, process-lifetime relay health, updated
        # by `push/transport.py`'s `RelayTransport` on every relay response.
        "relay": RELAY_HEALTH.as_dict(),
    }


@router.post("/feedback")
async def post_feedback(request: Request) -> dict[str, Any]:
    """Log user feedback on a push notification (tuning trace, no routing
    changes this phase). Accepts any verdict string for forward compat."""
    body = await request.json()
    if not isinstance(body, dict) or "card_key" not in body or "verdict" not in body:
        raise HTTPException(
            status_code=400,
            detail={
                "error": _ERR_BAD_FEEDBACK,
                "message": "card_key and verdict required",
            },
        )
    logger.info(
        "push-feedback: card_key=%s event_id=%s verdict=%s",
        body["card_key"],
        body.get("event_id", ""),
        body["verdict"],
    )
    return {"ok": True}
