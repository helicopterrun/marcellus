"""Store writes commit themselves (Wave 2B §3).

`db.open_sidecar` uses sqlite3's default isolation: the first write on a
connection opens an implicit transaction that stays open -- holding the WAL
write lock -- until something commits, which for an `async def` route
handler can span every `await` in the body. That's the "database is locked"
HTTP routes were seeing. Every write helper in `push/store.py` now commits
(or, for a genuinely multi-statement helper, wraps itself in `with conn:`)
before returning.

Two layers of coverage:

1. A generic introspection sweep (spec's own suggestion) over every *public*
   write helper -- one whose source contains INSERT/UPDATE/DELETE -- so a
   future helper that forgets to commit fails this test without anyone
   having to remember to add it here by name. Private helpers (leading `_`)
   are excluded: a private, non-committing primitive used only inside a
   `with conn:` block by its public callers is the intended shape for a
   handful of these (see `replace_snoozes`'s docstring), not an oversight.
2. A concrete two-physical-connection test for three representative helpers
   (one device write, one activity write, one snooze write) proving the
   commit is real -- visible to a second connection without either side
   calling `commit()` itself.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

from marcellus import db
from marcellus.push import store

_SQL_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE)\b")


def _public_write_helpers() -> list[str]:
    """Every function defined in `push.store` (not imported into it), not
    private, whose own source contains a literal INSERT/UPDATE/DELETE."""
    names = []
    for name, fn in vars(store).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if fn.__module__ != store.__name__:
            continue
        try:
            src = inspect.getsource(fn)
        except OSError:  # pragma: no cover - defensive
            continue
        if _SQL_WRITE.search(src):
            names.append(name)
    return names


def test_every_public_write_helper_commits_or_wraps_with_conn() -> None:
    names = _public_write_helpers()
    # Sanity floor so a refactor that accidentally renames/hides every write
    # helper (making the sweep vacuously pass) doesn't slip through quietly.
    assert len(names) >= 15, names

    missing = []
    for name in names:
        src = inspect.getsource(getattr(store, name))
        if "conn.commit()" not in src and "with conn:" not in src:
            missing.append(name)
    assert missing == [], (
        f"write helper(s) in push/store.py with no commit()/with-conn: {missing}"
    )


def _two_connections(db_path: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    """Both physical connections to the same WAL-mode sidecar DB -- a write
    on one is only visible on the other once it's actually committed."""
    conn_a = db.open_sidecar(db_path)
    conn_b = db.open_sidecar(db_path)
    return conn_a, conn_b


def test_upsert_device_visible_on_second_connection_without_explicit_commit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sidecar.db"
    conn_a, conn_b = _two_connections(db_path)
    try:
        store.upsert_device(
            conn_a, apns_token="tokA", bundle_id="com.example.elsinore",
            environment="sandbox", cameras=["doorbell"], min_severity="alert",
        )
        # No conn_a.commit() here -- upsert_device must have done it itself.
        assert store.get_device(conn_b, "tokA") is not None
    finally:
        conn_a.close()
        conn_b.close()


def test_open_activity_visible_on_second_connection_without_explicit_commit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sidecar.db"
    conn_a, conn_b = _two_connections(db_path)
    try:
        store.upsert_device(
            conn_a, apns_token="tokA", bundle_id="com.example.elsinore",
            environment="sandbox", cameras=[], min_severity="alert",
        )
        store.open_activity(
            conn_a, activity_id="a_1", apns_token="tokA",
            situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID,
            camera="doorbell", collapse_id=store.DEVICE_SITUATION_ID, handle="",
        )
        # Again, no conn_a.commit() -- open_activity's own commit is what a
        # second connection is relying on here.
        assert store.get_activity(conn_b, "a_1") is not None
    finally:
        conn_a.close()
        conn_b.close()


def test_set_snooze_visible_on_second_connection_without_explicit_commit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sidecar.db"
    conn_a, conn_b = _two_connections(db_path)
    try:
        store.set_snooze(conn_a, apns_token="tokA", scope="global", until_epoch=9_999_999_999.0)
        assert "global" in store.active_snoozes(conn_b, "tokA", now=0.0)
    finally:
        conn_a.close()
        conn_b.close()


def test_replace_snoozes_delete_and_inserts_are_atomic(tmp_path: Path) -> None:
    """`replace_snoozes` wraps its delete+insert(s) in one `with conn:` --
    a second connection must see either the OLD complete set or the NEW
    complete set, never a state with the old ones gone and none of the new
    ones landed yet (which independently-committed helper calls would risk)."""
    db_path = tmp_path / "sidecar.db"
    conn_a, conn_b = _two_connections(db_path)
    try:
        store.set_snooze(conn_a, apns_token="tokA", scope="global", until_epoch=9_999_999_999.0)
        assert store.active_snoozes(conn_b, "tokA", now=0.0) == {"global"}

        store.replace_snoozes(
            conn_a, apns_token="tokA",
            snoozes=[
                {"scope": "camera:doorbell", "until_epoch": 9_999_999_999.0},
                {"scope": "camera:garden", "until_epoch": 9_999_999_999.0},
            ],
        )
        seen = store.active_snoozes(conn_b, "tokA", now=0.0)
        assert seen == {"camera:doorbell", "camera:garden"}
    finally:
        conn_a.close()
        conn_b.close()


def test_delete_activity_row_and_sends_commit_together(tmp_path: Path) -> None:
    """The other multi-statement helper (spec §3): deleting the activity row
    and its send-history rows must land together, wrapped in `with conn:`."""
    db_path = tmp_path / "sidecar.db"
    conn_a, conn_b = _two_connections(db_path)
    try:
        store.upsert_device(
            conn_a, apns_token="tokA", bundle_id="com.example.elsinore",
            environment="sandbox", cameras=[], min_severity="alert",
        )
        store.open_activity(
            conn_a, activity_id="a_1", apns_token="tokA",
            situation_id=store.DEVICE_SITUATION_ID, track_id=store.DEVICE_TRACK_ID,
            camera="doorbell", collapse_id=store.DEVICE_SITUATION_ID, handle="",
        )
        store.record_activity_send(conn_a, activity_id="a_1")
        assert store.get_activity(conn_b, "a_1") is not None

        store.delete_activity(conn_a, "a_1")
        assert store.get_activity(conn_b, "a_1") is None
        row = conn_b.execute(
            "SELECT COUNT(*) AS n FROM push_activity_sends WHERE activity_id = ?", ("a_1",)
        ).fetchone()
        assert row["n"] == 0
    finally:
        conn_a.close()
        conn_b.close()
