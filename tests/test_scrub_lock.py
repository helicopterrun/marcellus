"""`scrub/lock.py`: the single-writer lock on the scrub cache directory."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from marcellus.scrub.lock import ScrubCacheLock, ScrubLockHeld

runner = CliRunner()


def test_acquire_release_round_trip(tmp_path: Path) -> None:
    lock = ScrubCacheLock(tmp_path / "cache")
    lock.acquire()
    lock.release()
    # Re-acquirable immediately after release.
    lock.acquire()
    lock.release()


def _hold_lock(
    cache_dir: str,
    ready: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    lock = ScrubCacheLock(Path(cache_dir))
    lock.acquire()
    ready.set()
    release.wait(timeout=10)
    lock.release()


def test_second_lock_raises_while_held_cross_process(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(cache_dir), ready, release))
    proc.start()
    try:
        assert ready.wait(timeout=10), "holder process never acquired the lock"
        # Give the write of pid/argv into the lock file a moment to land.
        time.sleep(0.3)

        lock = ScrubCacheLock(cache_dir, timeout_s=0.0)
        with pytest.raises(ScrubLockHeld) as excinfo:
            lock.acquire()
        assert "locked by another process" in str(excinfo.value)
        assert f"pid {proc.pid}" in str(excinfo.value)
    finally:
        release.set()
        proc.join(timeout=10)

    # After the holder exits, the lock is immediately acquirable.
    lock2 = ScrubCacheLock(cache_dir, timeout_s=0.0)
    lock2.acquire()
    lock2.release()


def test_cli_scrub_prune_exits_2_when_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcellus.cli import app
    from marcellus.config import Settings

    cache_dir = tmp_path / "cache"
    lock = ScrubCacheLock(cache_dir)
    lock.acquire()
    try:
        settings = Settings()
        settings = settings.model_copy(
            update={"scrub": settings.scrub.model_copy(update={"cache_dir": cache_dir})}
        )
        monkeypatch.setattr("marcellus.cli.load_settings", lambda: settings)

        result = runner.invoke(app, ["scrub", "prune"])
        assert result.exit_code == 2
    finally:
        lock.release()


def test_context_manager_acquires_and_releases(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    with ScrubCacheLock(cache_dir):
        pass
    # Re-entrant use of a fresh instance proves release happened.
    with ScrubCacheLock(cache_dir):
        pass
