"""SQLite-backed storage for push devices and handles.

Uses the sidecar's own DB (`db.open_sidecar`) -- same pattern as the
scrub-cache tables, just a different pair of tables (`push_devices`,
`push_handles`, both in `db.SIDECAR_SCHEMA`).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, cast

from marcellus.push.models import Device


def device_id_for_token(apns_token: str) -> str:
    """Deterministic local handle derived from the token.

    Not a second identity -- re-registering the same token must yield the
    same `device_id` so a client that logs it for support purposes sees a
    stable value across the idempotent-PUT re-registrations the spec
    requires (§1: iOS reissues tokens rarely, but every relaunch re-PUTs the
    *current* one).
    """
    digest = hashlib.sha256(apns_token.encode()).hexdigest()
    return f"d_{digest[:10]}"


def _col(row: sqlite3.Row, name: str, default: object = None) -> Any:
    """`row[name]`, or `default` when the column isn't present."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _json_or(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _row_to_device(row: sqlite3.Row) -> Device:
    from marcellus.push.situations import parse_situations

    situations = parse_situations(_json_or(_col(row, "situations"), []))
    loc = _json_or(_col(row, "location"), None)
    location: tuple[float, float] | None = None
    if isinstance(loc, dict):
        try:
            location = (float(loc["lat"]), float(loc["lon"]))
        except (KeyError, TypeError, ValueError):
            location = None

    return Device(
        apns_token=row["apns_token"],
        device_id=row["device_id"],
        bundle_id=row["bundle_id"],
        environment=row["environment"],
        app_version=row["app_version"],
        cameras=tuple(json.loads(row["cameras"])),
        labels=tuple(json.loads(row["labels"])),
        min_severity=row["min_severity"],
        # The stored `schema_version` is what the client claimed; what decides
        # the evaluation path is `situations`, and a row with rules on it is a
        # v2 row whatever the client called itself.
        schema_version=2 if situations else int(_col(row, "schema_version", 1) or 1),
        timezone=str(_col(row, "timezone", "") or ""),
        location=location,
        situations=situations,
        live_activity_token=str(_col(row, "live_activity_token", "") or ""),
        push_to_start_token=str(_col(row, "push_to_start_token", "") or ""),
        la_capable=bool(
            int(_col(row, "la_capable", 1) if _col(row, "la_capable", 1) is not None else 1)
        ),
        frequent_pushes_enabled=bool(int(_col(row, "frequent_pushes_enabled", 0) or 0)),
    )


# Registration fields this phase persists without reading. Named here so the
# later-phase handoffs have one place to look for what is already arriving.
IGNORED_REGISTRATION_FIELDS = ("live_activity_token", "morning_digest", "llm")


