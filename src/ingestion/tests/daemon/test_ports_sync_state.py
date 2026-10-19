"""Cursor round-trip against a real SQLite file."""

from __future__ import annotations

from pathlib import Path

import pytest
from ingestion.daemon.ports.sync_state import SqliteSyncStateStore
from ingestion.daemon.store.local import apply_migrations


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


async def test_get_cursor_returns_none_when_unset(db_path: Path) -> None:
    store = SqliteSyncStateStore(db_path)
    assert await store.get_cursor("alice@example.com", "moodle", "default") is None


async def test_set_then_get_round_trip(db_path: Path) -> None:
    store = SqliteSyncStateStore(db_path)
    await store.set_cursor(
        "alice@example.com",
        "moodle",
        "default",
        {"last_seen": "2026-04-19T00:00:00Z", "offsets": {"8740": 17}},
    )
    got = await store.get_cursor("alice@example.com", "moodle", "default")
    assert got == {"last_seen": "2026-04-19T00:00:00Z", "offsets": {"8740": 17}}


async def test_set_cursor_upserts(db_path: Path) -> None:
    store = SqliteSyncStateStore(db_path)
    await store.set_cursor("alice@example.com", "moodle", "default", {"v": 1})
    await store.set_cursor("alice@example.com", "moodle", "default", {"v": 2})
    assert await store.get_cursor("alice@example.com", "moodle", "default") == {"v": 2}


async def test_cursors_are_scoped_per_user_platform_and_scope(db_path: Path) -> None:
    store = SqliteSyncStateStore(db_path)
    await store.set_cursor("alice@example.com", "moodle", "default", {"v": 1})
    await store.set_cursor("bob@example.com", "moodle", "default", {"v": 2})
    await store.set_cursor("alice@example.com", "teams", "default", {"v": 3})
    await store.set_cursor("alice@example.com", "moodle", "messages", {"v": 4})

    assert await store.get_cursor("alice@example.com", "moodle", "default") == {"v": 1}
    assert await store.get_cursor("bob@example.com", "moodle", "default") == {"v": 2}
    assert await store.get_cursor("alice@example.com", "teams", "default") == {"v": 3}
    assert await store.get_cursor("alice@example.com", "moodle", "messages") == {"v": 4}
