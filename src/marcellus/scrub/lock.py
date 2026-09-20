"""Single-writer lock on the scrub cache directory.

Two writers racing the same cache dir (a restarting service process, a CLI
`fsc scrub` invocation, an overlapping systemd unit) can corrupt in-flight
sheet publication or double-run retention pruning. The lock is a plain
`fcntl.flock(LOCK_EX)` on `<cache_dir>/.lock` -- released by the kernel the
moment the holding process exits (crash, SIGKILL, normal exit alike), so
there is no pidfile to reap and no stale-lock cleanup path to get wrong.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from pathlib import Path
from types import TracebackType


class ScrubLockHeld(RuntimeError):
    """Raised when the scrub cache lock could not be acquired in time."""


_POLL_INTERVAL_S = 0.2


class ScrubCacheLock:
    """Advisory single-writer lock on `<cache_dir>/.lock`.

    Not reentrant and not thread-safe for multiple acquires on the same
    instance -- one `ScrubCacheLock` per holder.
    """

    def __init__(self, cache_dir: Path, timeout_s: float = 0.0) -> None:
        self._cache_dir = cache_dir
        self._path = cache_dir / ".lock"
        self._timeout_s = timeout_s
        self._fd: int | None = None

    def acquire(self, timeout_s: float | None = None) -> None:
        if timeout_s is None:
            timeout_s = self._timeout_s
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = self._describe_holder(fd)
                    os.close(fd)
                    suffix = f" (held by {holder})" if holder else ""
                    raise ScrubLockHeld(
                        f"scrub cache {self._path} is locked by another process{suffix}"
                    ) from None
                time.sleep(_POLL_INTERVAL_S)
        # Informational only: helps a future contender's error message name
        # who is holding the lock. Best-effort -- never fails acquisition.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {os.path.basename(sys.argv[0])}\n".encode())
        except OSError:
            pass
        self._fd = fd

    def _describe_holder(self, fd: int) -> str | None:
        try:
            with open(self._path) as f:
                content = f.read().strip()
        except OSError:
            return None
        if not content:
            return None
        parts = content.split(None, 1)
        pid = parts[0] if parts else ""
        argv = parts[1] if len(parts) > 1 else ""
        if not pid:
            return None
        return f"pid {pid} ({argv})" if argv else f"pid {pid}"

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> ScrubCacheLock:
        self.acquire(self._timeout_s)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