def upsert_device(
    conn: sqlite3.Connection,
    *,
    apns_token: str,
    bundle_id: str,
    environment: str,
    app_version: str = "",
    cameras: list[str] | None = None,
    labels: list[str] | None = None,
    min_severity: str = "alert",
    schema_version: int = 1,
    timezone_name: str = "",
    location: dict[str, float] | None = None,
    situations: list[dict[str, Any]] | None = None,
    live_activity_token: str = "",
    morning_digest: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
    push_to_start_token: str = "",
    la_capable: bool = True,
    frequent_pushes_enabled: bool = False,
) -> str:
    """Idempotent PUT on the token (spec §1) -- overwrites filter state in
    place rather than accumulating duplicate rows that would double-fire
    alerts. Returns the device_id.

    Every v2 field is persisted whether or not this phase evaluates it (plan
    §8 / handoff item 1), so an app build can start sending the full shape
    before the sidecar acts on all of it.
    """
    device_id = device_id_for_token(apns_token)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO push_devices "
        "(apns_token, device_id, bundle_id, environment, app_version, cameras, labels, "
        " min_severity, registered_at, updated_at, schema_version, timezone, location, "
        " situations, live_activity_token, morning_digest, llm, push_to_start_token, "
        " la_capable, frequent_pushes_enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(apns_token) DO UPDATE SET "
        "bundle_id=excluded.bundle_id, environment=excluded.environment, "
        "app_version=excluded.app_version, cameras=excluded.cameras, labels=excluded.labels, "
        "min_severity=excluded.min_severity, updated_at=excluded.updated_at, "
        "schema_version=excluded.schema_version, timezone=excluded.timezone, "
        "location=excluded.location, situations=excluded.situations, "
        "live_activity_token=excluded.live_activity_token, "
        "morning_digest=excluded.morning_digest, llm=excluded.llm, "
        "la_capable=excluded.la_capable, "
        "frequent_pushes_enabled=excluded.frequent_pushes_enabled, "
        # A re-registration that omits the token must not blank a working one:
        # the app uploads it from an async token stream, so the first PUT after
        # launch can legitimately race ahead of the token arriving.
        "push_to_start_token=CASE WHEN excluded.push_to_start_token != '' "
        " THEN excluded.push_to_start_token ELSE push_devices.push_to_start_token END",
        (
            apns_token, device_id, bundle_id, environment, app_version,
            json.dumps(cameras or []), json.dumps(labels or []), min_severity, now, now,
            int(schema_version), timezone_name,
            json.dumps(location) if location else None,
            json.dumps(situations or []), live_activity_token,
            json.dumps(morning_digest) if morning_digest is not None else None,
            json.dumps(llm) if llm is not None else None,
            push_to_start_token, int(la_capable), int(frequent_pushes_enabled),
        ),
    )
    # Commits itself (spec Wave 2B §3): sqlite3's default isolation leaves
    # the write transaction -- and the WAL write lock -- open across every
    # `await` in the caller until *something* commits, which is the
    # "database is locked" HTTP routes were seeing. Single-statement helpers
    # commit right here instead of leaving it to the caller.
    conn.commit()
    return device_id


def delete_device(conn: sqlite3.Connection, apns_token: str) -> bool:
    """Delete a device row. Used for explicit unregistration (DELETE
    /v1/push/devices/{token}) and 410/400 feedback-driven pruning (spec §5).
    Returns True if a row was actually removed."""
    cur = conn.execute("DELETE FROM push_devices WHERE apns_token = ?", (apns_token,))
    conn.commit()
    return cur.rowcount > 0


def get_device(conn: sqlite3.Connection, apns_token: str) -> Device | None:
    row = conn.execute(
        "SELECT * FROM push_devices WHERE apns_token = ?", (apns_token,)
    ).fetchone()
    return _row_to_device(row) if row else None


def get_device_row(conn: sqlite3.Connection, apns_token: str) -> sqlite3.Row | None:
    """Raw `push_devices` row (alerts-slice2 §B's device-detail route needs
    `registered_at`/`updated_at`, which aren't on the `Device` dataclass)."""
    row = conn.execute(
        "SELECT * FROM push_devices WHERE apns_token = ?", (apns_token,)
    ).fetchone()
    return cast(sqlite3.Row, row) if row is not None else None


def list_devices(conn: sqlite3.Connection) -> list[Device]:
    rows = conn.execute("SELECT * FROM push_devices").fetchall()
    return [_row_to_device(r) for r in rows]


def mint_handle(
    conn: sqlite3.Connection,
    *,
    camera: str,
    event_id: str,
    review_id: str,
    ttl_s: float,
    now: float | None = None,
    situation_id: str = "",
    track_id: str = "",
) -> str:
    """Mint an opaque, short-lived handle mapping to {camera, event_id}.

    Not the raw Frigate event id itself -- a Frigate id is
    `<start_time>-<rand>` and embeds a wall-clock timestamp, exactly the kind
    of incidental leak the spec's privacy line (§4) exists to avoid. The NSE
    and app redeem the handle server-side; they never see the raw id from
    the APNs payload.
    """
    now = time.time() if now is None else now
    handle = f"h_{secrets.token_urlsafe(8)}"
    conn.execute(
        "INSERT INTO push_handles (handle, camera, event_id, review_id, created_at, expires_at, "
        "situation_id, track_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (handle, camera, event_id, review_id, now, now + ttl_s, situation_id, track_id),
    )
    conn.commit()
    return handle


