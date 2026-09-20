"""The watchdog is the thing that restarts Frigate — the one component whose
own bugs turn a hung camera stream into a restart loop or a silent no-op.
These tests pin the probe classification, the hourly restart cap, and the
failure paths of the restart command itself.
"""

from __future__ import annotations

import subprocess
from collections import deque
from unittest import mock

import httpx
import pytest

from marcellus.config import WatchdogSection
from marcellus.watchdog import _classify, _prune, _try_restart


def _cfg(**overrides: object) -> WatchdogSection:
    base: dict[str, object] = {
        "enabled": True,
        "restart_command": ["true"],
        "max_restarts_per_hour": 3,
        "restart_timeout_s": 5.0,
        "failures_before_restart": 3,
    }
    base.update(overrides)
    return WatchdogSection(**base)  # type: ignore[arg-type]


# -- probe classification ----------------------------------------------------


def _classify_with(response_or_exc: httpx.Response | Exception) -> str:
    def _get(url: str, timeout: float) -> httpx.Response:
        if isinstance(response_or_exc, Exception):
            raise response_or_exc
        return response_or_exc

    with mock.patch("marcellus.watchdog.httpx.get", side_effect=_get):
        outcome, _ = _classify("http://frigate.test:5000/api/version", 2.0)
    return outcome


def test_200_is_healthy() -> None:
    assert _classify_with(httpx.Response(200)) == "healthy"


def test_5xx_counts_toward_restart() -> None:
    assert _classify_with(httpx.Response(502)) == "down"


def test_transport_error_counts_toward_restart() -> None:
    assert _classify_with(httpx.ConnectError("refused")) == "down"


def test_4xx_is_not_the_hang_signature() -> None:
    # 401/404 mean Frigate is up and answering; restarting on those would
    # bounce a healthy container over an auth or path misconfiguration.
    assert _classify_with(httpx.Response(401)) == "other"


# -- restart cap -------------------------------------------------------------


def test_restart_cap_blocks_a_restart_storm() -> None:
    cfg = _cfg(max_restarts_per_hour=2)
    restarts: deque[float] = deque([1000.0, 2000.0])
    with mock.patch("marcellus.watchdog.subprocess.run") as run:
        assert _try_restart(cfg, restarts, now=2500.0) is False
    run.assert_not_called()


def test_restart_cap_window_slides() -> None:
    # Restarts older than an hour age out; a fresh failure may restart again.
    cfg = _cfg(max_restarts_per_hour=2)
    restarts: deque[float] = deque([1000.0, 2000.0])
    ok = mock.Mock(returncode=0, stdout="frigate\n", stderr="")
    with mock.patch("marcellus.watchdog.subprocess.run", return_value=ok):
        assert _try_restart(cfg, restarts, now=1000.0 + 3601.0) is True
    assert len(restarts) == 2  # one aged out, one new appended


def test_prune_keeps_entries_inside_the_window() -> None:
    restarts: deque[float] = deque([0.0, 100.0, 3000.0])
    _prune(restarts, now=3650.0)
    assert list(restarts) == [100.0, 3000.0]


# -- restart command failure paths -------------------------------------------


def test_successful_restart_is_recorded() -> None:
    cfg = _cfg()
    restarts: deque[float] = deque()
    ok = mock.Mock(returncode=0, stdout="frigate\n", stderr="")
    with mock.patch("marcellus.watchdog.subprocess.run", return_value=ok):
        assert _try_restart(cfg, restarts, now=100.0) is True
    assert list(restarts) == [100.0]


def test_failed_restart_is_not_counted_against_the_cap() -> None:
    # A command that exits nonzero didn't restart anything; counting it
    # against the hourly cap would lock out the retry that might work.
    cfg = _cfg()
    restarts: deque[float] = deque()
    bad = mock.Mock(returncode=1, stdout="", stderr="no such container")
    with mock.patch("marcellus.watchdog.subprocess.run", return_value=bad):
        assert _try_restart(cfg, restarts, now=100.0) is False
    assert not restarts


def test_timed_out_restart_returns_false() -> None:
    cfg = _cfg()
    restarts: deque[float] = deque()
    with mock.patch(
        "marcellus.watchdog.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker restart frigate", timeout=5.0),
    ):
        assert _try_restart(cfg, restarts, now=100.0) is False
    assert not restarts


def test_unrunnable_command_returns_false() -> None:
    cfg = _cfg(restart_command=["/no/such/binary"])
    restarts: deque[float] = deque()
    with mock.patch(
        "marcellus.watchdog.subprocess.run", side_effect=OSError("not found")
    ):
        assert _try_restart(cfg, restarts, now=100.0) is False
    assert not restarts


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
