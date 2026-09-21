"""Migration-safety contract for the sidecar DB schema.

`db.SIDECAR_SCHEMA` is the fresh (`CREATE TABLE IF NOT EXISTS ...`) schema;
`db._ADDED_COLUMNS` + `db._apply_added_columns` are the additive-ALTER path
that brings an EXISTING deployment's DB up to date. The two must produce
byte-identical `PRAGMA table_info`/`PRAGMA index_list` results, or a fresh
install and a migrated one silently diverge.

Deriving the "old" (pre-`_ADDED_COLUMNS`) schema by programmatically
stripping columns named in `_ADDED_COLUMNS` out of `SIDECAR_SCHEMA`'s CREATE
TABLE text is brittle here: several of those columns are the LAST column in
their table (no trailing comma to also strip) and a couple of the CREATE
TABLE bodies contain inline `CHECK(x IN (...))` constraints with their own
commas, so a naive comma-split misparses them. Per the migration-schema
spec, this test instead keeps a frozen, hand-written copy of the affected
tables (`push_devices`, `push_handles`, `push_activities`, `push_cards`,
`face_enrichments`, `encounter_members`) as they existed before their
`_ADDED_COLUMNS` entries were added, confirmed against git history / the
comments in db.py at write time.
Every other table is reused verbatim from the current `SIDECAR_SCHEMA`.
"""

from __future__ import annotations

import re
import sqlite3

from marcellus import db

# Frozen pre-migration CREATE TABLE text for the tables that have
# `_ADDED_COLUMNS` entries, exactly as they were before those columns
# existed (indexes on these tables are unaffected and are NOT duplicated
# here -- they're left in place from the current SIDECAR_SCHEMA text).
_OLD_TABLES: dict[str, str] = {
    "push_devices": """
CREATE TABLE IF NOT EXISTS push_devices (
    apns_token   TEXT PRIMARY KEY,
    device_id    TEXT NOT NULL,
    bundle_id    TEXT NOT NULL,
    environment  TEXT NOT NULL CHECK(environment IN ('sandbox','prod')),
    app_version  TEXT NOT NULL DEFAULT '',
    cameras      TEXT NOT NULL DEFAULT '[]',
    labels       TEXT NOT NULL DEFAULT '[]',
    min_severity TEXT NOT NULL DEFAULT 'alert' CHECK(min_severity IN ('alert','detection')),
    registered_at TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
""",
    "push_handles": """
CREATE TABLE IF NOT EXISTS push_handles (
    handle       TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    review_id    TEXT NOT NULL,
    created_at   REAL NOT NULL,
    expires_at   REAL NOT NULL
);
""",
    "push_activities": """
CREATE TABLE IF NOT EXISTS push_activities (
    activity_id  TEXT PRIMARY KEY,
    apns_token   TEXT NOT NULL,
    situation_id TEXT NOT NULL,
    track_id     TEXT NOT NULL,
    camera       TEXT NOT NULL DEFAULT '',
    token        TEXT NOT NULL DEFAULT '',
    collapse_id  TEXT NOT NULL DEFAULT '',
    handle       TEXT NOT NULL DEFAULT '',
    stage        TEXT NOT NULL DEFAULT 'arriving',
    thumbnail_revision INTEGER NOT NULL DEFAULT 1,
    from_detection INTEGER NOT NULL DEFAULT 0,
    promoted     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL,
    last_push_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    ended_at     REAL
);
""",
    "push_cards": """
CREATE TABLE IF NOT EXISTS push_cards (
    card_key      TEXT PRIMARY KEY,
    level         TEXT NOT NULL,
    subject_kind  TEXT NOT NULL DEFAULT '',
    place_class   TEXT NOT NULL DEFAULT '',
    camera        TEXT NOT NULL DEFAULT '',
    zone_name     TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    state_since_at REAL NOT NULL,
    sound_count   INTEGER NOT NULL DEFAULT 0,
    handled       INTEGER NOT NULL DEFAULT 0,
    handled_at    REAL,
    last_sound_at REAL,
    resound_count INTEGER NOT NULL DEFAULT 0,
    resolved      INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0
);
""",
    # Pre-Wave-6B form: no `excluded_at` (soft-exclude) column.
    "face_enrichments": """
CREATE TABLE IF NOT EXISTS face_enrichments (
    event_id          TEXT PRIMARY KEY,
    camera            TEXT NOT NULL,
    event_start_ts    REAL NOT NULL,
    cluster_id        INTEGER,
    distance          REAL,
    faces_found       INTEGER NOT NULL DEFAULT 0,
    faces_used        INTEGER NOT NULL DEFAULT 0,
    best_quality      REAL,
    embedding         BLOB,
    sub_label_written TEXT,
    status            TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    detail            TEXT,
    processed_at      TEXT NOT NULL
);
""",
    "encounter_members": """
CREATE TABLE IF NOT EXISTS encounter_members (
    atom_id         TEXT PRIMARY KEY,
    encounter_id    TEXT NOT NULL,
    camera          TEXT NOT NULL,
    start_time      REAL NOT NULL,
    end_time        REAL,
    severity        TEXT NOT NULL,
    labels_json     TEXT NOT NULL DEFAULT '[]',
    zones_json      TEXT NOT NULL DEFAULT '[]',
    event_ids_json  TEXT NOT NULL DEFAULT '[]',
    sub_labels_json TEXT NOT NULL DEFAULT '[]',
    link_reason     TEXT NOT NULL,
    confidence      REAL NOT NULL,
    joined_at       REAL NOT NULL
);
""",
}