def store_thumbnail(conn: sqlite3.Connection, handle: str, jpeg: bytes) -> bool:
    """Attach the pre-warmed JPEG to an already-minted handle (plan §4 lever 1).

    Separate from `mint_handle` because the two happen in parallel, not in
    series: the push goes out the moment the situation matches, and the
    thumbnail lands beside it while APNs is still carrying the alert. A
    thumbnail that never arrives is a notification without an image, never a
    notification withheld.
    """
    cur = conn.execute(
        "UPDATE push_handles SET thumbnail = ? WHERE handle = ?", (sqlite3.Binary(jpeg), handle)
    )
    conn.commit()
    return cur.rowcount > 0


def get_thumbnail(
    conn: sqlite3.Connection, handle: str, *, now: float | None = None
) -> bytes | None:
    """The NSE's `GET /v1/push/thumbnail/{handle}` payload, or None if the
    handle is unknown, expired, or its warm-up didn't land."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT thumbnail FROM push_handles WHERE handle = ? AND expires_at > ?",
        (handle, now),
    ).fetchone()
    if row is None or row["thumbnail"] is None:
        return None
    return bytes(row["thumbnail"])


# -- Snooze / mute (plan §6) -------------------------------------------------


def set_snooze(
    conn: sqlite3.Connection,
    *,
    apns_token: str,
    scope: str,
    until_epoch: float,
    now: float | None = None,
) -> None:
    """Snooze `scope` for one device until `until_epoch`.

    Upsert rather than insert: re-snoozing an already-snoozed scope extends
    (or shortens) it, which is what "Snooze 15m" pressed twice has to mean.
    """
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_snoozes (apns_token, scope, until_epoch, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(apns_token, scope) DO UPDATE SET until_epoch=excluded.until_epoch",
        (apns_token, scope, float(until_epoch), now),
    )
    conn.commit()


def clear_snooze(conn: sqlite3.Connection, *, apns_token: str, scope: str) -> bool:
    cur = conn.execute(
        "DELETE FROM push_snoozes WHERE apns_token = ? AND scope = ?", (apns_token, scope)
    )
    conn.commit()
    return cur.rowcount > 0


def active_snoozes(
    conn: sqlite3.Connection, apns_token: str, *, now: float | None = None
) -> set[str]:
    """Scopes currently silenced for this device.

    Expiry is a timestamp comparison, not a scheduled job -- the snooze
    re-enables itself at `until_epoch` with nothing to run and nothing to miss
    if the sidecar was restarted in between.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT scope FROM push_snoozes WHERE apns_token = ? AND until_epoch > ?",
        (apns_token, now),
    ).fetchall()
    return {r["scope"] for r in rows}


def list_snoozes(
    conn: sqlite3.Connection, apns_token: str, *, now: float | None = None
) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT scope, until_epoch FROM push_snoozes "
        "WHERE apns_token = ? AND until_epoch > ? ORDER BY scope",
        (apns_token, now),
    ).fetchall()
    return [{"scope": r["scope"], "until_epoch": r["until_epoch"]} for r in rows]


