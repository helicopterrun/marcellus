"""`PushEngine.sweep_activities` also closes stale open cards (Work item B,
branch fix/la-duplicate-starts-card-leak): an open card whose resolve never
arrived (dropped Frigate `end`, failed write) must not leak forever and
inflate `extra_stories` on every device's Live Activity."""

from __future__ import annotations

import asyncio
from pathlib import Path

from marcellus import db
from marcellus.push import card_store
from marcellus.push.cards import Card
from marcellus.push.engine import PushEngine
from marcellus.push.transport import LogTransport


def _make_engine(db_path: Path) -> PushEngine:
    return PushEngine(
        db_path=str(db_path), transport=LogTransport(), server_id="s1",
        card_resolution_s=600.0,
    )


def test_sweep_activities_closes_stale_open_cards(sidecar_db_path: Path) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    stale = Card(
        card_key="stale1", level="notify", created_at=1.0, updated_at=100.0, state_since_at=1.0,
    )
    fresh = Card(
        card_key="fresh1", level="notify", created_at=1.0, updated_at=990.0, state_since_at=1.0,
    )
    card_store.upsert_card(conn, stale)
    card_store.upsert_card(conn, fresh)
    conn.close()

    engine = _make_engine(sidecar_db_path)
    asyncio.run(engine.sweep_activities(now=1000.0))

    conn = db.open_sidecar(sidecar_db_path)
    assert card_store.get_card(conn, "stale1").closed is True
    assert card_store.get_card(conn, "fresh1").closed is False
    conn.close()


def test_sweep_activities_leaves_already_closed_cards_untouched(
    sidecar_db_path: Path,
) -> None:
    conn = db.open_sidecar(sidecar_db_path)
    already_closed = Card(
        card_key="closed1", level="notify", created_at=1.0, updated_at=100.0,
        state_since_at=1.0, closed=True, resolved=True,
    )
    card_store.upsert_card(conn, already_closed)
    conn.close()

    engine = _make_engine(sidecar_db_path)
    asyncio.run(engine.sweep_activities(now=1000.0))

    conn = db.open_sidecar(sidecar_db_path)
    fetched = card_store.get_card(conn, "closed1")
    assert fetched.closed is True
    assert fetched.updated_at == 100.0
    conn.close()


def test_sweep_activities_stale_card_no_longer_counted_as_open(
    sidecar_db_path: Path,
) -> None:
    """After the sweep, a leaked card no longer shows up in `list_open_cards`
    -- the candidate set `extra_stories` is derived from."""
    conn = db.open_sidecar(sidecar_db_path)
    stale = Card(
        card_key="stale2", level="notify", created_at=1.0, updated_at=100.0, state_since_at=1.0,
    )
    card_store.upsert_card(conn, stale)
    conn.close()

    engine = _make_engine(sidecar_db_path)
    asyncio.run(engine.sweep_activities(now=1000.0))

    conn = db.open_sidecar(sidecar_db_path)
    keys = {c.card_key for c, _ctx in card_store.list_open_cards(conn)}
    assert "stale2" not in keys
    conn.close()
