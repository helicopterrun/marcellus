"""Publisher transports.

`PushTransport` is the interface the engine (`engine.py`) sends through;
`LogTransport` and `RelayTransport` are the two implementations.

**Decision override from the spec (§4's open question), confirmed:** the
relay-visible alert text is fully generic ("New alert on your server"-style)
rather than naming the camera. The relay's only inputs are exactly the
spec's minimal set -- `{device_token, environment, handle, server_id,
severity}` plus the `apns-collapse-id` -- and it constructs the templated
`aps` text itself; camera/label detail is redeemed later by the NSE from the
handle, never sent here. This makes both transports below equally content-
-free by construction: there's no camera-name parameter to plumb through
even if a future transport wanted one.

No real APNs credentials exist yet (spec §4 -- the relay is the only sane
way to get Elsinore's `.p8` key off the sidecar). `LogTransport` is what
every dev/test environment runs against until a relay is actually deployed;
`RelayTransport` is the wire contract the relay should implement, exercised
here against a mock relay in tests, never against real Apple infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from marcellus.push.models import Device
from marcellus.push.stats import STATS

logger = logging.getLogger(__name__)

# -- Retry backoff (RelayTransport, wave 2A) ----------------------------------
_BACKOFF_BASE_S = 0.5
_BACKOFF_MULT = 3.0
_BACKOFF_JITTER = 0.3  # +/- 30%
_RETRY_AFTER_CAP_S = 5.0


def compute_retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Delay before the *next* attempt.

    `attempt` is 0-indexed by retry number (0 = the delay before the first
    retry, i.e. after the 1st attempt failed; 1 = before the 2nd retry; ...),
    giving 0.5, 1.5, 4.5... seconds before jitter. `retry_after`, when given
    (a 429 response's numeric `Retry-After` header), overrides the exponential
    schedule entirely and is capped at 5s so a large value from the relay
    can't stall a bounded retry loop.

    Exposed at module level (rather than buried in the retry loop) so tests
    can pin it and so `asyncio.sleep` can be patched around a deterministic
    delay value.
    """
    if retry_after is not None:
        return min(retry_after, _RETRY_AFTER_CAP_S)
    base = _BACKOFF_BASE_S * (_BACKOFF_MULT**attempt)
    jitter = random.uniform(1 - _BACKOFF_JITTER, 1 + _BACKOFF_JITTER)
    return base * jitter


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _exc_error(exc: BaseException) -> str:
    """A `TransportResult.error` string that is never empty.

    `str(exc)` is blank for some httpx exceptions (e.g. certain
    `ConnectError`/`ReadTimeout` instances carry no message), which produced
    logs like `ok=False error=` with nothing to diagnose. Falling back to the
    exception's class name keeps `error` diagnostic even then.
    """
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _is_relay_token_error(resp: httpx.Response) -> bool:
    """True only when a relay 422 body says the device token itself is bad.

    The relay (elsinore-push-relay/src/relay.ts) returns 422 for many
    unrelated validation failures -- bad `environment`, over-long fields,
    missing fields -- as well as for a malformed `device_token`. Only the
    token-specific case means the device is actually dead; everything else
    is a payload/code bug on our side and must not prune. Defensive: an
    unparseable body is treated as NOT a token error (the safe default).
    """
    try:
        payload = resp.json()
    except ValueError:
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, str):
        return False
    return "device_token" in error.lower()


@dataclass
class RelayHealth:
    """Module-level, in-memory relay health (alerts-slice2 §C), updated by
    `RelayTransport._send_with_retry` on every relay HTTP response (not on
    breaker-skip/transport-exception paths without a response, except
    `last_error`/`last_error_at`, which those *do* update -- a status page
    that goes silent the moment the relay is actually down would be the one
    time this matters most).

    Process-lifetime only, like `STATS` -- a restart resets it, which is fine
    for a "since last restart" health snapshot.
    """

    last_ok_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    last_status_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        from marcellus.push.store import epoch_to_iso as _epoch_to_iso

        return {
            "last_ok_at": _epoch_to_iso(self.last_ok_at),
            "last_error": self.last_error,
            "last_error_at": _epoch_to_iso(self.last_error_at),
            "last_status_code": self.last_status_code,
        }