def prune_expired_snoozes(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_snoozes WHERE until_epoch <= ?", (now,))
    conn.commit()
    return cur.rowcount


def replace_snoozes(
    conn: sqlite3.Connection, *, apns_token: str, snoozes: list[dict[str, Any]]
) -> None:
    """Set this device's snoozes to exactly `snoozes` (registration §8 field).

    Only called when the client *sent* the key: a registration that omits
    `snoozes` leaves existing ones alone. The app re-registers on every launch
    and on every token reissue, and a launch silently cancelling a snooze the
    user set an hour ago would break the plan's "state that survives app kill"
    non-negotiable in the most confusing way available.

    The delete and every insert run inside one `with conn:` block (spec Wave
    2B §3): this is the one helper that genuinely needs delete+insert(s)
    atomic -- a partial replacement (old snoozes gone, only some new ones
    landed) is a real user-visible bug, not just an internal inconsistency.
    """
    now = time.time()
    with conn:
        conn.execute("DELETE FROM push_snoozes WHERE apns_token = ?", (apns_token,))
        for item in snoozes:
            scope = str(item.get("scope") or "").strip()
            if not scope:
                continue
            # Plan §8 spells the registration field `until`; the standalone
            # `POST /v1/push/snooze` body spells it `until_epoch`. Both are
            # read here so a client using either vocabulary lands in the
            # same place.
            raw_until = item.get("until", item.get("until_epoch", 0))
            try:
                until = float(raw_until)
            except (TypeError, ValueError):
                continue
            if until > now:
                # Inlined rather than calling `set_snooze` (which self-commits):
                # every insert here must land inside this one `with conn:`
                # transaction, not as its own independently-committed write.
                conn.execute(
                    "INSERT INTO push_snoozes (apns_token, scope, until_epoch, created_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(apns_token, scope) DO UPDATE SET until_epoch=excluded.until_epoch",
                    (apns_token, scope, until, now),
                )


# -- Rate limiting (plan §6) -------------------------------------------------


def count_sends_since(
    conn: sqlite3.Connection, *, apns_token: str, situation_id: str, since: float
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM push_sends "
        "WHERE apns_token = ? AND situation_id = ? AND sent_at > ?",
        (apns_token, situation_id, since),
    ).fetchone()
    return int(row["n"]) if row else 0


def record_send(
    conn: sqlite3.Connection, *, apns_token: str, situation_id: str, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_sends (apns_token, situation_id, sent_at) VALUES (?, ?, ?)",
        (apns_token, situation_id, now),
    )
    conn.commit()


def bump_suppressed(conn: sqlite3.Connection, *, apns_token: str, situation_id: str) -> None:
    conn.execute(
        "INSERT INTO push_suppressed (apns_token, situation_id, count) VALUES (?, ?, 1) "
        "ON CONFLICT(apns_token, situation_id) DO UPDATE SET count = count + 1",
        (apns_token, situation_id),
    )
    conn.commit()


def take_suppressed(conn: sqlite3.Connection, *, apns_token: str, situation_id: str) -> int:
    """Read and reset the suppressed counter.

    Read-and-reset in one call because the count exists only to be spent on
    the next push's `" · +X more"` suffix (plan §6) -- leaving it behind would
    repeat the same claim on every subsequent push.
    """
    row = conn.execute(
        "SELECT count FROM push_suppressed WHERE apns_token = ? AND situation_id = ?",
        (apns_token, situation_id),
    ).fetchone()
    count = int(row["count"]) if row else 0
    if count:
        conn.execute(
            "DELETE FROM push_suppressed WHERE apns_token = ? AND situation_id = ?",
            (apns_token, situation_id),
        )
        conn.commit()
    return count


def prune_old_sends(
    conn: sqlite3.Connection, *, older_than: float, now: float | None = None
) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_sends WHERE sent_at <= ?", (now - older_than,))
    conn.commit()
    return cur.rowcount


# -- Card-mutation sends / device stats (alerts-slice2 §A/§B) ---------------


def record_card_send(
    conn: sqlite3.Connection,
    *,
    apns_token: str,
    card_key: str,
    mutation: str,
    sent_at: float | None = None,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """Record one card-mutation send for receipt pairing and device stats.

    Commits itself, same as `record_send` -- callers that batch several of
    these per mutation (one per eligible device) still get one commit per
    row rather than holding the WAL write lock across the loop's `await
    transport.*` calls.
    """
    sent_at = time.time() if sent_at is None else sent_at
    conn.execute(
        "INSERT INTO push_card_sends (apns_token, card_key, mutation, sent_at, ok, error) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (apns_token, card_key, mutation, sent_at, 1 if ok else 0, error),
    )
    conn.commit()


def device_stats(
    conn: sqlite3.Connection, apns_token: str, *, window_days: float = 7.0, now: float | None = None
) -> dict[str, Any]:
    """Send/receipt stats for `GET /v1/push/devices/{token}` (alerts-slice2
    §B). `sent` counts `push_card_sends` rows for this token in the window;
    `received` counts *distinct* paired receipts (a receipt with no
    `sent_at` still counted as accepted, but not as "received" against this
    device's send volume -- it just never found a matching send)."""
    now = time.time() if now is None else now
    since = now - window_days * 86400.0

    sent_row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(sent_at) AS last FROM push_card_sends "
        "WHERE apns_token = ? AND sent_at >= ?",
        (apns_token, since),
    ).fetchone()
    sent = int(sent_row["n"]) if sent_row else 0
    last_sent_at = sent_row["last"] if sent_row else None

    ok_row = conn.execute(
        "SELECT MAX(sent_at) AS last FROM push_card_sends "
        "WHERE apns_token = ? AND ok = 1 AND sent_at >= ?",
        (apns_token, since),
    ).fetchone()
    last_send_ok_at = ok_row["last"] if ok_row else None

    err_row = conn.execute(
        "SELECT error, sent_at FROM push_card_sends "
        "WHERE apns_token = ? AND ok = 0 AND sent_at >= ? "
        "ORDER BY sent_at DESC LIMIT 1",
        (apns_token, since),
    ).fetchone()
    last_send_error = err_row["error"] if err_row else None
    last_send_error_at = err_row["sent_at"] if err_row else None

    recv_rows = conn.execute(
        "SELECT latency_s, received_at FROM push_receipts "
        "WHERE apns_token = ? AND received_at >= ? AND sent_at IS NOT NULL",
        (apns_token, since),
    ).fetchall()
    received = len(recv_rows)
    latencies = [r["latency_s"] for r in recv_rows if r["latency_s"] is not None]
    last_received_at = max((r["received_at"] for r in recv_rows), default=None)

    from marcellus.db import percentile

    median_latency_s = percentile(latencies, 50) if latencies else None
    p90_latency_s = percentile(latencies, 90) if latencies else None
    if median_latency_s is not None and median_latency_s != median_latency_s:  # NaN guard
        median_latency_s = None
    if p90_latency_s is not None and p90_latency_s != p90_latency_s:
        p90_latency_s = None

    return {
        "window_days": window_days,
        "sent": sent,
        "received": received,
        "median_latency_s": median_latency_s,
        "p90_latency_s": p90_latency_s,
        "last_sent_at": epoch_to_iso(last_sent_at),
        "last_send_ok_at": epoch_to_iso(last_send_ok_at),
        "last_received_at": epoch_to_iso(last_received_at),
        "last_send_error": last_send_error,
        "last_send_error_at": epoch_to_iso(last_send_error_at),
    }


def epoch_to_iso(ts: float | None) -> str | None:
    """Contract: every `*_at` on the wire is ISO8601 `Z` (seconds); epoch
    floats stay internal to SQLite."""
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


# -- Live Activities (Phase 2) ----------------------------------------------

#: Sentinel (device-scoped) identity the sidecar itself writes for the one
#: Live Activity per device it manages (Elsinore Phase 4). Any other row --
#: notably the app's Settings -> "Try" debug demo activity, whose upload goes
#: through `attach_activity_token` with its own real `situation_id`/
#: `track_id` -- is not owned by the sidecar's lifecycle: it must never be
#: returned as "the device activity", ended, or updated by the delivery/
#: engine code. `delivery_wire.py` imports these rather than redefining them
#: so the two modules can't drift.
DEVICE_SITUATION_ID = "device:elsinore"
DEVICE_TRACK_ID = "device"


def open_activity(
    conn: sqlite3.Connection,
    *,
    activity_id: str,
    apns_token: str,
    situation_id: str,
    track_id: str,
    camera: str,
    collapse_id: str,
    handle: str,
    from_detection: bool = False,
    now: float | None = None,
) -> None:
    """Record that a start push went out for this (device, situation, track).

    The row exists before the app has a per-activity token: iOS creates the
    activity, hands the app a token, and only then does the app upload it. In
    between there is a live activity on screen the sidecar cannot yet update,
    which is a normal state, not an error.
    """
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_activities (activity_id, apns_token, situation_id, track_id, "
        " camera, collapse_id, handle, stage, from_detection, created_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'arriving', ?, ?, ?) "
        "ON CONFLICT(activity_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (activity_id, apns_token, situation_id, track_id, camera, collapse_id, handle,
         int(from_detection), now, now),
    )
    conn.commit()


