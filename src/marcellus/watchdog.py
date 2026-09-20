"""Standalone health watchdog for the Frigate container.

Runs as its own process (see contrib/frigate-watchdog.service) so it stays
alive even if the sidecar web app's event loop is blocked by heavy analysis
work. Polls Frigate's HTTP API; when the backend is hung — connection refused
or repeated 5xx — for `failures_before_restart` consecutive probes, it runs the
configured restart command (default: ``docker restart frigate``).

Why this exists: Frigate's main process can hang on a frozen camera stream (an
OpenCV ffmpeg stream-timeout) while its s6 PID 1 keeps running. Docker's own
restart policy never fires (PID 1 is alive), the container shows "Up" but
unhealthy, and every /api/* request 500s via nginx until someone restarts it
by hand. This watchdog is the external recovery for exactly that failure mode.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import time
from collections import deque
from types import FrameType

import httpx

from marcellus.config import Settings, WatchdogSection, load_settings

logger = logging.getLogger("marcellus.watchdog")

# Probe outcomes.
_HEALTHY = "healthy"  # HTTP 200 — backend is serving
_DOWN = "down"  # transport error or 5xx — counts toward a restart
_OTHER = "other"  # e.g. 4xx — not the hang signature; logged, not acted on


class _StopFlag:
    """Set by SIGTERM/SIGINT so the loop and sleeps exit promptly."""

    def __init__(self) -> None:
        self.stop = False

    def install(self) -> _StopFlag:
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)
        return self

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        logger.info("received signal %s — shutting down", signum)
        self.stop = True


def _classify(url: str, timeout: float) -> tuple[str, str]:
    """Probe `url`; return (outcome, human-readable detail)."""
    try:
        resp = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        return _DOWN, f"transport error: {type(exc).__name__}: {exc}"
    if resp.status_code == 200:
        return _HEALTHY, "200 OK"
    if resp.status_code >= 500:
        return _DOWN, f"HTTP {resp.status_code}"
    return _OTHER, f"HTTP {resp.status_code}"


def _interruptible_sleep(seconds: float, flag: _StopFlag) -> None:
    """Sleep up to `seconds`, waking within ~1s if the stop flag is set."""
    end = time.monotonic() + seconds
    while not flag.stop:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def _prune(restarts: deque[float], now: float, window: float = 3600.0) -> None:
    while restarts and now - restarts[0] > window:
        restarts.popleft()


def _try_restart(cfg: WatchdogSection, restarts: deque[float], now: float) -> bool:
    """Run the restart command unless the hourly cap is hit. Returns True on success."""
    _prune(restarts, now)
    if len(restarts) >= cfg.max_restarts_per_hour:
        logger.critical(
            "Frigate still unhealthy but restart cap reached (%d in the last hour) "
            "— NOT restarting; manual intervention needed",
            len(restarts),
        )
        return False

    cmd = " ".join(cfg.restart_command)
    logger.critical(
        "Frigate unhealthy for %d consecutive probes — running: %s",
        cfg.failures_before_restart,
        cmd,
    )
    try:
        proc = subprocess.run(
            cfg.restart_command,
            capture_output=True,
            text=True,
            timeout=cfg.restart_timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.error("restart command timed out after %ss: %s", cfg.restart_timeout_s, cmd)
        return False
    except OSError as exc:
        logger.error("restart command failed to execute (%s): %s", cmd, exc)
        return False

    if proc.returncode == 0:
        restarts.append(now)
        logger.warning("restart OK (%s): %s", cmd, (proc.stdout or "").strip())
        return True
    logger.error(
        "restart command exited %d (%s): %s",
        proc.returncode,
        cmd,
        (proc.stderr or "").strip(),
    )
    return False


def run_watchdog(settings: Settings | None = None) -> int:
    """Blocking poll loop. Returns a process exit code."""
    settings = settings or load_settings()
    cfg = settings.watchdog
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO ("HTTP Request: GET ... 200 OK"); at our
    # poll cadence that floods journald with thousands of healthy-probe lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not cfg.enabled:
        logger.warning("watchdog disabled (watchdog.enabled=false) — exiting")
        return 0

    url = settings.frigate.base_url.rstrip("/") + cfg.probe_path
    logger.info(
        "watchdog up: probing %s every %gs; restart after %d consecutive failures "
        "(cmd=%s, cooldown=%gs, cap=%d/hr)",
        url,
        cfg.interval_s,
        cfg.failures_before_restart,
        " ".join(cfg.restart_command),
        cfg.cooldown_s,
        cfg.max_restarts_per_hour,
    )

    flag = _StopFlag().install()
    consecutive = 0
    restarts: deque[float] = deque()
    cooldown_until = 0.0

    while not flag.stop:
        now = time.monotonic()
        outcome, detail = _classify(url, cfg.timeout_s)

        if outcome == _HEALTHY:
            if consecutive:
                logger.info("Frigate healthy again after %d failed probe(s)", consecutive)
            consecutive = 0
        elif outcome == _OTHER:
            logger.warning("probe returned %s (not restart-worthy) — ignoring", detail)
        elif now < cooldown_until:
            logger.info(
                "probe failed (%s) but in post-restart cooldown (%ds left)",
                detail,
                int(cooldown_until - now),
            )
        else:
            consecutive += 1
            logger.warning(
                "probe failed (%s) [%d/%d]",
                detail,
                consecutive,
                cfg.failures_before_restart,
            )
            if consecutive >= cfg.failures_before_restart:
                restarted = _try_restart(cfg, restarts, now)
                consecutive = 0
                # Back off either way: a real restart needs boot time; a capped
                # or failed attempt shouldn't re-log every interval.
                backoff = cfg.cooldown_s if restarted else max(cfg.interval_s, 60.0)
                cooldown_until = time.monotonic() + backoff

        _interruptible_sleep(cfg.interval_s, flag)

    logger.info("watchdog stopped")
    return 0