def _old_schema() -> str:
    """`SIDECAR_SCHEMA` with each affected table's CREATE TABLE block
    replaced by its frozen pre-migration form (leaving indexes, the toybox
    seed INSERT, and every unaffected table untouched)."""
    schema = db.SIDECAR_SCHEMA
    for table, old_ddl in _OLD_TABLES.items():
        pattern = re.compile(
            rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \(.*?\n\);\n",
            re.DOTALL,
        )
        new_schema, n = pattern.subn(old_ddl.strip() + "\n", schema, count=1)
        assert n == 1, f"couldn't find {table}'s CREATE TABLE in SIDECAR_SCHEMA"
        schema = new_schema
    return schema


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _table_info(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    """name -> (type, notnull, dflt_value, pk), NOT ordered by column position.

    `ALTER TABLE ... ADD COLUMN` always appends, regardless of where the
    equivalent column sits in a from-scratch `CREATE TABLE` (e.g.
    `push_activities.dwell_seconds` is declared mid-table in
    `SIDECAR_SCHEMA` today but would land last via the migration path) --
    column ORDER is not part of SQLite's on-disk column identity contract
    (columns are addressed by name everywhere in this codebase), so this
    intentionally compares the per-column definitions unordered rather than
    positionally.
    """
    return {
        r["name"]: (r["type"], r["notnull"], r["dflt_value"], r["pk"])
        for r in conn.execute(f"PRAGMA table_info({table})")
    }


def _index_list(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [
        (r["name"], r["unique"], r["origin"], r["partial"])
        for r in conn.execute(f"PRAGMA index_list({table})")
    ]


def test_migrated_schema_matches_fresh_schema() -> None:
    # DB A: today's fresh schema, straight from SIDECAR_SCHEMA.
    conn_a = sqlite3.connect(":memory:")
    conn_a.row_factory = sqlite3.Row
    conn_a.executescript(db.SIDECAR_SCHEMA)

    # DB B: the "old" (pre-_ADDED_COLUMNS) schema, then migrated forward the
    # same way `open_sidecar` brings an existing deployment's DB up to date.
    conn_b = sqlite3.connect(":memory:")
    conn_b.row_factory = sqlite3.Row
    conn_b.executescript(_old_schema())
    db._apply_added_columns(conn_b)
    conn_b.commit()

    tables_a = set(_table_names(conn_a))
    tables_b = set(_table_names(conn_b))
    assert tables_a == tables_b, (tables_a, tables_b)

    for table in sorted(tables_a):
        assert _table_info(conn_a, table) == _table_info(conn_b, table), (
            f"{table}: PRAGMA table_info diverges between fresh and migrated schema"
        )
        assert _index_list(conn_a, table) == _index_list(conn_b, table), (
            f"{table}: PRAGMA index_list diverges between fresh and migrated schema"
        )

    conn_a.close()
    conn_b.close()


def test_old_schema_actually_lacks_the_added_columns() -> None:
    """Sanity check on the frozen fixture itself: without this, a typo that
    makes `_old_schema()` accidentally equal the current schema would pass
    the migration test having exercised nothing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_old_schema())
    for table, columns in db._ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, _decl in columns:
            assert name not in have, f"{table}.{name} unexpectedly already in the old schema"
    conn.close()


def test_added_columns_has_no_duplicate_keys() -> None:
    """`_ADDED_COLUMNS` is a literal dict: a duplicate table key would
    silently clobber the earlier entry (Python dict semantics), and a
    duplicate (table, column) pair across a table's own list would issue a
    second, failing `ALTER TABLE ... ADD COLUMN` against a column that
    already exists. Both hazards are noted at db.py:~436."""
    seen: set[tuple[str, str]] = set()
    dupes: list[tuple[str, str]] = []
    for table, columns in db._ADDED_COLUMNS.items():
        for name, _decl in columns:
            key = (table, name)
            if key in seen:
                dupes.append(key)
            seen.add(key)
    assert not dupes, f"duplicate (table, column) pairs in _ADDED_COLUMNS: {dupes}"