def attach_activity_token(
    conn: sqlite3.Connection,
    *,
    activity_id: str,
    apns_token: str,
    situation_id: str,
    track_id: str,
    token: str,
    now: float | None = None,
) -> None:
    """Bind the per-activity push token the app just observed.

    Upsert rather than update: the app is the only source of `activity_id`, so
    a token can arrive for an activity the sidecar started under a synthetic
    id, or (with push-to-start) for one it started without knowing the id iOS
    would assign.

    On CONFLICT (a row already exists -- i.e. an activity the *sidecar*
    started, keyed under its `DEVICE_SITUATION_ID`/`DEVICE_TRACK_ID`
    sentinel) only `token`/`apns_token`/`last_seen_at` are updated:
    `situation_id`/`track_id` are deliberately left alone. The app posts
    back whatever `attributes.cardKey`/`trackId` it read off the activity --
    for a device-scoped activity that's just the sentinel, echoed as-is, but
    a stale/mismatched client can post a real card key instead, and
    clobbering the sentinel with that would make `find_activity` (which
    looks up by `DEVICE_SITUATION_ID`) unable to find the sidecar's own row
    ever again, causing missed updates and a duplicate start on the next
    mutation. On INSERT (no existing row -- e.g. the app's Settings -> "Try"
    debug demo activity, which the sidecar never opened) there is nothing to
    protect, so the app-supplied `situation_id`/`track_id` are stored as
    given, exactly as before.
    """
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_activities (activity_id, apns_token, situation_id, track_id, "
        " token, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(activity_id) DO UPDATE SET token=excluded.token, "
        " apns_token=excluded.apns_token, last_seen_at=excluded.last_seen_at",
        (activity_id, apns_token, situation_id, track_id, token, now, now),
    )
    conn.commit()