#: Single process-wide instance. `routes/push.py`'s status endpoint reads
#: this directly rather than reaching through a `push_engine`/`transport`
#: instance, so relay health is reportable even when constructed against a
#: different transport instance in tests.
RELAY_HEALTH = RelayHealth()


def reset_relay_health_for_tests() -> None:
    RELAY_HEALTH.last_ok_at = None
    RELAY_HEALTH.last_error = None
    RELAY_HEALTH.last_error_at = None
    RELAY_HEALTH.last_status_code = None


@dataclass
class TransportResult:
    ok: bool
    # True if the relay/APNs reported the token as permanently dead (410
    # Unregistered, 400 BadDeviceToken, spec §5, or 422 payload-rejected) --
    # the caller prunes the device row immediately rather than retrying.
    unregistered: bool = False
    error: str | None = None
    status_code: int | None = None


class PushTransport(Protocol):
    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult: ...

    async def send_situation(
        self,
        device: Device,
        *,
        payload: dict[str, Any],
        collapse_id: str,
        apns_priority: int | None = None,
        apns_expiration: int | None = None,
    ) -> TransportResult:
        """One Interrupt-tier situation push (plan §8).

        Distinct from `send()` because the payload is built *here*, not at the
        relay: a situation's title is its user-authored name and its body
        names the label and dwell, none of which a fixed severity-keyed
        template can produce. Plan §8's "Relay boundary" paragraph settles
        what that means for the privacy line -- the relay forwards these bytes
        to APNs in flight without persisting, logging, or inspecting them,
        which is what "content-free **at rest**" has always meant and is how
        every push service works. The snapshot still never transits it.
        """
        ...

    async def send_live_activity(
        self,
        device: Device,
        *,
        token: str,
        payload: dict[str, Any],
        collapse_id: str,
        event: str,
        apns_priority: int | None = None,
        apns_expiration: int | None = None,
    ) -> TransportResult:
        """One Live Activity push: `start`, `update`, or `end` (Phase 2).

        Three things make this not a `send_situation` with a different body,
        which is why it is its own method rather than a flag:

        * **A different token.** `start` goes to the device's push-to-start
          token, `update`/`end` to the per-activity token iOS minted. Neither
          is `device.apns_token`, so the token is passed in.
        * **A different `apns-push-type`.** `liveactivity`, not `alert`.
        * **A different `apns-topic`.** `<bundle-id>.push-type.liveactivity`.
          Apple rejects a live-activity push sent to the plain app topic.
        """
        ...

    async def send_test(self, device: Device) -> TransportResult:
        """One fixed, self-contained test alert to a single device (spec §1).

        Deliberately not `send()` with a throwaway handle: the spec's test push
        carries **no** `handle` key and no `mutable-content`, because there is
        nothing for the NSE to redeem -- it passes the payload through
        unmodified and a tap just opens the app. Routing it through the normal
        send path would hand the relay a handle that resolves to nothing.

        Environment routing is *not* bypassed: the device row's sandbox/prod
        value picks the APNs endpoint exactly as a real send would, so a
        black-holed token fails here the same way it fails in production. That
        is the entire point of the button.
        """
        ...


