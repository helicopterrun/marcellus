"""Sidecar lock handling: the failed-write snapshot trap, connection pragmas,
and how lock failures are logged.

Every test here uses a real database file under `tmp_path` and two separate
connections -- `:memory:` has no WAL and no second connection to contend with.
Nothing depends on `sqlite_errorcode`/`sqlite_errorname`, which Python 3.10
(production and one CI leg) does not have.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

import pytest

from marcellus import db


def _two_writers(path: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """`a` is a sidecar connection the way the app opens one; `other` is any
    other writer (push pipeline, a request handler), in autocommit so each
    statement is its own transaction unless it says BEGIN."""
    a = db.open_sidecar(path, busy_timeout_ms=50)
    a.execute("CREATE TABLE IF NOT EXISTS t (k INTEGER PRIMARY KEY, v INTEGER)")
    a.commit()
    other = sqlite3.connect(path, timeout=2.0, isolation_level=None)
    return a, other


def _fail_one_write(a: sqlite3.Connection, other: sqlite3.Connection) -> None:
    """`a`'s write waits out its busy timeout behind `other` and fails --
    the ordinary, expected kind of lock error."""
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO t (v) VALUES (1)")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            a.execute("INSERT INTO t (v) VALUES (2)")
    finally:
        other.execute("COMMIT")


def test_a_failed_write_leaves_the_connection_failing_instantly(tmp_path: Path) -> None:
    """The production mechanism, reproduced.

    Python's implicit BEGIN survives the failed INSERT, so the connection is
    still in a transaction; its next read pins a snapshot, another connection
    commits, and the write after that fails at once (SQLITE_BUSY_SNAPSHOT) --
    with a 5 s busy timeout that is never consulted.
    """
    a, other = _two_writers(tmp_path / "sidecar.db")
    try:
        _fail_one_write(a, other)
        assert a.in_transaction, "the failed write's transaction is still open"

        a.execute("PRAGMA busy_timeout = 5000")
        for _ in range(3):
            a.execute("SELECT COUNT(*) FROM t").fetchone()
            other.execute("INSERT INTO t (v) VALUES (3)")
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                a.execute("INSERT INTO t (v) VALUES (4)")
            assert time.monotonic() - started < 2.0, "should fail without waiting"
    finally:
        a.close()
        other.close()


def test_rollback_quietly_clears_the_failed_write(tmp_path: Path) -> None:
    a, other = _two_writers(tmp_path / "sidecar.db")
    try:
        _fail_one_write(a, other)
        db.rollback_quietly(a)
        assert not a.in_transaction

        # The same interleaving that failed above now succeeds every time.
        for _ in range(3):
            a.execute("SELECT COUNT(*) FROM t").fetchone()
            other.execute("INSERT INTO t (v) VALUES (3)")
            a.execute("INSERT INTO t (v) VALUES (4)")
            a.commit()
        assert a.execute("SELECT COUNT(*) FROM t WHERE v = 4").fetchone()[0] == 3
    finally:
        a.close()
        other.close()


def test_rollback_quietly_leaves_a_clean_connection_alone(tmp_path: Path) -> None:
    conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        assert not conn.in_transaction
        db.rollback_quietly(conn)
        assert not conn.in_transaction
    finally:
        conn.close()


def test_sidecar_connections_use_wal_with_synchronous_normal(tmp_path: Path) -> None:
    """NORMAL (1), not the FULL (2) default: in WAL mode FULL fsyncs on every
    commit while the write lock is held."""
    sidecar = tmp_path / "sidecar.db"
    conn = db.open_sidecar(sidecar)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.SIDECAR_BUSY_TIMEOUT_MS
    finally:
        conn.close()

    frigate = tmp_path / "frigate.db"
    sqlite3.connect(frigate).close()
    joined = db.open_joined(frigate, sidecar, busy_timeout_ms=7000)
    try:
        assert joined.execute("PRAGMA sidecar.synchronous").fetchone()[0] == 1
        assert joined.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    finally:
        joined.close()


def test_open_sidecar_busy_timeout_is_adjustable(tmp_path: Path) -> None:
    conn = db.open_sidecar(tmp_path / "sidecar.db", busy_timeout_ms=1234)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("database is locked", True),
        ("database table is locked", True),
        ("no such table: t", False),
    ],
)
def test_is_lock_error(message: str, expected: bool) -> None:
    assert db.is_lock_error(sqlite3.OperationalError(message)) is expected
    assert db.is_lock_error(RuntimeError(message)) is False


def test_a_lock_failure_logs_one_line(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("marcellus.test.locking")
    with caplog.at_level(logging.INFO, logger=log.name):
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError:
            db.log_db_failure(log, "scrub: %s failed for camera %s", "live edge", "doorbell")
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None, "a busy lock must not log a traceback"
    assert record.getMessage() == (
        "scrub: live edge failed for camera doorbell (sidecar DB busy: database is locked)"
    )


def test_any_other_failure_keeps_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("marcellus.test.locking")
    with caplog.at_level(logging.INFO, logger=log.name):
        try:
            raise sqlite3.OperationalError("no such table: scrub_buckets")
        except sqlite3.OperationalError:
            db.log_db_failure(log, "scrub: %s failed", "backfill")
        try:
            raise ValueError("bad frame")
        except ValueError:
            db.log_db_failure(log, "scrub: flushing failed", level=logging.WARNING)
    first, second = caplog.records
    assert first.levelno == logging.ERROR and first.exc_info is not None
    assert second.levelno == logging.WARNING and second.exc_info is not None