def find_activity(conn: sqlite3.Connection, *, apns_token: str) -> sqlite3.Row | None:
    """The single open live activity for this device, if any.

    Device-scoped (Elsinore Phase 4): one Live Activity per device now
    aggregates every open card, so lookup keys on `apns_token` plus the
    sidecar's own `DEVICE_SITUATION_ID` sentinel -- any other row (e.g. the
    app's debug demo activity, uploaded via `attach_activity_token` with its
    own real situation/track id) is not the device activity and must never
    be returned here.
    """
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM push_activities WHERE apns_token = ? AND situation_id = ? "
            "AND ended_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (apns_token, DEVICE_SITUATION_ID),
        ).fetchone(),
    )


def get_activity(conn: sqlite3.Connection, activity_id: str) -> sqlite3.Row | None:
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM push_activities WHERE activity_id = ?", (activity_id,)
        ).fetchone(),
    )


def touch_activity(
    conn: sqlite3.Connection,
    activity_id: str,
    *,
    stage: str | None = None,
    pushed: bool = False,
    seen: bool = True,
    thumbnail_revision: int | None = None,
    promoted: bool | None = None,
    dwell_seconds: int | None = None,
    now: float | None = None,
) -> None:
    now = time.time() if now is None else now
    sets = []
    args: list[Any] = []
    if stage is not None:
        sets.append("stage = ?")
        args.append(stage)
    if dwell_seconds is not None:
        sets.append("dwell_seconds = ?")
        args.append(dwell_seconds)
    if pushed:
        sets.append("last_push_at = ?")
        args.append(now)
    if seen:
        sets.append("last_seen_at = ?")
        args.append(now)
    if thumbnail_revision is not None:
        sets.append("thumbnail_revision = ?")
        args.append(thumbnail_revision)
    if promoted is not None:
        sets.append("promoted = ?")
        args.append(int(promoted))
    if not sets:
        return
    args.append(activity_id)
    conn.execute(f"UPDATE push_activities SET {', '.join(sets)} WHERE activity_id = ?", args)
    conn.commit()


def close_activity(
    conn: sqlite3.Connection, activity_id: str, *, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE push_activities SET ended_at = ?, stage = 'ending' WHERE activity_id = ?",
        (now, activity_id),
    )
    conn.commit()


def delete_activity(conn: sqlite3.Connection, activity_id: str) -> bool:
    """Deletes both the activity row and its send-history rows -- two
    statements that must land together (spec Wave 2B §3): a row deleted
    without its sends, or vice versa, is a dangling reference either way, so
    this is wrapped in `with conn:` rather than two independent commits."""
    with conn:
        cur = conn.execute("DELETE FROM push_activities WHERE activity_id = ?", (activity_id,))
        conn.execute("DELETE FROM push_activity_sends WHERE activity_id = ?", (activity_id,))
    return cur.rowcount > 0