class LogTransport:
    """Mock transport: logs what would be sent, always succeeds.

    Lets the entire pipeline -- MQTT subscriber, decision engine,
    registration, handle minting -- be exercised end to end with no APNs
    credentials and no relay deployed. `sent` records every call for tests.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "handle": handle,
            "server_id": server_id,
            "severity": severity,
            "collapse_id": collapse_id,
        }
        self.sent.append(record)
        logger.info(
            "push (mock transport): device=%s environment=%s severity=%s handle=%s "
            "collapse_id=%s server_id=%s",
            device.device_id, device.environment, severity, handle, collapse_id, server_id,
        )
        return TransportResult(ok=True)

    async def send_situation(
        self, device: Device, *, payload: dict[str, Any], collapse_id: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "payload": payload,
            "collapse_id": collapse_id,
            "situation_id": payload.get("situation_id", ""),
            "handle": payload.get("handle", ""),
        }
        self.sent.append(record)
        alert = payload.get("aps", {}).get("alert", {})
        logger.info(
            "push (mock transport): device=%s situation=%s collapse_id=%s title=%r body=%r "
            "handle=%s -- not delivered anywhere; set push.transport=relay for a real send",
            device.device_id, payload.get("situation_id"), collapse_id,
            alert.get("title"), alert.get("body"), payload.get("handle"),
        )
        return TransportResult(ok=True)

    async def send_live_activity(
        self, device: Device, *, token: str, payload: dict[str, Any],
        collapse_id: str, event: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "live_activity": True,
            "event": event,
            "token": token,
            "payload": payload,
            "collapse_id": collapse_id,
        }
        self.sent.append(record)
        state = payload.get("aps", {}).get("content-state", {})
        logger.info(
            "push (mock transport): LA %s device=%s collapse_id=%s stage=%s dwell=%s "
            "-- not delivered anywhere; set push.transport=relay for a real send",
            event, device.device_id, collapse_id,
            state.get("stage"), state.get("dwell_seconds"),
        )
        return TransportResult(ok=True)

    async def send_test(self, device: Device) -> TransportResult:
        record: dict[str, object] = {
            "device_id": device.device_id,
            "environment": device.environment,
            "test": True,
        }
        self.sent.append(record)
        # Loud, because on a mock transport `{"sent": true}` is a statement
        # about this process only -- nothing reaches a phone. Someone pressing
        # the app's button and seeing success needs this line to exist.
        logger.info(
            "push (mock transport): TEST push for device=%s environment=%s -- "
            "not delivered anywhere; set push.transport=relay for a real send",
            device.device_id, device.environment,
        )
        return TransportResult(ok=True)


class RelayTransport:
    """Posts the minimal relay payload (spec §4) over HTTP.

    The relay holds the one real `.p8` key and signs the APNs provider JWT;
    it never receives a camera name, label, or anything content-bearing --
    only `{device_token, environment, handle, server_id, severity}` plus the
    collapse id, which it uses to fill in a fixed, severity-keyed template
    before forwarding to APNs.
    """

    #: The sidecar's own API, its DB CHECK constraint and spec §1 all spell the
    #: production environment `prod`; the relay's wire API spells it
    #: `production` and rejects anything else with 422. Translating here, at the
    #: one boundary between the two vocabularies, keeps `prod` the only spelling
    #: anywhere else in this codebase. Without it every push to a prod-registered
    #: device was rejected and no production device could ever be notified --
    #: invisible so far only because deployments run the mock transport.
    _RELAY_ENVIRONMENT = {"prod": "production", "sandbox": "sandbox"}

    #: How long an idle connection to the relay is kept for reuse. Handoff
    #: item 15: one long-lived connection per sidecar process, so the first
    #: push after a quiet hour doesn't pay TLS setup on the interrupt path.
    #: Cloudflare closes idle edge connections on its own schedule, so this is
    #: a ceiling on our side, not a guarantee -- and the relay -> APNs hop is
    #: explicitly not ours to keep warm (Workers don't guarantee outbound
    #: reuse across isolates).
    _KEEPALIVE_EXPIRY_S = 600.0

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
        relay_key: str = "",
        retry_attempts: int = 3,
        breaker_failures: int = 3,
        breaker_open_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or self._build_client(timeout)
        self._owns_client = client is None
        self._relay_key = relay_key
        if not relay_key:
            logger.warning("push: relay_key not set — relay requests will be unauthenticated")

        # -- Retry + circuit breaker (wave 2A) --
        self._retry_attempts = retry_attempts
        self._breaker_failures = breaker_failures
        self._breaker_open_s = breaker_open_s
        self._clock = clock
        # A "failure" here is a transport exception or 5xx at the *attempt*
        # level (429/other 4xx never count -- they mean the relay was
        # reached and answered). Any response <500 resets this to 0.
        self._consecutive_failures = 0
        # Epoch (per `clock`) the breaker re-closes at; 0 means closed.
        self._open_until = 0.0

    @classmethod
    def _build_client(cls, timeout: float) -> httpx.AsyncClient:
        limits = httpx.Limits(
            max_keepalive_connections=4, max_connections=8,
            keepalive_expiry=cls._KEEPALIVE_EXPIRY_S,
        )
        try:
            return httpx.AsyncClient(timeout=timeout, limits=limits, http2=True)
        except ImportError:
            # `httpx[http2]` (the `h2` package) isn't installed. HTTP/1.1
            # keep-alive still reuses the connection, which is where nearly
            # all of the saving in handoff item 15 comes from -- multiplexing
            # buys a single-request-at-a-time sender very little.
            logger.info(
                "push: h2 not installed, relay connection is HTTP/1.1 keep-alive "
                "(install marcellus with the http2 extra for HTTP/2)"
            )
            return httpx.AsyncClient(timeout=timeout, limits=limits)

    @classmethod
    def _environment(cls, device: Device) -> str:
        return cls._RELAY_ENVIRONMENT.get(device.environment, device.environment)

    def _headers(self) -> dict[str, str]:
        if self._relay_key:
            return {"x-relay-key": self._relay_key}
        return {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self,
        device: Device,
        *,
        handle: str,
        server_id: str,
        severity: str,
        collapse_id: str,
    ) -> TransportResult:
        payload = {
            "device_token": device.apns_token,
            "environment": self._environment(device),
            "handle": handle,
            "server_id": server_id,
            "severity": severity,
            "apns-collapse-id": collapse_id,
        }
        url = f"{self.base_url}/v1/relay/push"
        return await self._send_with_retry(url, payload, kind="push", log_key=collapse_id)

    async def send_situation(
        self, device: Device, *, payload: dict[str, Any], collapse_id: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        """POST a fully-built situation payload to `/v1/relay/situation`."""
        body: dict[str, Any] = {
            "device_token": device.apns_token,
            "environment": self._environment(device),
            "apns-collapse-id": collapse_id,
            "payload": payload,
        }
        if apns_priority is not None:
            body["apns_priority"] = apns_priority
        if apns_expiration is not None:
            body["apns_expiration"] = apns_expiration
        url = f"{self.base_url}/v1/relay/situation"
        return await self._send_with_retry(url, body, kind="situation", log_key=collapse_id)

    async def send_live_activity(
        self, device: Device, *, token: str, payload: dict[str, Any],
        collapse_id: str, event: str,
        apns_priority: int | None = None, apns_expiration: int | None = None,
    ) -> TransportResult:
        """POST a Live Activity push to `/v1/relay/liveactivity`."""
        body: dict[str, Any] = {
            "device_token": token,
            "environment": self._environment(device),
            "apns-collapse-id": collapse_id,
            "event": event,
            "payload": payload,
        }
        # Underscored keys are the relay's wire contract (checkDeliveryHints);
        # hyphenated variants are silently ignored there.
        if apns_priority is not None:
            body["apns_priority"] = apns_priority
        if apns_expiration is not None:
            body["apns_expiration"] = apns_expiration
        url = f"{self.base_url}/v1/relay/liveactivity"
        return await self._send_with_retry(
            url, body, kind="liveactivity", event=event, log_key=collapse_id
        )

    async def send_test(self, device: Device) -> TransportResult:
        """POST the test push to the relay's `/v1/relay/test`.

        A separate endpoint rather than a flag on `/v1/relay/push`, because the
        two payloads differ in kind: the normal one is templated by severity and
        carries `handle` + `mutable-content` for the NSE, while this one is a
        fixed literal alert with neither. `/v1/relay/push` validates `handle` as
        required, so a test send cannot go through it at all.

        Implemented in elsinore-push-relay (`/v1/relay/test`); a relay that
        predates it returns 404 here and surfaces as `test_send_failed` --
        not a silent success.
        """
        payload = {
            "device_token": device.apns_token,
            "environment": self._environment(device),
        }
        url = f"{self.base_url}/v1/relay/test"
        return await self._send_with_retry(
            url, payload, kind="test", log_key=device.device_id
        )

    # -- Retry + circuit breaker (wave 2A) ------------------------------------

    def _max_attempts(self, kind: str, event: str | None) -> int:
        """Attempts for one *logical* send, before the circuit breaker's own
        half-open override (which always forces exactly 1)."""
        if kind == "liveactivity":
            # A late retried `update` can arrive after the `end` that
            # superseded it -- the next frame already supersedes a stale one,
            # so there is nothing a retry buys here. `start`/`end` behave
            # like `push`.
            return 1 if event == "update" else self._retry_attempts
        if kind == "push":
            return self._retry_attempts
        # situation, test: one attempt, no supersession semantics to lean on.
        return 1

    def _breaker_skip(self) -> TransportResult | None:
        """`TransportResult` to return immediately (no HTTP attempt) if the
        breaker is open, else `None` to proceed (possibly as a half-open
        probe)."""
        now = self._clock()
        if self._open_until and now < self._open_until:
            logger.debug("push: relay breaker open, skipping send without an HTTP attempt")
            STATS.incr("relay.breaker.skipped")
            STATS.incr("relay.send.failed")
            return TransportResult(ok=False, error="breaker open")
        return None

    def _breaker_is_open(self) -> bool:
        """True while the breaker is open. Checked after each failed attempt
        so a send whose own failures just tripped the breaker stops retrying
        -- the remaining attempts would only pile onto a relay we've just
        declared down."""
        return bool(self._open_until) and self._clock() < self._open_until

    def _is_half_open_probe(self) -> bool:
        now = self._clock()
        return bool(self._open_until) and now >= self._open_until

    def _record_breaker_outcome(self, *, failed: bool) -> None:
        """Update breaker state from one HTTP attempt's outcome.

        `failed` = transport exception or 5xx; any other outcome (200,
        4xx including 429) counts as a success for the breaker's purposes --
        the relay was reached and answered, whatever it said.
        """
        was_open = bool(self._open_until)
        if not failed:
            self._consecutive_failures = 0
            if was_open:
                self._open_until = 0.0
                STATS.gauge("relay.breaker.state", 0)
                STATS.gauge("relay.breaker.open_until", 0)
                logger.info("relay breaker closed")
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_failures:
            now = self._clock()
            self._open_until = now + self._breaker_open_s
            STATS.incr("relay.breaker.open")
            STATS.gauge("relay.breaker.state", 1)
            # Wall-clock epoch, for humans reading the status page -- `now`
            # and `_open_until` above are on the injected monotonic clock.
            STATS.gauge("relay.breaker.open_until", time.time() + self._breaker_open_s)
            logger.warning(
                "relay breaker open for %.0fs after %d consecutive failures",
                self._breaker_open_s, self._consecutive_failures,
            )

    async def _send_with_retry(
        self,
        url: str,
        body: dict[str, Any],
        *,
        kind: str,
        event: str | None = None,
        log_key: str = "",
    ) -> TransportResult:
        skip = self._breaker_skip()
        if skip is not None:
            return skip

        probe = self._is_half_open_probe()
        max_attempts = 1 if probe else self._max_attempts(kind, event)
        headers = self._headers()
        label = kind if event is None else f"{kind} {event}"

        last_error = "unknown error"
        retry_after: float | None = None
        for attempt in range(max_attempts):
            STATS.incr(f"relay.send.{kind}.attempts")
            if attempt > 0:
                STATS.incr("relay.retry")
                delay = compute_retry_delay(attempt - 1, retry_after)
                retry_after = None
                await asyncio.sleep(delay)

            try:
                resp = await self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                self._record_breaker_outcome(failed=True)
                last_error = _exc_error(exc)
                RELAY_HEALTH.last_error = last_error
                RELAY_HEALTH.last_error_at = time.time()
                RELAY_HEALTH.last_status_code = None
                logger.debug(
                    "push: relay %s attempt %d/%d transport error: %s",
                    label, attempt + 1, max_attempts, last_error,
                )
                if self._breaker_is_open():
                    break
                continue

            if resp.status_code == 200:
                self._record_breaker_outcome(failed=False)
                STATS.incr("relay.send.ok")
                RELAY_HEALTH.last_ok_at = time.time()
                RELAY_HEALTH.last_status_code = 200
                return TransportResult(ok=True)

            if resp.status_code in (410, 400, 422):
                # 410 Unregistered / 400 BadDeviceToken (spec §5): always a
                # dead token, never retried -- the caller deletes the device
                # row. 422 is the relay's generic payload-validation error and
                # covers multiple, unrelated failure modes -- only some are
                # token-specific (e.g. "device_token must be hex"). Others
                # (bad environment, field-length/type errors, missing
                # fields) are payload/code bugs on our side, not dead
                # devices, and must NOT prune -- pruning on those would wipe
                # every device on a single sidecar bug. Only treat 422 as
                # unregistered when the relay's error body names the token.
                self._record_breaker_outcome(failed=False)
                logger.warning("push: relay %s body: %s", resp.status_code, resp.text[:500])
                is_token_error = resp.status_code in (410, 400) or _is_relay_token_error(resp)
                if not is_token_error:
                    # Non-token 422: treat as before Fix A -- log and move
                    # on, breaker-success, no prune, no retry.
                    RELAY_HEALTH.last_error = f"HTTP {resp.status_code}"
                    RELAY_HEALTH.last_error_at = time.time()
                    RELAY_HEALTH.last_status_code = resp.status_code
                    return TransportResult(
                        ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                STATS.incr("relay.send.unregistered")
                RELAY_HEALTH.last_error = f"HTTP {resp.status_code}"
                RELAY_HEALTH.last_error_at = time.time()
                RELAY_HEALTH.last_status_code = resp.status_code
                return TransportResult(
                    ok=False, unregistered=True, error=f"HTTP {resp.status_code}",
                    status_code=resp.status_code,
                )

            if resp.status_code == 429:
                # Per-token rate limit -- never reached Apple, which otherwise
                # looks identical to a delivered-but-throttled LA update.
                self._record_breaker_outcome(failed=False)
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                if attempt == max_attempts - 1:
                    logger.warning(
                        "push: relay %s (rate limited) body: %s",
                        resp.status_code, resp.text[:500],
                    )
                    STATS.incr("relay.send.rejected")
                    RELAY_HEALTH.last_error = f"HTTP 429: {resp.text[:200]}"
                    RELAY_HEALTH.last_error_at = time.time()
                    RELAY_HEALTH.last_status_code = 429
                    return TransportResult(
                        ok=False, error=f"HTTP 429: {resp.text[:200]}", status_code=429,
                    )
                last_error = f"HTTP 429: {resp.text[:200]}"
                logger.debug(
                    "push: relay %s attempt %d/%d got 429, retrying",
                    label, attempt + 1, max_attempts,
                )
                continue

            if resp.status_code >= 500:
                self._record_breaker_outcome(failed=True)
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                RELAY_HEALTH.last_error = last_error
                RELAY_HEALTH.last_error_at = time.time()
                RELAY_HEALTH.last_status_code = resp.status_code
                logger.debug(
                    "push: relay %s attempt %d/%d got %s",
                    label, attempt + 1, max_attempts, resp.status_code,
                )
                if self._breaker_is_open():
                    break
                continue

            # Any other 4xx: terminal, never retried.
            self._record_breaker_outcome(failed=False)
            STATS.incr("relay.send.failed")
            RELAY_HEALTH.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            RELAY_HEALTH.last_error_at = time.time()
            RELAY_HEALTH.last_status_code = resp.status_code
            return TransportResult(
                ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )

        # Retries exhausted, the breaker tripped mid-send, or the half-open
        # probe itself failed.
        STATS.incr("relay.send.failed")
        RELAY_HEALTH.last_error = last_error
        RELAY_HEALTH.last_error_at = time.time()
        logger.warning(
            "push: relay %s send failed key=%s attempts=%d last_error=%s",
            label, log_key, attempt + 1, last_error,
        )
        return TransportResult(ok=False, error=last_error)