def dismiss_activity(
    conn: sqlite3.Connection, activity_id: str, *, now: float | None = None
) -> bool:
    """The app-side dismissal tombstone (Phase A §3): close the row instead of
    deleting it, so a re-start for the same (device, situation, track) can be
    suppressed until an escalation breaks through it. Returns whether a row
    was actually tracked."""
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE push_activities SET ended_at = ?, stage = 'dismissed' "
        "WHERE activity_id = ? AND ended_at IS NULL",
        (now, activity_id),
    )
    conn.commit()
    if cur.rowcount > 0:
        return True
    # Idempotent: a second dismiss (or a dismiss racing close_activity) on an
    # already-ended row is still "was tracked" if the row exists at all.
    return get_activity(conn, activity_id) is not None


def find_dismissed_activity(
    conn: sqlite3.Connection, *, apns_token: str
) -> sqlite3.Row | None:
    """Mirrors `find_activity`, but for the dismissal tombstone left behind by
    `dismiss_activity`: an ended, `stage='dismissed'` row for this device,
    which suppresses a future re-start (including a brand-new story joining)
    until an ESCALATE clears it, or the device's last open story closes
    clears it (device-scoped quiet period, Elsinore Phase 4 §4). Scoped to
    `DEVICE_SITUATION_ID` for the same reason as `find_activity`: a demo row
    is never a real-story tombstone."""
    return cast(
        "sqlite3.Row | None",
        conn.execute(
            "SELECT * FROM push_activities WHERE apns_token = ? AND situation_id = ? "
            "AND ended_at IS NOT NULL AND stage = 'dismissed' "
            "ORDER BY created_at DESC LIMIT 1",
            (apns_token, DEVICE_SITUATION_ID),
        ).fetchone(),
    )


def stale_activities(
    conn: sqlite3.Connection, *, quiet_for: float, now: float | None = None
) -> list[sqlite3.Row]:
    """Live activities with no `frigate/events` observation for `quiet_for`.

    This is resolution: the object stopped being reported, so the situation is
    over. Frigate's own `end` message is the faster signal and the engine acts
    on it directly; this catches the case where it never arrives.

    Scoped to `DEVICE_SITUATION_ID`: the sweeper sends an `end` push for
    every row it returns, and a demo row (Settings -> "Try") must never be
    sent anything by the sidecar's lifecycle code. A quiet demo row isn't
    "stale" in any sense this sweep cares about -- it just sits there until
    the app itself ends it -- so excluding it here is the semantically
    correct behavior, not just a safety filter.
    """
    now = time.time() if now is None else now
    return conn.execute(
        "SELECT * FROM push_activities WHERE ended_at IS NULL AND situation_id = ? "
        "AND last_seen_at <= ?",
        (DEVICE_SITUATION_ID, now - quiet_for),
    ).fetchall()


def reap_activities(
    conn: sqlite3.Connection, *, older_than: float, now: float | None = None
) -> int:
    """Drop ended activities once their dismissal window has safely passed."""
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT activity_id FROM push_activities WHERE ended_at IS NOT NULL AND ended_at <= ?",
        (now - older_than,),
    ).fetchall()
    for row in rows:
        delete_activity(conn, row["activity_id"])
    return len(rows)


def record_activity_send(
    conn: sqlite3.Connection, *, activity_id: str, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    conn.execute(
        "INSERT INTO push_activity_sends (activity_id, sent_at) VALUES (?, ?)",
        (activity_id, now),
    )
    conn.commit()


def redeem_handle(
    conn: sqlite3.Connection, handle: str, *, now: float | None = None
) -> dict[str, str] | None:
    """Look up a handle, or None if it doesn't exist or has expired.

    Handles are single-use in the sense that they're short-lived, not that
    redemption consumes them -- the NSE (steps 2-3) and, potentially, the app
    itself both redeem the same handle from one push, and neither should
    race the other out of a result.
    """
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT camera, event_id FROM push_handles WHERE handle = ? AND expires_at > ?",
        (handle, now),
    ).fetchone()
    if row is None:
        return None
    return {"camera": row["camera"], "event_id": row["event_id"]}


def prune_expired_handles(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    now = time.time() if now is None else now
    cur = conn.execute("DELETE FROM push_handles WHERE expires_at <= ?", (now,))
    conn.commit()
    return cur.rowcount
